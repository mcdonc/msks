"""The consent websocket wire: connect, guard, close.

The reconnect vocabulary (delays, states, close codes), the
registration frame, the one-flight guard a reconnect races, the
shared SSL context, and the dial the consent page consumes.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from ...egress import events_url
from ...env import env_token, env_url, is_unix, ssl_context
from ...wsauth import auth_headers
from ...wsdial import dial as ws_dial

#: Reconnect backoff (seconds), capped; a repeatedly-dropping daemon
#: must not spin the client.
RECONNECT_DELAYS = (1.0, 2.0, 5.0)

#: After an auth refusal (the daemon's 4401 close for a bad token)
#: the retry slows to this fixed interval: bounded noise, and the
#: client still heals if a corrected token lands mid-session.
REFUSED_RETRY_INTERVAL = 60.0

#: How long a flashed message owns the status line.

#: The close code the events websocket uses for a refused token.
AUTH_CLOSE_CODE = 4401

CONNECTED = "connected"

RECONNECTING = "reconnecting"

REFUSED = "refused — bad token?"

#: The registration-refusal and unusable-token states' labels — the
#: same strings :mod:`msks.client.tui.link` names its states by (the
#: link owns the connection; the surfaces render the labels, and an
#: import between the two modules would cycle).
REJECTED = "rejected"

UNUSABLE_TOKEN = "unusable token"

logger = logging.getLogger(__name__)


def registration_frame(workspace_id: str) -> str:
    """The decider-registration frame the handshake sends."""
    return json.dumps({"type": "egress.decider", "workspace": workspace_id})


#: A held request's countdown turns urgent below this many
#: seconds (#470 C7): a hold that lapses fails closed, so the
#: deadline reads in the warning color while it is still a
#: decision away.


def refused_close(exc: websockets.ConnectionClosed) -> bool:
    """Whether a close was the daemon's token refusal (4401)."""
    return exc.rcvd is not None and exc.rcvd.code == AUTH_CLOSE_CODE


def backoff(delays: tuple[float, ...], attempt: int) -> float:
    """The reconnect delay for an attempt: the ladder's first
    delay at the reset (attempt 0) and below it, capped at the
    last."""
    if not delays:
        return 0.0
    return delays[min(max(attempt - 1, 0), len(delays) - 1)]


class OneFlight:
    """A re-armable single-flight rebuild (#201), the one mechanism
    the rules screen, the audit view, and the queue share: one
    rebuild is in the air at a time, a request landing mid-flight
    re-arms, and the in-air flight loops to apply the newer state
    the moment it lands — never two concurrent rebuilds over one
    widget tree. ``rebuild`` and ``alive`` are read at flight time,
    so a test (or a subclass) swapping the callable mid-session is
    honored."""

    def __init__(self, rebuild, alive, label: str) -> None:
        self._rebuild = rebuild
        self._alive = alive
        self._label = label
        #: The flight's state, read by the owner's tests: armed, and
        #: re-armed by a mid-flight request.
        self.scheduled = False
        self.pending = False

    def request(self) -> None:
        """Arm one flight; a request mid-flight re-arms after it."""
        if self.scheduled:
            self.pending = True
            return
        self.scheduled = True
        # Referenced: an unreferenced task can be collected mid-await.
        self._task = asyncio.create_task(self._flight())

    async def _flight(self) -> None:
        rearm = False
        try:
            while self._alive():
                self.pending = False
                await self._rebuild()
                if not self.pending:
                    return
        except Exception:
            rearm = self.died_mid_flight()
        finally:
            self.scheduled = False
            if rearm:
                # Clear the in-air flag first so request() arms a
                # real flight; this block takes no await, so a
                # concurrent request cannot interleave (#322).
                self.pending = False
                self.request()

    def died_mid_flight(self) -> bool:
        """A rebuild died mid-flight: log it on a live owner (teardown
        unmounts the tree under a mid-swap flight — not a bug worth
        a traceback after exit), and say whether a request armed
        while the flight was dying must be carried: the loop honors
        ``pending`` only after a successful rebuild, and the audit
        view's unchanged-log path takes no rebuild from a later
        tick — a dropped request would leave its rows unmounted
        until the next frame landed."""
        if self._alive():
            logger.exception("%s rebuild failed", self._label)
            return self.pending
        return False


async def close_ws(ws) -> None:
    """Close the connection, swallowing a peer that already went."""
    try:
        await ws.close()
    except Exception:
        pass


# --- default seams (the real socket and REST calls) ---------------------

_SHARED_SSL: list = [None]


def shared_ssl():
    """The one TLS context for the app's lifetime: building it
    prints the TOFU warning when MSKSC_CAFILE is unset, and building
    it per verdict or per reconnect would spray that warning across
    the live screen. The local listener (#555) answers None — no
    TLS rides a unix socket, so no warning exists to print."""
    if is_unix(env_url()):
        return None
    if _SHARED_SSL[0] is None:
        _SHARED_SSL[0] = ssl_context()
    return _SHARED_SSL[0]


def ws_connect_kwargs(url: str, token: str, ssl_ctx) -> dict:
    """The events connection's kwargs (a plain-ws URL takes no ssl
    argument) — built on the shared context, not a fresh one. The
    token rides the handshake's Authorization header (#216)."""
    return {
        "uri": events_url(url),
        "additional_headers": auth_headers(token),
        "ssl": None if url.startswith("http://") else ssl_ctx,
        "max_size": 2**22,
    }


def default_ws_factory():
    """The events websocket connection (the decider's stream)."""
    url = env_url()
    return ws_dial(
        events_url(url),
        env_token(),
        None if is_unix(url) else shared_ssl(),
        max_size=2**22,
    )
