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
import json
import os
import re
import secrets as pysecrets
from pathlib import Path

#: The sentinel format: versioned prefix + 32 random bytes
#: base64url — uniform fixed length, so the wire matcher can
#: recognize it without parsing (#198).
SENTINEL_PREFIX = "mskssec1_"

#: The manifest the daemon generates and owns, inside the store root.
MANIFEST_NAME = "secretspec.toml"

#: The audit/ref identifier rule SecretSpec enforces on names:
#: letters, numbers, and underscores, no leading digit.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

    ``MSKS_<WS>_<NAME>``: identifier-safe (dashes and dots become
    underscores), uppercase. Two (workspace, name) pairs that
    sanitize identically collide on the unique index — the mint
    answers 409 rather than silently sharing a backend ref.
    """
    parts = [
        re.sub(r"[^A-Za-z0-9_]", "_", part).upper()
        for part in (workspace_id, name)
    ]
    return "MSKS_" + "_".join(parts)


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
        # Atomic replace: a temp file (0600) renamed over the live
        # manifest, so a concurrent read never sees a half write and
        # the mode is right on every sync, not just the first.
        tmp = root / (MANIFEST_NAME + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
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

        0.20's ``get`` appends one newline when stdout is a pipe;
        it is stripped here so the value round-trips.
        """
        if ref in self._cache:
            return self._cache[ref]
        out = await self.run(["get", ref])
        value = out.decode(errors="replace").removesuffix("\n")
        self._cache[ref] = value
        return value

    async def delete(self, ref: str) -> None:
        """Remove one secret's stored value (revoke's store half)."""
        await self.run(["delete", ref])
        self._cache.pop(ref, None)

    async def check(self) -> dict:
        """Probe the configured store: write, read back, delete.

        A green run means the provider URI is reachable and writable
        before the first mint — a typo'd setting fails here, loudly,
        instead of at first use. The probe runs against a throwaway
        manifest under the store root (its declaration is temporary;
        the synced manifest is untouched), and its value lands in —
        and is removed from — the real provider.
        """
        ref = "MSKS_STORE_PROBE"
        token = SENTINEL_PREFIX + pysecrets.token_urlsafe(8)
        uri = provider_uri(self)
        root = self.settings.root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = root / "probe.toml"
        probe.write_text(render_manifest(uri, [(ref, "store probe")]))
        try:
            await self.run(
                ["set", "--provider", uri, ref],
                stdin=token.encode(),
                manifest=probe,
            )
            out = await self.run(["get", ref], manifest=probe)
            if out.decode(errors="replace").removesuffix("\n") != token:
                raise SecretStoreError("check", "probe value mismatch")
        finally:
            await self.run(["delete", ref], manifest=probe)
            probe.unlink(missing_ok=True)
        return {"provider": self.settings.provider, "ok": True}
