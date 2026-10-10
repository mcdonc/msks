"""The create command's domain: the identity source and the POST.

Shared by the CLI's ``msks create`` (:mod:`msks.client.cli`) and
the workspace TUI's create form (#309) so both speak the daemon's
create surface the same way: the operator's configured key
(``identity_file`` / ``MSKSC_IDENTITY_FILE``, #336) is the
identity (#486) — its derived public half rides the create body,
and the private material never crosses the wire and never moves
from where the operator keeps it. ``--pubkey`` supplies a one-off
public line instead (#132). msks generates no key, ever; a client
with no identity configured refuses the create (#486). Every
piece is quiet — the callers own the operator surface (prints in
the CLI, flashes in the TUI).
"""

import getpass

from ..identity import LOGIN_NAME_RE
from .agent import load_private
from .rest import api_client, request
from .ssh import derived_public, operator_identity

#: The refusal an unconfigured client raises at create (#486): the
#: operator names a key the client never generates.
IDENTITY_UNCONFIGURED = (
    "msks: no identity configured — msks never generates a key.\n"
    "Point identity_file (the client config, ~/.config/msks/"
    "msks.yaml) or MSKSC_IDENTITY_FILE at your private key file, "
    "or pass --pubkey at create"
)


def operator_pubkey() -> tuple[str, str]:
    """The create's identity (#336, #486): ``(public line, note)``.

    One operator key becomes every workspace's key: the file
    ``identity_file`` / ``MSKSC_IDENTITY_FILE`` names, read in
    place each time — the operator's private material is never
    copied anywhere. An unconfigured client refuses rather than
    generating (:data:`IDENTITY_UNCONFIGURED`).
    """
    resolved = operator_identity()
    if resolved is None:
        raise SystemExit(IDENTITY_UNCONFIGURED)
    pem, source = resolved
    return public_line(pem), f"identity: {source}"


def public_line(pem: str) -> str:
    """The authorized_keys line a private half derives — algorithm
    and key body, no comment (the daemon annotates provenance its
    own way)."""
    return " ".join(derived_public(load_private(pem)))


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
    pubkey: str | None = None,
    announce=None,
) -> dict:
    """POST one workspace with its identity: resolve the identity
    source, send the create. Returns the daemon's row — no prints
    and no boot; the callers own the operator surface.

    ``pubkey``, when given, is the one-off public line (#132);
    otherwise the operator's configured key supplies the half
    (:func:`operator_pubkey` — an unconfigured client refuses
    before any network roundtrip).

    ``announce``, when given, is called with the daemon's row the
    moment the workspace exists (the CLI prints its created line
    there; the TUI stays quiet and flashes its own line after the
    whole exchange lands).
    """
    if pubkey is None:
        pubkey, _note = operator_pubkey()
    body["ssh_pubkey"] = pubkey
    async with api_client(url, token, transport, ssl_ctx) as client:
        row = await request(
            client, "POST", "/api/v1/workspaces", json_body=body
        )
        if announce is not None:
            announce(row)
    return row
