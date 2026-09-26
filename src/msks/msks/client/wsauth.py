"""The websocket handshake's authentication (#116).

Every msks websocket carries its bearer token in the
Sec-WebSocket-Protocol offer — ``["bearer", <token>]`` — and the
daemon's accept selects ``bearer`` back. A URL query string would
land the token in access logs, proxy logs, browser history, and
process listings; a handshake header does not, and it is the one
mechanism a browser's ``new WebSocket()`` can send as well. The
REST surface keeps its ``Authorization: Bearer`` header unchanged.
"""

import asyncio
import contextlib
import re

import websockets

#: The subprotocol name the offer leads with and the daemon echoes
#: on a successful handshake.
BEARER = "bearer"

#: The close code for a token the daemon does not hold — the shared
#: contract of every msks websocket surface.
CLOSE_AUTH_FAILED = 4401

#: The one-line label for that close — the message every surface's
#: close-code table gives it, owned here so the copies cannot drift.
AUTH_FAILED_MESSAGE = "authentication failed (bad token?)"

#: The HTTP ``token`` grammar (RFC 9110): the charset the
#: Sec-WebSocket-Protocol value carries. The daemon mints inside it
#: (#116); a client-held token outside it (a padded seed, a stray
#: space in the environment) cannot ride the handshake at all.
TCHAR_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")

#: How long the failed-echo check waits for the daemon's refusal
#: close before naming the handshake itself as the problem.
ECHO_WAIT_S = 5.0

#: The refusal for a token the handshake cannot carry. The message
#: names the credential's problem without echoing the credential:
#: the websocket library's own validation error embeds the token
#: whole, and a traceback would print it.
UNUSABLE_MESSAGE = (
    "the token cannot ride the websocket handshake (characters "
    "outside the Sec-WebSocket-Protocol grammar) — check MSKSC_TOKEN "
    "or the client token file"
)


class UnusableToken(Exception):
    """A token outside the handshake's value grammar (#116): it
    cannot ride Sec-WebSocket-Protocol at all, so no retry can
    help."""


def subprotocols(token: str) -> list[str]:
    """The Sec-WebSocket-Protocol offer that carries the token.

    Refused here — message intact, token unechoed — when the token
    cannot fit the handshake's value grammar: the websocket library
    would otherwise reject the connect call with the token embedded
    in its error.
    """
    if not TCHAR_RE.fullmatch(token):
        raise UnusableToken(UNUSABLE_MESSAGE)
    return [BEARER, token]


def echoed(ws) -> bool:
    """Whether the handshake selected the bearer subprotocol.

    A handshake that completes without the selection leaves the
    session unauthenticated — the caller must not pump frames
    through such a connection.
    """
    return getattr(ws, "subprotocol", None) == BEARER


async def require_echo(ws, wait_s: float = ECHO_WAIT_S) -> None:
    """Abort unless the daemon selected the bearer subprotocol.

    The daemon selects it only for a token it holds; otherwise it
    closes 4401, and that close is awaited here so the operator
    sees the real refusal. A connection that stays open with no
    selection means something else answered or rewrote the
    handshake — a proxy stripping Sec-WebSocket-Protocol — and is
    named and exited, never pumped.
    """
    if echoed(ws):
        return
    try:
        await asyncio.wait_for(ws.recv(), wait_s)
    except websockets.ConnectionClosed as closed:
        raise echo_refusal(closed) from None
    except TimeoutError:
        pass
    with contextlib.suppress(Exception):
        await ws.close()
    raise SystemExit(
        "msks: the daemon did not echo the websocket auth protocol — "
        "a proxy may be stripping Sec-WebSocket-Protocol"
    )


def echo_refusal(closed: websockets.ConnectionClosed) -> SystemExit:
    """The exit for a daemon close behind a missing echo: the 4401
    refusal verbatim (the message every surface's close-code table
    gives it), any other close named with its code."""
    code = closed.rcvd.code if closed.rcvd is not None else None
    if code == CLOSE_AUTH_FAILED:
        return SystemExit(f"msks: {AUTH_FAILED_MESSAGE}")
    return SystemExit(
        "msks: the daemon closed the websocket before "
        f"authenticating (code {code})"
    )
