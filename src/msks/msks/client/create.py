"""The create command's domain: the identity modes and the POST.

Shared by the CLI's ``msks create`` (:mod:`msks.client.cli`) and
the workspace TUI's create form (#309) so both speak the daemon's
create surface the same way: the client mint (#121) mints locally
and sends the public half only, an operator-supplied key (#132)
sends its line, and the daemon mint is the explicit opt-out. Every
piece is quiet — the callers own the operator surface (prints in
the CLI, flashes in the TUI).
"""

import asyncio
import getpass
import os
from pathlib import Path

from ..identity import LOGIN_NAME_RE, mint
from .rest import api_client, request
from .ssh import data_dir


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


def write_client_identity(workspace_id: str, private_pem: str) -> Path:
    """The client-minted private half, persisted mode 0600 (#121).

    The file is created 0600 from the first byte (open-write-chmod
    would leave a umask-window where the workspace's only private
    half is group-readable), the mode forced again on a pre-existing
    file, under the data root (not the cache: this half must survive
    cache sweeps). No escrow cuts both ways: a failed write is loud —
    the workspace exists with the public half planted, and the
    private half exists nowhere on disk.
    """
    root = data_dir() / workspace_id
    path = root / "identity"
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(private_pem)
    except OSError as exc:
        raise SystemExit(
            f"msks: {workspace_id} was created, but its client-minted "
            f"identity could not be written to {path}: {exc}\n"
            "The private half now exists nowhere on disk — ssh cannot "
            "use this workspace's identity. Use the console, or delete "
            "and recreate the workspace"
        ) from exc
    return path


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
