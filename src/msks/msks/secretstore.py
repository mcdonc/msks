"""The secret store behind placeholder rows (#198, #423).

Real secrets live in one age-encrypted **agefile** under the
store root. The operator supplies each value at mint (a file or
stdin on the CLI, a masked field on the TUI form); the daemon
strips it at both ends, stores it through SecretSpec's ``age``
provider, and never echoes it back — the mint reply and the
on-demand row fetch (#440) carry the sentinel, not the value.
Values ride the token-authenticated TLS API from
the client machine to the daemon's, and the agefile and the age
identity stay on the daemon's host; workspaces keep receiving
only the sentinel.

The age identity is the daemon's own, minted on the spot the
first store operation needs it (the #138 key-minting pattern):
a plaintext age-keygen X25519 file, 0600, named by
``MSKSD_SECRET_STORE_AGE_IDENTITY`` (default
``<store root>/age.key``). Plaintext, because the daemon must
decrypt unattended; the agefile itself stays ciphertext at
rest. msksd never decrypts anything itself — every store
operation spawns the SecretSpec CLI
(``secretspec get/set/delete --provider "age://<root>/secrets.age?identity=<path>"``)
and the agefile is decrypted inside that child process.

The SecretSpec **CLI** is the integration surface, not the Python
SDK: the SDK's native ABI exposes only the resolve path, while
every write (``set``, ``delete``) lives in the CLI. That matches
the house pattern for external tools — the binary is named by a
setting (``secret_store_cli``), the way the VMM and nft binaries
are — and it keeps values off argv: ``set`` reads the value from
piped stdin (text-trimmed in 0.20; exact bytes arrive with the
0.21+ ``--from-file`` flag the pin can move to).

The daemon owns a generated manifest (``<root>/secretspec.toml``)
declaring every placeholder's backend ref against the age
provider — regenerated whenever placeholder rows change, because
the database, not the manifest, is the source of truth.

Resolved values are cached in memory (the interceptor's #199 swap
path reads this cache). The cache clears per-ref on write and
delete, and wholesale on a settings swap; a daemon restart
starts it empty and re-fetches by backend ref on first use.
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

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from .model.secrets import coverage_label

LOG = logging.getLogger(__name__)

#: The scoped sentinel format: versioned prefix + 32 random bytes
#: base64url — uniform fixed length, so the wire matcher can
#: recognize it without parsing (#198).
SENTINEL_PREFIX = "mskssec1_"

#: The daemon-wide sentinel format (#339): the same body under a
#: distinct prefix, so an operator who finds a sentinel in a log
#: or a guest can tell its reach from the string alone —
#: ``mskssec1_`` swaps on its row's workspaces, ``mskssec2_`` on
#: every accepting workspace's tap.
DAEMON_SENTINEL_PREFIX = "mskssec2_"

#: The encrypted vault's file name, inside the store root.
AGEFILE_NAME = "secrets.age"

#: The age identity's default file name, inside the store root.
AGE_IDENTITY_NAME = "age.key"

#: The manifest the daemon generates and owns, inside the store root.
MANIFEST_NAME = "secretspec.toml"

#: The prefix every scoped backend ref carries. The resolved
#: values are read inside workspaces, so the ref carries the
#: workspace family's prefix (#335).
REF_PREFIX = "MSKSWS_"

#: The prefix a daemon-wide row's backend ref carries (#339): a
#: ref family of its own, so the store's namespace names the
#: row's reach the same way the sentinel prefix does. It shares no
#: spelling with the legacy ``MSKS_`` family (the fifth character
#: differs), so the #335 startup migration never claims one.
DAEMON_REF_PREFIX = "MSKSDAEMON_"

#: The prefix refs minted before #335's rename carry; the startup
#: migration rewrites any row still holding one.
LEGACY_REF_PREFIX = "MSKS_"

#: A legacy declaration key at line start in the generated
#: manifest — a new-format ref can contain the legacy prefix
#: inside its name (``MSKSWS_MSKS_...``), so the staleness check
#: matches keys, not substrings.
_LEGACY_DECLARATION = re.compile(r"(?m)^MSKS_[A-Za-z0-9_]+\s*=")

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

    An unrunnable binary — the classic misspelled
    ``secret_store_cli`` — surfaces as the named error instead of
    a bare FileNotFoundError; a non-zero exit carries the stderr
    tail; a hang answers the timeout.
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


def new_sentinel(daemon_wide: bool = False) -> str:
    """A fresh placeholder sentinel (#198, #339): the
    daemon-wide row mints under ``mskssec2_``, a scoped row under
    ``mskssec1_``."""
    prefix = DAEMON_SENTINEL_PREFIX if daemon_wide else SENTINEL_PREFIX
    return prefix + pysecrets.token_urlsafe(32)


def backend_ref(workspaces: list[str], name: str) -> str:
    """The SecretSpec declaration name for one placeholder (#339:
    *workspaces* is the row's coverage set).

    Scoped: ``MSKSWS_<WS...>_<NAME>`` — identifier-safe (dashes
    and dots become underscores), uppercase, the coverage's sorted
    ids in order. Daemon-wide (an empty coverage): a ref of its
    own family, ``MSKSDAEMON_<NAME>``. Two coverage/name pairs
    that sanitize identically collide on the unique index — the
    mint answers 409 rather than silently sharing a backend ref.
    """
    parts = [
        re.sub(r"[^A-Za-z0-9_]", "_", part).upper()
        for part in (
            [*sorted(set(workspaces)), name] if workspaces else [name]
        )
    ]
    prefix = REF_PREFIX if workspaces else DAEMON_REF_PREFIX
    return prefix + "_".join(parts)


def valid_name(name: str) -> bool:
    """Whether *name* is a mintable placeholder label."""
    return bool(_IDENTIFIER.match(name))


def provider_uri(store) -> str:
    """The SecretSpec provider URI — the agefile vault — for the
    live settings (read live so a SIGHUP settings swap applies to
    subsequent operations without a restart). The identity is
    ensured first: the secretspec child cannot use a URI whose
    identity file is absent, so the first operation against a
    fresh state dir is the one that mints it.
    """
    s = store.settings
    identity = Path(s.age_identity or (s.root / AGE_IDENTITY_NAME))
    ensure_age_identity(identity)
    return f"age://{s.root / AGEFILE_NAME}?identity={identity}"


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


#: The bech32 character set (BIP-173): age identities and
#: recipients encode with it.
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_polymod(values: list[int]) -> int:
    """The bech32 checksum accumulator over *values* (5-bit)."""
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def bech32_checksum(hrp: str, data: list[int]) -> list[int]:
    """The six 5-bit checksum words for *data* under *hrp* (the
    bech32 const-1 variant age uses)."""
    expanded = [ord(c) >> 5 for c in hrp] + [0]
    expanded += [ord(c) & 31 for c in hrp]
    polymod = bech32_polymod(expanded + data + [0] * 6) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def convertbits(data: bytes, frombits: int, tobits: int) -> list[int]:
    """Re-group *data*'s bits: 8-bit bytes into 5-bit words
    (zero-padded at the tail, as age's fixed-size keys need)."""
    acc = 0
    bits = 0
    words = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            words.append((acc >> bits) & maxv)
    if bits:
        words.append((acc << (tobits - bits)) & maxv)
    return words


def encode_bech32(hrp: str, data: bytes) -> str:
    """*data* as a bech32 string under the lowercase *hrp*."""
    words = convertbits(data, 8, 5)
    payload = words + bech32_checksum(hrp, words)
    return hrp + "1" + "".join(BECH32_CHARSET[w] for w in payload)


def mint_age_identity() -> str:
    """A fresh age-keygen-shaped identity file body: the X25519
    private key bech32-encoded as ``AGE-SECRET-KEY-1…`` with its
    recipient on a comment line (the shape ``age-keygen`` writes,
    so the file reads as a native age identity everywhere — the
    recipient line lets an operator re-key or back up without
    decrypting anything).
    """
    private = x25519.X25519PrivateKey.generate()
    scalar = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    recipient = encode_bech32(
        "age",
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
    )
    identity = encode_bech32("age-secret-key-", scalar).upper()
    return f"# public key: {recipient}\n{identity}\n"


def ensure_age_identity(path: Path) -> None:
    """Create the age identity at *path* when it does not exist.

    ``O_EXCL`` makes the create atomic against a concurrent
    caller: either this process created the file or another
    already did — two concurrent first operations can never mint
    two identities, a fate under which a value encrypted by one
    would never decrypt under the other. The parent joins the
    store root's 0700 posture. The mint is named in the log: a
    fresh state dir expects exactly one, and a reload that
    repointed the identity path at a typo names the fresh vault
    it just made instead of failing silently on the old values.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(mint_age_identity())
    LOG.warning(
        "minted a new age identity at %s — this file owns the agefile's "
        "decryption; if an existing vault was expected, restore the "
        "previous identity file (a value encrypted under another "
        "identity never decrypts)",
        path,
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
        written 0600 — the store root holds the encrypted agefile
        and the age identity, so its directory joins the house
        pattern of secret-bearing artifacts readable only by the
        daemon's user.
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
        """Probe the store: write, read back, delete.

        A green run means the agefile vault is reachable and
        writable before the first mint — a typo'd setting fails
        here, loudly, instead of at first use. The probe ref is
        unreachable by any minted placeholder (lowercase;
        backend_ref uppercases), the probe manifest is 0600 like
        the synced one (it carries the provider URI), and its
        value lands in — and is removed from — the agefile itself.
        A cleanup failure after a failed probe is suppressed so
        the ORIGINAL error is the one raised; a cleanup failure
        after a green probe propagates (residue is inert, but the
        operator should hear about it).
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
        return {"provider": "age", "ok": True}

    # --- legacy ref migration (#335) ---------------------------------

    def manifest_declares_legacy(self) -> bool:
        """Whether the on-disk manifest still declares a legacy
        ref — exactly what a pass that renamed every row but died
        before its final re-sync leaves behind (the re-sync is
        change-driven, so an unstale manifest is never rewritten).
        The check matches declaration keys, not substrings: a
        new-format ref can contain the legacy prefix inside its
        name (a workspace named ``msks`` mints
        ``MSKSWS_MSKS_...``). No configured store root means no
        manifest."""
        root = self.settings.root
        if root is None:
            return False
        try:
            text = (root / MANIFEST_NAME).read_text(encoding="utf-8")
        except OSError:
            return False
        return _LEGACY_DECLARATION.search(text) is not None

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
        resumes on the next boot. A row whose value the store
        cannot answer stays put and retries too — a transient
        outage heals itself, and a definitive one (a store that
        moved) is named in the log every boot until the operator
        re-mints or repoints.

        Returns the number of rows moved onto their new refs;
        rows that stayed are named in the log.
        """
        model = self.app.state.model
        legacy = await self.legacy_placeholder_rows()
        if not legacy and not self.manifest_declares_legacy():
            return 0
        await self.declare_transition_refs(model, legacy)
        moved = 0
        for row in legacy:
            if await self.migrate_legacy_row(row):
                moved += 1
        # The final manifest drops the legacy declarations of the
        # renamed rows: it is re-rendered off the rows alone — the
        # rows a failing copy left behind keep their declarations.
        self._cache.clear()
        refs = await model.placeholder_refs()
        await asyncio.to_thread(self.sync_manifest, refs)
        return moved

    async def declare_transition_refs(self, model, legacy) -> None:
        """Declare every legacy row's new ref beside the old one
        before any value copy: ``set`` answers 404 for refs the
        manifest does not declare, and the rows still carry the
        legacy names until each copy lands."""
        await asyncio.to_thread(
            self.sync_manifest,
            (await model.placeholder_refs())
            + [
                (
                    backend_ref(row["workspaces"], row["name"]),
                    f"{coverage_label(row['workspaces'])}/{row['name']}",
                )
                for row in legacy
            ],
        )

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

    async def migrate_legacy_row(self, row: dict) -> bool:
        """One legacy row: copy its value under the new ref, repoint
        the row, drop the old value (#335).

        Returns whether the row landed on its new ref. The order is
        the crash story: the copy lands before the row moves (a
        crash between them leaves both values present and the row
        legacy — the next boot simply redoes the copy), the row
        moves before the old value drops (a crash between them
        leaves an inert orphan under the legacy ref, named in the
        log when the drop fails). Every failure — a value the
        store cannot answer (a transient outage as much as a moved
        store: the CLI's errors do not separate them, so both
        retry), a failed copy, a failed row rename — logs and
        leaves the row on its legacy ref, where reads keep working
        and the next startup retries the move. A legacy row is
        single-workspace by construction (every pre-#335 row was),
        so the label names its one id.
        """
        model = self.app.state.model
        old = row["backend_ref"]
        label = coverage_label(row["workspaces"])
        new = backend_ref(row["workspaces"], row["name"])
        try:
            try:
                value = await self.read(old)
            except SecretStoreError as exc:
                LOG.warning(
                    "placeholder %s/%s has no readable value at "
                    "legacy ref %s (%s); the row stays put and the "
                    "next startup retries",
                    label,
                    row["name"],
                    old,
                    exc,
                )
                return False
            await self.write(new, value)
            await model.rename_placeholder_ref(row["id"], new)
            try:
                await self.delete(old)
            except SecretStoreError:
                LOG.warning(
                    "legacy value %s left behind in the store after "
                    "its copy under %s; the orphan is inert — remove "
                    "it by hand if it lingers",
                    old,
                    new,
                )
        except Exception:  # noqa: BLE001 - logged, never fatal
            LOG.exception(
                "placeholder %s/%s stays on legacy ref %s; "
                "the next startup retries it",
                label,
                row["name"],
                old,
            )
            return False
        return True
