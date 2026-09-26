"""The secret store behind placeholder rows (#198).

Real secrets live in a SecretSpec provider; this module is the
daemon's one route to them. The provider is a setting
(``MSKSD_SECRET_STORE_*``, :class:`~msks.settings.SecretStoreSettings`)
so at-rest encryption (``age``) or a managed vault (``awssm``,
``bws``) is a configuration change, never code — the crypto posture
the repo keeps everywhere.

The SecretSpec **CLI** is the integration surface, not the Python
SDK: the SDK's native ABI exposes only the resolve path, while
every write (``set``, ``delete``) lives in the CLI. That matches
the house pattern for external tools — the binary is named by a
setting (``secret_store_cli``), the way the VMM and nft binaries
are — and it keeps values off argv: ``set`` reads the value from
piped stdin (text-trimmed in 0.20; exact bytes arrive with the
0.21+ ``--from-file`` flag the pin can move to).

The daemon owns a generated manifest (``<root>/secretspec.toml``)
declaring every placeholder's backend ref against the configured
provider — regenerated whenever placeholder rows change, because
the database, not the manifest, is the source of truth. Provider
credentials never pass through msksd settings: the subprocess
inherits the daemon's environment, so each provider reads its own
chain (the AWS SDK chain, ``BWS_ACCESS_TOKEN``, …).

Resolved values are cached in memory (the interceptor's #199 swap
path reads this cache); a daemon restart starts the cache empty and
re-fetches by backend ref on first use.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import os
import re
import secrets as pysecrets
from pathlib import Path

LOG = logging.getLogger(__name__)

#: The sentinel format: versioned prefix + 32 random bytes
#: base64url — uniform fixed length, so the wire matcher can
#: recognize it without parsing (#198).
SENTINEL_PREFIX = "mskssec1_"

#: The manifest the daemon generates and owns, inside the store root.
MANIFEST_NAME = "secretspec.toml"

#: The prefix every minted backend ref carries. The resolved
#: values are read inside workspaces, so the ref carries the
#: workspace family's prefix (#335).
REF_PREFIX = "MSKSWS_"

#: The prefix refs minted before #335's rename carry; the startup
#: migration rewrites any row still holding one.
LEGACY_REF_PREFIX = "MSKS_"

#: The audit/ref identifier rule SecretSpec enforces on names:
#: letters, numbers, and underscores, no leading digit.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A probe ref no minted placeholder can ever own: backend_ref
#: uppercases every emitted ref and lowercase survives sanitization
#: untouched, so a lowercase probe name is unreachable by
#: construction (a workspace literally named ``store`` with a
#: placeholder ``probe`` mints MSKSWS_STORE_PROBE, not this).
PROBE_REF = "msks_store_probe"

#: A process-wide counter for unique manifest temp names: two
#: concurrent syncs (both off the event loop) must never share a
#: temp path.
_TMP_COUNTER = itertools.count()


class SecretStoreError(Exception):
    """A failed store operation, carrying the CLI's stderr tail."""

    def __init__(self, operation: str, detail: str) -> None:
        super().__init__(f"secret store {operation} failed: {detail}")
        self.operation = operation
        self.detail = detail


async def spawn(
    cli: str,
    args: list[str],
    stdin: bytes | None,
    manifest: Path,
    timeout: float,
) -> bytes:
    """One CLI invocation; returns stdout bytes.

    The subprocess inherits the daemon's environment, so each
    provider's own credential chain resolves (the AWS SDK chain,
    ``BWS_ACCESS_TOKEN``). An unrunnable binary — the classic
    misspelled ``secret_store_cli`` — surfaces as the named error
    instead of a bare FileNotFoundError; a non-zero exit carries
    the stderr tail; a hang answers the timeout.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            cli,
            "--file",
            str(manifest),
            *args,
            stdin=(asyncio.subprocess.PIPE if stdin is not None else None),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise SecretStoreError(cli, str(exc)) from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        proc.kill()
        raise SecretStoreError(
            " ".join(args), f"timed out after {timeout}s"
        ) from None
    if proc.returncode != 0:
        raise cli_error(args[0], err)
    return out


def new_sentinel() -> str:
    """A fresh placeholder sentinel (#198)."""
    return SENTINEL_PREFIX + pysecrets.token_urlsafe(32)


def backend_ref(workspace_id: str, name: str) -> str:
    """The SecretSpec declaration name for one placeholder.

    ``MSKSWS_<WS>_<NAME>``: identifier-safe (dashes and dots become
    underscores), uppercase. Two (workspace, name) pairs that
    sanitize identically collide on the unique index — the mint
    answers 409 rather than silently sharing a backend ref.
    """
    parts = [
        re.sub(r"[^A-Za-z0-9_]", "_", part).upper()
        for part in (workspace_id, name)
    ]
    return REF_PREFIX + "_".join(parts)


def valid_name(name: str) -> bool:
    """Whether *name* is a mintable placeholder label."""
    return bool(_IDENTIFIER.match(name))


def provider_uri(store) -> str:
    """The SecretSpec provider URI for the live settings.

    Read live (``app.state.settings``) so a SIGHUP settings swap
    applies to subsequent operations without a restart.
    """
    s = store.settings
    builders = {
        "file": lambda: f"file:{s.root}",
        "age": lambda: (
            f"age://{s.root / 'secrets.age'}?identity={s.age_identity}"
        ),
        "awssm": lambda: awssm_uri(s),
        "bws": lambda: f"bws://{s.project}",
    }
    return builders[s.provider]()


def awssm_uri(s) -> str:
    """The awssm URI: optional profile, required region, optional
    prefix option."""
    uri = f"awssm://{s.region}"
    if s.profile:
        uri = f"awssm://{s.profile}@{s.region}"
    if s.prefix:
        uri += f"?prefix={s.prefix}"
    return uri


def write_private(path: Path, body: str) -> None:
    """Create/replace *path* 0600 in one step (no mode dance)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)


def cli_error(operation: str, err: bytes) -> SecretStoreError:
    """A non-zero exit as an error, carrying the stderr's last line."""
    detail = err.decode(errors="replace").strip().splitlines()
    return SecretStoreError(
        operation, detail[-1] if detail else "unknown error"
    )


def render_manifest(uri: str, refs: list[tuple[str, str]]) -> str:
    """The generated manifest body: one declaration per ref.

    ``refs`` are ``(backend_ref, description)`` pairs; the
    description carries the operator-facing ``workspace/name`` so a
    human reading the store sees msks's naming, not just an
    identifier. TOML basic strings via JSON quoting (the same
    grammar for these values).
    """
    lines = [
        "[project]",
        'name = "msks"',
        'revision = "1.0"',
        "",
        "[providers]",
        f"store = {json.dumps(uri)}",
        "",
        "[profiles.default]",
    ]
    for ref, description in sorted(refs):
        lines.append(
            f"{ref} = {{ description = {json.dumps(description)}, "
            'providers = ["store"], required = false }'
        )
    return "\n".join(lines) + "\n"


class SecretStore:
    """Owns the CLI subprocesses and the value cache; caches only
    ``self.app`` (the klangk ownership rule)."""

    def __init__(self, app) -> None:
        self.app = app
        # ref -> value; the interceptor's swap path reads this.
        self._cache: dict[str, str] = {}

    def cache_clear(self) -> None:
        """Drop every cached value (a SIGHUP settings swap; the next
        read re-fetches from the new settings' store)."""
        self._cache.clear()

    @property
    def settings(self):
        return self.app.state.settings.secret_store

    # --- manifest ---------------------------------------------------

    def manifest_path(self) -> Path:
        return self.settings.root / MANIFEST_NAME

    def sync_manifest(self, refs: list[tuple[str, str]]) -> None:
        """Write the manifest when its body changed.

        Called with every placeholder row (mint, revoke, and expiry
        sweep re-sync); the root is created 0700 and the manifest
        written 0600 — the store holds real secrets' bytes on the
        ``file`` provider, so its directory joins the house pattern
        of secret-bearing artifacts readable only by the daemon's
        user.
        """
        body = render_manifest(provider_uri(self), refs)
        path = self.manifest_path()
        try:
            if path.read_text(encoding="utf-8") == body:
                return
        except FileNotFoundError:
            pass
        root = self.settings.root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Atomic replace from a UNIQUE temp file (0600): two
        # concurrent syncs never share a temp path, a concurrent
        # read never sees a half write, and the mode is right on
        # every sync, not just the first.
        tmp = root / f"{MANIFEST_NAME}.{os.getpid()}.{next(_TMP_COUNTER)}.tmp"
        write_private(tmp, body)
        os.replace(tmp, path)

    # --- subprocess plumbing ----------------------------------------

    async def run(
        self,
        args: list[str],
        stdin: bytes | None = None,
        manifest: Path | None = None,
    ) -> bytes:
        """One CLI invocation against the (or an explicit) manifest;
        see :func:`spawn` for the failure story."""
        return await spawn(
            self.settings.cli,
            args,
            stdin,
            manifest or self.manifest_path(),
            self.settings.timeout_s,
        )

    # --- operations -------------------------------------------------

    async def write(self, ref: str, value: str) -> None:
        """Store one secret under *ref*; the value rides stdin."""
        await self.run(
            ["set", "--provider", provider_uri(self), ref],
            stdin=value.encode(),
        )
        self._cache[ref] = value

    async def read(self, ref: str) -> str:
        """Fetch one secret by ref (cached after the first fetch).

        The live provider URI rides the call (as every operation's
        does), so a connection-detail reload applies without a
        manifest re-sync; the manifest carries declarations, not
        routing. 0.20's ``get`` appends one newline when stdout is
        a pipe; it is stripped here so the value round-trips.
        """
        if ref in self._cache:
            return self._cache[ref]
        out = await self.run(["get", "--provider", provider_uri(self), ref])
        value = out.decode(errors="replace").removesuffix("\n")
        self._cache[ref] = value
        return value

    async def delete(self, ref: str) -> None:
        """Remove one secret's stored value (revoke's store half)."""
        await self.run(["delete", "--provider", provider_uri(self), ref])
        self._cache.pop(ref, None)

    async def check(self) -> dict:
        """Probe the configured store: write, read back, delete.

        A green run means the provider URI is reachable and writable
        before the first mint — a typo'd setting fails here, loudly,
        instead of at first use. The probe ref is unreachable by any
        minted placeholder (lowercase; backend_ref uppercases), the
        probe manifest is 0600 like the synced one (it carries the
        provider URI), and its value lands in — and is removed from
        — the real provider. A cleanup failure after a failed probe
        is suppressed so the ORIGINAL error is the one raised; a
        cleanup failure after a green probe propagates (residue is
        inert, but the operator should hear about it).
        """
        ref = f"{PROBE_REF}_{pysecrets.token_hex(4)}"
        token = SENTINEL_PREFIX + pysecrets.token_urlsafe(8)
        uri = provider_uri(self)
        root = self.settings.root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = root / "probe.toml"
        write_private(probe, render_manifest(uri, [(ref, "store probe")]))
        try:
            await self.run(
                ["set", "--provider", uri, ref],
                stdin=token.encode(),
                manifest=probe,
            )
            out = await self.run(
                ["get", "--provider", uri, ref], manifest=probe
            )
            if out.decode(errors="replace").removesuffix("\n") != token:
                raise SecretStoreError("check", "probe value mismatch")
        except SecretStoreError:
            with contextlib.suppress(SecretStoreError):
                await self.run(["delete", ref], manifest=probe)
            probe.unlink(missing_ok=True)
            raise
        await self.run(["delete", ref], manifest=probe)
        probe.unlink(missing_ok=True)
        return {"provider": self.settings.provider, "ok": True}

    # --- legacy ref migration (#335) ---------------------------------

    def manifest_declares_legacy(self) -> bool:
        """Whether the on-disk manifest still declares a legacy
        ref — exactly what a pass that renamed every row but died
        before its final re-sync leaves behind (the re-sync is
        change-driven, so an unstale manifest is never rewritten).
        No configured store root means no manifest."""
        root = self.settings.root
        if root is None:
            return False
        try:
            text = (root / MANIFEST_NAME).read_text(encoding="utf-8")
        except OSError:
            return False
        return LEGACY_REF_PREFIX in text

    async def migrate_legacy_refs(self) -> int:
        """Move placeholders minted before #335 onto ``MSKSWS_`` refs.

        The minted name is a stored format — placeholder rows, the
        provider's stored values, and the generated manifest all
        carry it — so the daemon rewrites all three at startup,
        while nothing else serves requests yet. Each row migrates
        independently and idempotently: the value lands under the
        new ref before the old one is dropped, and the row's own
        prefix marks it done (a new-format ref can never start with
        the legacy one), so a pass interrupted at any point simply
        resumes on the next boot. A row whose value cannot be read
        (a reconfigured provider, a store that moved) still gets its
        row and manifest rewritten — reads follow the row's ref, so
        what remains is a missing value, not a wrong one.

        Returns the number of legacy rows seen; rows whose store
        copy failed are named in the log and left otherwise intact.
        """
        model = self.app.state.model
        legacy = await self.legacy_placeholder_rows()
        if not legacy and not self.manifest_declares_legacy():
            return 0
        # Declare every legacy row's new ref beside the old one
        # before any value copy: ``set`` answers 404 for refs the
        # manifest does not declare, and the rows still carry the
        # legacy names until each copy lands.
        await asyncio.to_thread(
            self.sync_manifest,
            (await model.placeholder_refs())
            + [
                (
                    backend_ref(row["workspace_id"], row["name"]),
                    f"{row['workspace_id']}/{row['name']}",
                )
                for row in legacy
            ],
        )
        for row in legacy:
            await self.migrate_legacy_row(row)
        # The final manifest drops the legacy declarations: it is
        # re-rendered off the renamed rows alone.
        self._cache.clear()
        refs = await model.placeholder_refs()
        await asyncio.to_thread(self.sync_manifest, refs)
        return len(legacy)

    async def legacy_placeholder_rows(self) -> list[dict]:
        """Rows still carrying the pre-#335 ref format; empty when
        no store root is configured — there is no manifest to
        redeclare and no provider to copy through."""
        if self.settings.root is None:
            return []
        return [
            row
            for row in await self.app.state.model.list_placeholders()
            if row["backend_ref"].startswith(LEGACY_REF_PREFIX)
        ]

    async def migrate_legacy_row(self, row: dict) -> None:
        """One legacy row: copy its value under the new ref, drop
        the old one, repoint the row (#335).

        Any failure logs and leaves the row on its legacy ref —
        reads follow the row's ref, so what remains is a retry on
        the next startup, never a wrong pointer.
        """
        model = self.app.state.model
        old = row["backend_ref"]
        new = backend_ref(row["workspace_id"], row["name"])
        try:
            try:
                value = await self.read(old)
            except SecretStoreError:
                LOG.warning(
                    "placeholder %s/%s has no readable value at "
                    "legacy ref %s; renaming the row without a "
                    "store copy",
                    row["workspace_id"],
                    row["name"],
                    old,
                )
                value = None
            if value is not None:
                await self.write(new, value)
                with contextlib.suppress(SecretStoreError):
                    await self.delete(old)
            await model.rename_placeholder_ref(row["id"], new)
        except Exception:  # noqa: BLE001 - logged, never fatal
            LOG.exception(
                "placeholder %s/%s stays on legacy ref %s; "
                "the next startup retries it",
                row["workspace_id"],
                row["name"],
                old,
            )
