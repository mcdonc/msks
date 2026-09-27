"""The interceptor CA's client half (#392): the trust line the
workspace page paints, the console-channel install that establishes
the trust, and the hand-run recipe the page prints.

The guest's trust store is unreadable from the host, so the state
is the operator's confirmation: the page names the CA untrusted
until an install lands, and the install action is the confirmation
— it writes the CA into the running guest through the console
channel (the same session surface ``msks console`` opens, as root,
scripted rather than handed to the operator) and flips the line on
success. A marker file under the client data root carries the
record beside the client-minted identity, and the record names the
CA it trusts — the marker holds the PEM's digest, so a re-minted
CA (a daemon state restored, the cert file deleted) reads
untrusted again instead of lying. When [#200] seeds the trust at
create, the page reports trusted from its seed and the install
action goes.

The install is one shell line: the CA's PEM travels base64-encoded
(coreutils' ``base64`` decodes it in the guest) into
``/usr/local/share/ca-certificates/``, and the guest's
``update-ca-certificates`` folds it into the system store. The
marker the session waits for is quoted inside the echoed command,
so the pty's own echo of the line — which arrives before the shell
runs anything — can never satisfy it.
"""

import asyncio
import base64
import hashlib
from pathlib import Path

import websockets

from .console import dial, report_close, ws_url
from .consoleauth import auth_exchange
from .env import env_token, env_url
from .ssh import data_dir

#: The CA's guest-side destination — the docs recipe's own path
#: (``update-ca-certificates`` folds every ``.crt`` under
#: ``/usr/local/share/ca-certificates/`` into the system bundle).
CA_DEST = "/usr/local/share/ca-certificates/msks-ws.crt"

#: The CA file's name inside the workspace's state directory —
#: the recipe's fallback path when a daemon reply carried none.
CA_CERT_NAME = "interceptor-ca.crt"

#: The trust marker's file name under the client data root.
TRUSTED_FILE = "interceptor-ca.trusted"

#: The install's whole window — the connect, the challenge answer
#: when the guest serves one, and the guest's own
#: ``update-ca-certificates`` run: one deadline bounds every read
#: in it, so a chatty console cannot stretch the wait without end.
INSTALL_TIMEOUT_S = 60.0

#: The success marker the guest prints once the install lands.
MARKER = b"MSKS_CA_INSTALLED"


def trusted_path(workspace_id: str) -> Path:
    """The trust marker's path under the client data root."""
    return data_dir() / workspace_id / TRUSTED_FILE


def pem_digest(ca_pem: str) -> str:
    """The PEM's sha256 — the marker's content, so the record
    names the CA it trusts."""
    return hashlib.sha256(ca_pem.encode()).hexdigest()


def recorded_digest(workspace_id: str) -> str | None:
    """The digest this client recorded for the workspace's CA, or
    None when no install landed here."""
    try:
        return trusted_path(workspace_id).read_text().strip() or None
    except OSError:
        return None


def ca_trusted(workspace_id: str, ca_pem: str | None = None) -> bool:
    """Whether this client recorded the workspace's CA as trusted.
    With the CA's PEM beside it (the page's copy of the daemon's
    answer), the record must name that CA — a re-minted CA reads
    untrusted again. With none, the record stands alone: this
    client installed some CA of the workspace's, which is all the
    page knows before its first fetch."""
    recorded = recorded_digest(workspace_id)
    if recorded is None:
        return False
    return recorded == pem_digest(ca_pem) if ca_pem else True


def mark_ca_trusted(workspace_id: str, ca_pem: str) -> None:
    """Record the install's confirmation: the installed PEM's
    digest under the workspace's own directory in the client data
    root."""
    path = trusted_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pem_digest(ca_pem)}\n")


def ca_line(workspace_id: str, ca_pem: str | None = None) -> str:
    """The workspace page's trust line (#392): trusted once an
    install landed, untrusted — with the symptom it names — until
    then, so the failure the operator sees on the wire reads as
    the trust it is instead of a placeholder that does not swap."""
    if ca_trusted(workspace_id, ca_pem):
        return "interceptor CA: trusted"
    return (
        "interceptor CA: untrusted — HTTPS toward allowlisted "
        "destinations fails validation"
    )


def install_command(ca_pem: str) -> str:
    """The one shell line the install runs in the guest: decode the
    CA into the store directory and fold it into the system bundle.
    The marker rides quoted — the pty echoes the command before the
    shell runs it, and the quoted form cannot match the unquoted
    marker the run prints, so the wait cannot end on the echo."""
    blob = base64.b64encode(ca_pem.encode()).decode()
    return (
        f"echo {blob} | base64 -d > {CA_DEST}"
        ' && update-ca-certificates && echo MSKS_CA_"INSTALLED"'
    )


async def timed_recv(ws, deadline: float):
    """One read bounded by the install's single deadline: the
    frame, or a ``TimeoutError`` once the window has lapsed — the
    budget is not reset per read, so output the guest keeps
    printing cannot stretch the wait without end."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(ws.recv(), remaining)


async def wait_for_marker(ws) -> None:
    """Read the session's output until the success marker lands;
    :data:`INSTALL_TIMEOUT_S` bounds the whole wait."""
    deadline = asyncio.get_running_loop().time() + INSTALL_TIMEOUT_S
    seen = bytearray()
    try:
        while MARKER not in seen:
            chunk = await timed_recv(ws, deadline)
            if isinstance(chunk, str):
                chunk = chunk.encode()
            seen += chunk
    except TimeoutError:
        raise SystemExit(
            "msks: the guest did not finish the CA install within "
            f"{INSTALL_TIMEOUT_S:.0f}s — the console session may never "
            "have reached a shell"
        ) from None
    except websockets.ConnectionClosed as closed:
        report_close(closed)
        raise SystemExit(
            "msks: the console session ended before the install finished"
        ) from None


async def install_ca(
    workspace_id: str, ca_pem: str, ssl_ctx, url: str | None = None
) -> None:
    """Install the CA into the running guest through the console
    channel (#392): one scripted root session — dial, answer the
    console challenge when the guest serves one, send the install
    line, wait for the marker. Every failure exits with one
    readable line (the console's close-code table names the
    daemon's own refusals — a stopped workspace among them)."""
    url = url or env_url()
    token = env_token()
    address = ws_url(url, workspace_id, user="root")
    ws = await dial(address, token, ssl_ctx, url)
    async with ws:
        lead = await auth_exchange(ws, workspace_id, url, token, ssl_ctx)
        if lead.startswith(b"MSKS ERR "):
            raise SystemExit(
                f"msks: {workspace_id} refused the console session "
                f"({lead.decode(errors='replace').strip()}) — the guest's "
                "console trust store is broken"
            )
        await ws.send(install_command(ca_pem).encode() + b"\n")
        await wait_for_marker(ws)


#: One recipe chunk's base64 budget: a whole shell line that fits
#: the recipe panel's 64 columns without wrapping, so a copy of
#: any line is a whole command however the terminal pastes.
RECIPE_CHUNK = 28

#: The recipe's guest-side scratch for the assembled blob.
RECIPE_SCRATCH = "/tmp/msks-ca.b64"

#: The recipe's decoded staging file (the store directory's own
#: path is too long for one panel-width line beside a redirect).
RECIPE_STAGING = "/tmp/msks-ws.crt"


def recipe_commands(ca_pem: str) -> list[str]:
    """The hand-run install as short whole shell lines: the blob
    arrives in chunks — each line its own complete command, so a
    copy that carries the line breaks pastes whole commands — and
    the decode lands the CA in the store directory."""
    blob = base64.b64encode(ca_pem.encode()).decode()
    lines = [
        f"printf %s {blob[:RECIPE_CHUNK]} > {RECIPE_SCRATCH}",
    ]
    for start in range(RECIPE_CHUNK, len(blob), RECIPE_CHUNK):
        lines.append(
            f"printf %s {blob[start : start + RECIPE_CHUNK]}"
            f" >> {RECIPE_SCRATCH}"
        )
    lines += [
        f"base64 -d {RECIPE_SCRATCH} > {RECIPE_STAGING}",
        f"cp {RECIPE_STAGING} {CA_DEST}",
        "update-ca-certificates",
    ]
    return lines


def recipe_lines(info: dict, workspace_id: str) -> list[str]:
    """The hand-run recipe's lines (#392): the CA's daemon-side path
    (the real state-dir file the daemon served the bytes from) and
    the guest-side commands that install it — the same install the
    console channel runs, spelled for a shell the operator already
    has open."""
    path = info.get("path") or (
        f"<state_dir>/vms/{workspace_id}/{CA_CERT_NAME}"
    )
    lines = [
        "The workspace's interceptor CA is daemon-side state:",
        f"  {path}",
        "",
        "In the guest, from a root shell (msks ssh -l root, or the",
        "console) — paste the lines whole:",
        *(f"  {command}" for command in recipe_commands(info["ca_pem"])),
        "",
        "After a hand install, the page's install action marks the line",
        "trusted — it writes the same CA again and records the trust.",
    ]
    return lines
