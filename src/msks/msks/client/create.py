"""The create command's domain: the identity modes and the POST.

Shared by the CLI's ``msks create`` (:mod:`msks.client.cli`) and
the workspace TUI's create form (#309) so both speak the daemon's
create surface the same way: the operator key is the CLI's
default (#336 — the operator's own ssh key, one key across
workspaces, public half only on the wire), an operator-supplied
line (#132) sends its line, the per-workspace client mint (#121,
``--key-type``) mints locally and sends the public half only, and
the daemon mint is the explicit opt-out. Every piece is quiet —
the callers own the operator surface (prints in the CLI, flashes
in the TUI).
"""

import asyncio
import getpass
import os
from contextlib import suppress
from pathlib import Path

from ..identity import LOGIN_NAME_RE, mint
from .agent import load_private
from .rest import api_client, request
from .ssh import (
    data_dir,
    derived_public,
    load_identity_file,
    operator_identity,
)

#: The mint rung's key type (#336): the same FIPS-approvable
#: default the per-workspace client mint and the daemon mint carry
#: (#115, #138) — the operator identity is minted by msks itself,
#: so it stays inside the mint's type set.
OPERATOR_KEY_TYPE = "ed25519"


async def identity_material(
    body: dict, key_type: str | None, pubkey: str | None
) -> tuple[str | None, str | None]:
    """Prepare the create's identity: ``(private_pem, public_line)``.

    The mint produces both halves; an operator-supplied line is the
    public half alone. Either way the public line rides the body as
    ``ssh_pubkey``; a daemon-mint create supplies neither and keeps
    the body free of key material.
    """
    if key_type is not None:
        private_pem, public = await asyncio.to_thread(mint, key_type)
    elif pubkey is None:
        return None, None
    else:
        private_pem, public = None, pubkey
    body["ssh_pubkey"] = public
    return private_pem, public


async def verify_no_escrow(client, workspace_id: str, public: str) -> None:
    """Confirm the daemon kept the no-escrow promise (#121).

    A daemon one version behind this client drops the unknown
    ``ssh_pubkey`` field (pydantic ignores extras) and silently mints
    its own pair — the create reports success while the daemon
    escrows a private half the operator was told does not exist.
    The key fetch answers for it: the served public line must carry
    the supplied key material and the private half must be null.
    """
    key = await request(
        client, "GET", f"/api/v1/workspaces/{workspace_id}/ssh-key"
    )
    served = key.get("public_key", "").split()[:2]
    if key.get("private_key") is not None or served != public.split()[:2]:
        raise SystemExit(
            f"msks: {workspace_id} was created, but the daemon did not "
            "keep the no-escrow promise: it holds its own minted "
            "identity for the workspace (a daemon older than this "
            "client's supplied-key support). The daemon's version of "
            "msks must be updated before creating without "
            "--daemon-mint; remove the escrowed workspace with: "
            f"msks rm {workspace_id}"
        )


def operator_pubkey() -> tuple[str, str]:
    """The bare create's identity (#336): ``(public line, note)``.

    One operator key becomes every workspace's key:
    ``identity_file`` / ``MSKSC_IDENTITY_FILE`` when set — the
    operator's explicit choice — else the key msks minted under
    the data root; when none exists there, one is minted (mode
    0600, once; every later create reuses it). The public half is
    derived from the private and rides the create body exactly as
    ``--pubkey``; the operator's private material is never copied
    anywhere — a resolved key's file stays where it lives, read in
    place each time.
    """
    resolved = operator_identity()
    if resolved is not None:
        pem, source = resolved
        return public_line(pem), f"identity: {source}"
    pem, path = mint_operator_identity()
    return public_line(pem), f"operator identity minted (mode 0600): {path}"


def public_line(pem: str) -> str:
    """The authorized_keys line a private half derives — algorithm
    and key body, no comment (the daemon annotates provenance its
    own way)."""
    return " ".join(derived_public(load_private(pem)))


#: The mint rung's refusal when its 0600 write cannot land — the
#: operator's way around an unusable data root is their own key.
OPERATOR_WRITE_REFUSAL = (
    "msks: the operator identity could not be written to {path}: {exc}\n"
    "msks cannot keep a key of its own there — point identity_file "
    "(or MSKSC_IDENTITY_FILE) at your own private key file, or fix "
    "the data root (MSKSC_DATA_DIR relocates it)"
)


def mint_operator_identity() -> tuple[str, Path]:
    """One fresh operator key, claimed at ``<data_dir>/identity``
    mode 0600 — the rung that fires when no operator key resolves
    anywhere.

    The claim is exclusive: two first-run creates that race both
    mint, one publish wins, and the loser reuses the winner's key
    — both workspaces then plant the same public half instead of
    one holding a private half the other just overwrote. The
    publish is atomic (written whole to a sibling temp file, then
    linked into place), so the path only ever holds a complete
    key: a loser that sees the path already there reads a usable
    key, never the winner's half-written file. The write happens
    before the create's POST on purpose: the key belongs to the
    operator, not to the workspace, so a refused create leaves it
    for the next create to reuse (rung 3), not an orphan tied to a
    workspace that never existed.
    """
    pem, _public = mint(OPERATOR_KEY_TYPE)
    path = data_dir() / "identity"
    try:
        publish_exclusive(path, pem)
    except FileExistsError:
        return claimed_identity(path, pem), path
    return pem, path


def claimed_identity(path: Path, pem: str) -> str:
    """The key in force at *path* when the exclusive publish lost
    its race: the concurrent winner's key (complete by
    construction), or *pem* after repairing content no intact
    publish could have left there (a torn file from an older
    version, external corruption)."""
    existing = load_identity_file(path)
    if existing is not None:
        return existing
    write_private_half(path, pem, OPERATOR_WRITE_REFUSAL)
    return pem


def publish_exclusive(path: Path, private_pem: str) -> None:
    """Publish the PEM at *path* mode 0600, atomically: written
    whole to a private temp sibling and linked into place, so the
    path never exists half-written. A file already there raises
    ``FileExistsError`` (the claim's own signal, re-raised for the
    caller); every other failure — the temp write included — is
    the operator's named refusal raised here."""
    temp = path.parent / f".{path.name}.{os.getpid()}"
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(private_pem)
            os.link(temp, path)
        finally:
            with suppress(OSError):
                temp.unlink(missing_ok=True)
    except FileExistsError:
        raise
    except OSError as exc:
        raise SystemExit(
            OPERATOR_WRITE_REFUSAL.format(path=path, exc=exc)
        ) from exc


def write_private_half(path: Path, private_pem: str, refusal: str) -> Path:
    """The 0600 private-half write shared by the two mints.

    The file is created 0600 from the first byte (open-write-chmod
    would leave a umask-window where the only private half is
    group-readable), the mode forced again on a pre-existing file.
    ``refusal`` is the one-line exit an unusable path raises
    (formatted with ``{path}`` and ``{exc}``) — each mint names its
    own recovery.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(private_pem)
    except OSError as exc:
        raise SystemExit(refusal.format(path=path, exc=exc)) from exc
    return path


def write_client_identity(workspace_id: str, private_pem: str) -> Path:
    """The client-minted private half, persisted mode 0600 (#121).

    The file lands under the data root (not the cache: this half
    must survive cache sweeps), keyed on the workspace's immutable
    id. No escrow cuts both ways: a failed write is loud — the
    workspace exists with the public half planted, and the private
    half exists nowhere on disk.
    """
    path = data_dir() / workspace_id / "identity"
    return write_private_half(
        path,
        private_pem,
        f"msks: {workspace_id} was created, but its client-minted "
        "identity could not be written to {{path}}: {{exc}}\n"
        "The private half now exists nowhere on disk — ssh cannot "
        "use this workspace's identity. Use the console, or delete "
        "and recreate the workspace",
    )


def invoking_user() -> str:
    """The create ``--user`` default (#248): the name of the user
    running msks — the workspace seeds this account as its own, so
    a bare create lands the operator's account, not a shared one.

    The name must fit the login-name charset (the same wire shape
    the guest accepts); a username that does not (a capitalized or
    accented one) is a named refusal pointing at ``--user``, not a
    cryptic daemon-side pattern error.
    """
    try:
        name = getpass.getuser()
    except OSError as exc:
        raise SystemExit(
            f"msks: cannot determine the invoking user's name ({exc}); "
            "pass --user <name>"
        ) from exc
    return checked_login_name(name, "your username")


def checked_login_name(name: str, what: str) -> str:
    """One login name the guest can carry, or the named refusal."""
    if LOGIN_NAME_RE.fullmatch(name) is None:
        raise SystemExit(
            f"msks: {what} ({name!r}) is not a valid login name — "
            "lowercase letters, digits, dashes, and underscores, "
            "starting with a lowercase letter or underscore, at most "
            "32 characters; pass --user <name>"
        )
    return name


async def create_workspace_core(
    url,
    token,
    body: dict,
    transport=None,
    ssl_ctx=None,
    key_type: str | None = None,
    pubkey: str | None = None,
    announce=None,
) -> tuple[dict, Path | None]:
    """POST one workspace with its identity handling: resolve the
    identity mode, send the create, keep the no-escrow promise
    checked, and persist a client-minted private half. Returns the
    daemon's row and the private half's path (``None`` when no
    private half was minted) — no prints and no boot; the callers
    own the operator surface.

    ``announce``, when given, is called with the daemon's row the
    moment the workspace exists (the CLI prints its created line
    there — a failed verification must not hide that the workspace
    was created; the TUI stays quiet and flashes its own line after
    the whole exchange lands).
    """
    private_pem, public = await identity_material(body, key_type, pubkey)
    async with api_client(url, token, transport, ssl_ctx) as client:
        row = await request(
            client, "POST", "/api/v1/workspaces", json_body=body
        )
        if announce is not None:
            announce(row)
        if public is not None:
            await verify_no_escrow(client, row["id"], public)
    path = None
    if private_pem is not None:
        path = write_client_identity(row["id"], private_pem)
    return row, path
