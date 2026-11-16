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
import json
import logging

from ..egress import env_dial_events

logger = logging.getLogger(__name__)

#: Reconnect backoff (seconds), capped — the TUI's wire ladder
#: (#69): a repeatedly-dropping daemon must not spin the client,
#: and a daemon that comes back heals inside five seconds.
RECONNECT_DELAYS = (1.0, 2.0, 5.0)


def backoff(delays: tuple[float, ...], attempt: int) -> float:
    """The reconnect delay for an attempt: the ladder's first
    delay at the reset (attempt 0) and below it, capped at the
    last."""
    if not delays:
        return 0.0
    step = min(max(attempt - 1, 0), len(delays) - 1)
    return delays[step]


def decode_frame(raw: str) -> dict | None:
    """One wire text as the frame dict the screens read, or
    ``None`` for junk — a line that is not JSON, or JSON that is
    not an object. The hub publishes objects only; anything else
    is a peer gone wrong, and the feed skips it rather than
    dying on it."""
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


class EventsFeed:
    """The subscription's whole life: dial, receive, deliver —
    reconnect until the task is cancelled. ``on_frame`` takes
    each decoded frame, ``on_drop`` the reason each connection
    ends or dial fails, ``on_reconnect`` the moment a connection
    serves after a drop; the bridge wires all three to signals.
    A drop that never connected tells once per attempt — the
    outage's own heartbeat, a flash while it lasts (#946,
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
        self.dropped = False

    async def run(self) -> None:
        """Connect and serve until cancelled, the ladder between
        every attempt: reset by a connection that served, climbed
        by one that never reached its serve loop."""
        attempt = 0
        while True:
            served = await self.pump_one()
            attempt = 0 if served else attempt + 1
            await asyncio.sleep(backoff(self.reconnect_delays, attempt))

    async def pump_one(self) -> bool:
        """One connection's lifetime; True when it reached its
        serve loop (the ladder resets on the next attempt), False
        when the dial itself failed (the ladder climbs). Every
        end — dial failure, wire error, the daemon's clean close
        — tells the drop with its reason."""
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
            return False
        try:
            await self.serve(ws)
        except Exception as exc:
            self.note_drop(f"the socket broke: {exc}")
            return True
        finally:
            await self.close_quietly(ws)
        self.note_drop("the daemon closed the events socket")
        return True

    async def serve(self, ws) -> None:
        """Receive until the stream ends: a connection that got
        this far resets the ladder and, after a drop, tells the
        reconnect. Every decoded frame crosses to ``on_frame``;
        junk is skipped, logged, not fatal."""
        self.note_connection()
        async for raw in ws:
            self.land(raw)

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

    async def close_quietly(self, ws) -> None:
        """The socket closed, swallowing a peer that already
        went."""
        try:
            await ws.close()
        except Exception:
            pass
