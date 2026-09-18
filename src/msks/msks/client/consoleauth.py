"""The client half of the console challenge (#123).

After the daemon's prelude, a guest whose seed planted the
allowed_signers trust store demands a signature over a fresh nonce
before any shell. The daemon relays the exchange; it cannot answer.
This module resolves the workspace's key the way ``msks ssh`` does
and produces the SSHSIG the guest's ssh-keygen will verify:

- the daemon-mint escrow (fetched over the API) or the client data
  root's file sign in-process;
- a key held by the operator's ssh-agent (``SSH_AUTH_SOCK``, a
  hardware key included) signs through the agent protocol — msks
  never reads the private half.

A guest without the trust store sends no challenge; the first bytes
are the shell's, and the caller keeps pumping them.
"""

import asyncio

from . import ssh, sshsig
from .rest import fetch_ssh_key

#: The challenge's arrival window from websocket connect. The daemon
#: accepts the websocket first and then does its own vsock connect
#: and prelude — up to its ``vsock_wait_timeout_s`` (15s) while a
#: freshly-booted guest reaches the console service — so the window
#: must cover that budget with margin: the first relayed byte
#: (challenge or shell banner) cannot arrive before the daemon's
#: own wait ends.
CHALLENGE_WINDOW_S = 20.0

CHALLENGE_PREFIX = b"AUTH CHALLENGE "
SIG_PREFIX = b"AUTH SIG "
OK_PREFIX = b"AUTH OK"
REFUSED = b"MSKS ERR auth"


def console_signer(private_pem: str):
    """A nonce → SSHSIG body signer over the msks-held half."""

    def sign(nonce: bytes) -> str:
        return sshsig.sign_payload(private_pem, nonce, sshsig.NAMESPACE)

    return sign


def agent_signer(socket_path: str, public_line: str):
    """A nonce → SSHSIG body signer through the operator's agent.
    The signature is one line, not a traceback, when the named
    socket names no live agent."""

    def sign(nonce: bytes) -> str:
        try:
            return sshsig.sign_via_agent(
                socket_path, public_line, nonce, sshsig.NAMESPACE
            )
        except OSError as exc:
            raise SystemExit(
                f"msks console: the agent named by SSH_AUTH_SOCK "
                f"({socket_path}) is not reachable: {exc}\n"
                "Start the agent (ssh-add's parent), or connect from "
                "the client that holds the workspace's private half."
            ) from exc

    return sign


def signer_for_key(key: dict, workspace_id: str):
    """The workspace's console signer from its key record, by where
    the private half lives.

    The daemon-mint escrow and the client data root sign in-process;
    the operator's agent (when the environment names one) is
    consulted for a key neither holds. A key nowhere on this client
    is one readable line, not a traceback.
    """
    public = key["public_key"]
    if key["private_key"] is not None:
        return console_signer(key["private_key"]), public
    try:
        return console_signer(ssh.resolve_private(key, workspace_id)), public
    except SystemExit:
        agent_path = sshsig.environment_agent()
        if agent_path is None:
            raise
        return agent_signer(agent_path, public), public


async def workspace_signer(
    url: str, token: str, workspace_id: str, ssl_ctx=None
):
    """The workspace's console signer, fetched over the API."""
    key = await fetch_ssh_key(url, token, workspace_id, ssl_ctx=ssl_ctx)
    return signer_for_key(key, workspace_id)


async def auth_exchange(
    ws, workspace_id: str, url: str, token: str, ssl_ctx=None
):
    """One console connection's auth: answer a challenge, pass a
    pre-#123 guest through. Returns the bytes already read that are
    not the protocol's (the shell's first output), for the caller to
    play before pumping.
    """
    first = await peek_first(ws)
    if first is None:
        return b""  # quiet behind the window: the pump will speak
    if not isinstance(first, bytes):
        return first  # a text frame: the caller normalizes it
    line, is_challenge = await challenge_or_shell(ws, first)
    if not is_challenge:
        return line  # the shell's own first bytes
    body = line[len(CHALLENGE_PREFIX) :].strip()
    try:
        nonce = bytes.fromhex(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(
            "msks console: the guest served a malformed challenge "
            f"({body!r}); the console refuses to guess at it."
        ) from exc
    signer, _public = await workspace_signer(url, token, workspace_id, ssl_ctx)
    return await send_signature(ws, workspace_id, signer, nonce)


async def peek_first(ws) -> bytes | None:
    """The connection's first message, or None when the challenge
    window passes quietly (a pre-#123 guest behind a slow daemon
    connect alike; the pump keeps waiting either way)."""
    try:
        return await asyncio.wait_for(ws.recv(), CHALLENGE_WINDOW_S)
    except TimeoutError:
        return None


async def challenge_or_shell(ws, first: bytes) -> tuple[bytes, bool]:
    """Decide what the first relayed bytes are: a whole challenge
    line, or the shell's own output.

    The prefix can arrive split across the relay's reads, so
    anything that could still grow into the prefix keeps reading;
    bytes that diverge from it are the shell's, returned whole with
    everything that followed them. A text frame reads as the
    shell's first output: the caller normalizes it for the tty.
    """
    seen = first
    while True:
        if seen.startswith(CHALLENGE_PREFIX):
            if b"\n" in seen:
                line, _, _rest = seen.partition(b"\n")
                return line, True
            # the challenge line is still arriving
        elif CHALLENGE_PREFIX.startswith(seen):
            pass  # still undecidable: a prefix-sized first read
        else:
            return seen, False
        seen += await ws.recv()


async def send_signature(ws, workspace_id: str, signer, nonce: bytes) -> bytes:
    """Send the signature and read the verdict; the bytes after the
    protocol's line are the shell's first output."""
    await ws.send(SIG_PREFIX + signer(nonce).encode() + b"\n")
    reply = b""
    while b"\n" not in reply:
        reply += await ws.recv()
        if REFUSED in reply:
            raise SystemExit(
                f"msks console: {workspace_id} refused the console "
                "signature — the workspace's key did not sign this "
                "challenge. The console still opens from the client "
                "that holds the current key."
            )
    verdict, _, rest = reply.partition(b"\n")
    if not verdict.startswith(OK_PREFIX):
        raise SystemExit(f"msks console: unexpected auth reply: {verdict!r}")
    return rest
