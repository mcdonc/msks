"""The GUI's live events feed (phase 4 of #924, #946): one
websocket subscription to the daemon's event channel
(:mod:`msks.events`'s hub behind ``/api/v1/events``), running as
a task on the bridge's loop — the same channel and frame JSON
the TUI's decider link consumes, as a plain subscriber (the
TUI's audit link's own shape: no registration frame, every hub
broadcast delivered).

The module holds no Qt: :class:`EventsFeed` is plain asyncio
over the shared dial seams, its callbacks the bridge wires to
signals — so the suite drives a feed against a fake socket with
no binding and no daemon, and the coverage gate lands on it
whole. The reconnect ladder is the TUI wire's own (#69's
decider): backoff on every drop, the ladder resetting on every
connection that reached its serve loop.
"""

import asyncio
import logging

import websockets

from ..egress import env_dial_events
from ..egress import refused as refused_close
from ..frames import decode_frame

logger = logging.getLogger(__name__)

#: Reconnect backoff (seconds), capped — the TUI's wire ladder
#: (#69): a repeatedly-dropping daemon must not spin the client,
#: and a daemon that comes back heals inside five seconds.
RECONNECT_DELAYS = (1.0, 2.0, 5.0)

#: After an auth-refused close (#216 — :func:`refused_close`
#: reads the code), seconds between retries — the TUI wire's
#: own interval: bounded noise on a refused token, and a
#: corrected token still heals the feed mid-session.
REFUSED_RETRY_S = 60.0

#: The socket close's bound, seconds: a live peer answers a
#: close handshake in milliseconds; a dead one is cut here, on
#: the reconnect path and the shutdown's unwind both.
CLOSE_TIMEOUT_S = 3.0


async def close_quietly(ws) -> None:
    """The socket closed, bounded and best-effort: a peer that
    already went raises, and one that cannot answer is cut at
    the bound — the reconnect moves on, and the shutdown's
    unwind cannot outlast the bridge's join. The decider link
    (#999) closes its own sockets through the same door: one
    close contract for both of the client's events
    connections."""
    try:
        await asyncio.wait_for(ws.close(), timeout=CLOSE_TIMEOUT_S)
    except Exception as exc:
        # The close stays best-effort — a socket the wire
        # already broke is the reconnect path's own case — but
        # the failure is named in the log, not swallowed
        # (AGENTS.md's exception rule: no silent except-and-
        # pass). Debug level: the broken peer is routine on
        # every reconnect after a drop, and the record is
        # there when asked for.
        logger.debug("the events socket's close failed: %s", exc)


def backoff(delays: tuple[float, ...], attempt: int) -> float:
    """The reconnect delay for an attempt: the ladder's first
    delay at the reset (attempt 0) and below it, capped at the
    last."""
    if not delays:
        return 0.0
    step = min(max(attempt - 1, 0), len(delays) - 1)
    return delays[step]


class EventsFeed:
    """The subscription's whole life: dial, receive, deliver —
    reconnect until the task is cancelled. ``on_frame`` takes
    each decoded frame, ``on_drop`` the reason each connection
    ends or dial fails, ``on_reconnect`` the moment a connection
    serves a frame after a drop, and ``on_serve`` every
    connection that reaches its serve loop (#1019, #1129) — the
    poke the screens re-read on, frame or no frame, first
    connection included; the bridge wires all four to signals.
    A drop that never connected tells once per attempt
    — the outage's own heartbeat, a flash while it lasts (#946,
    AGENTS.md's exception rule: never a silent ``None``)."""

    def __init__(
        self,
        ws_factory=None,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
    ) -> None:
        self.ws_factory = ws_factory or env_dial_events
        self.reconnect_delays = reconnect_delays
        self.on_frame = None
        self.on_drop = None
        self.on_reconnect = None
        self.on_serve = None
        self.dropped = False

    async def run(self) -> None:
        """Connect and serve until cancelled, the ladder between
        every attempt: reset by a connection that served, climbed
        by one that never reached its serve loop. An auth-refused
        close takes the fixed slow interval instead — a token the
        daemon does not hold cannot become valid by
        reconnecting. A daemon that accepts and then closes
        cleanly without a frame resets the ladder like any
        served connection, and each such connection costs the
        screens one re-read (#1129) — msksd itself closes an
        events socket only when the client does or its relay
        fails."""
        attempt = 0
        while True:
            served, refused = await self.pump_one()
            if refused:
                await asyncio.sleep(REFUSED_RETRY_S)
                continue
            attempt = 0 if served else attempt + 1
            await asyncio.sleep(backoff(self.reconnect_delays, attempt))

    async def pump_one(self) -> tuple[bool, bool]:
        """One connection's lifetime; ``(served, refused)`` —
        whether it reached its serve loop (the ladder resets on
        the next attempt), and whether the daemon's close was
        its token refusal (the fixed slow interval instead).
        Every end — dial failure, wire error, the daemon's clean
        close — tells the drop with its reason."""
        try:
            ws = await self.ws_factory().__aenter__()
        except (SystemExit, Exception) as exc:
            # SystemExit is the environment's own refusal (a
            # token the config will not give): the drop tells
            # it like any failed dial — the screens flash the
            # same refusal on their next refresh — and the next
            # attempt re-reads the environment, so a fixed
            # config heals the feed without a restart. The arm
            # also keeps the exit contract from killing the task
            # silently (AGENTS.md's exception rule).
            self.note_drop(f"the dial failed: {exc}")
            return False, False
        try:
            await self.serve(ws)
        except websockets.ConnectionClosed as exc:
            refused = refused_close(exc)
            self.note_drop(
                "the daemon refused the token"
                if refused
                else f"the socket broke: {exc}"
            )
            return True, refused
        except Exception as exc:
            # An on_frame callback's own raise lands here too
            # and reads as the socket's break — a lie about the
            # source, but the reconnect heals it; Qt's emit does
            # not raise, so nothing real takes this arm for the
            # callback.
            self.note_drop(f"the socket broke: {exc}")
            return True, False
        finally:
            await close_quietly(ws)
        self.note_drop("the daemon closed the events socket")
        return True, False

    async def serve(self, ws) -> None:
        """Receive until the stream ends: the connection tells
        the serve at the loop's entry, before any frame (#1019,
        #1129) — the screens' re-read rides the connection, not
        its first frame — and every decoded frame crosses to
        ``on_frame``; junk is skipped, logged, not fatal. The
        reconnect tell stays on the first frame: frames flowing
        again is what it reports."""
        self.note_serve()
        async for raw in ws:
            self.note_connection()
            self.land(raw)

    def note_serve(self) -> None:
        """A connection that reached its serve loop, told once
        per connection (#1019, #1129) — :meth:`serve` calls it
        at its entry and nowhere else: the serve poke the
        screens re-read on. Frames published while no connection
        stood never landed, so a feed that comes to serve hands
        its hosts a re-read, first connection included (the
        opening read and this re-read bracket the startup race:
        a frame published between the REST answer and the
        server-side subscribe misses the socket but shows in the
        re-read, whose REST answer postdates the subscribe: the
        server subscribes before its accept answer reaches the
        client). The poke fires before any frame because an idle
        daemon publishes none: a window opened against a daemon
        that was down gets its listing re-read when the dial
        finally connects, the TUI link's own moment. A connection
        the daemon refuses after the accept (#216) pokes once
        too: the re-read's own refusal is the honest answer. The
        reconnect flash beside it is :meth:`note_connection`'s
        own tell."""
        if self.on_serve is not None:
            self.on_serve()

    def land(self, raw: str) -> None:
        """One wire text delivered: decoded, junk skipped, the
        frames the screens read crossing to the callback."""
        frame = decode_frame(raw)
        if frame is None:
            logger.warning("events feed: a frame did not decode")
            return
        if self.on_frame is not None:
            self.on_frame(frame)

    def note_connection(self) -> None:
        """A connection serving: after a drop it is a reconnect —
        the bridge's recovery flash — and the standing drop
        clears. The first connection of a feed's life tells
        nothing: there is no outage to report recovered."""
        if not self.dropped:
            return
        self.dropped = False
        if self.on_reconnect is not None:
            self.on_reconnect()

    def note_drop(self, reason: str) -> None:
        """A connection's end or a dial's failure, told with its
        reason: the bridge flashes the current screen while the
        feed retries — each attempt re-tells, the outage's own
        heartbeat while it lasts."""
        self.dropped = True
        if self.on_drop is not None:
            self.on_drop(reason)
