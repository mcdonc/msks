"""The WSS event channel hub (#8).

One pub/sub hub behind ``GET /api/v1/events``: the status watcher
publishes workspace lifecycle transitions, every connected client
socket receives them. Same listener/port as HTTPS — one surface.

The subscription carries the subscriber's owner context (#1090):
a socket registered for one uid receives the owner-scoped frames
its uid owns beside every unscoped one, while a socket registered
without a uid — the bypass shape #1082's rule resolves to before
the subscribe (root, the daemon's own uid) — receives everything.
The audience lands on the publish: a frame that names its owning
uid reaches that owner and the unscoped subscribers alone, and
every other frame broadcasts. The scoping is the hub's own
shape, not a mask the relay applies: the payload a subscriber
never sees never spends a slot in its queue.

Every publish also writes one INFO line (#762): the daemon's
journal is the record of events, visible after the subscribers
are gone. The line is the whole frame, the same JSON the
subscribers receive. The frames carry identifiers, names,
hosts, ports, paths, and counts — a secret's value and a
sentinel never ride them, a property the suite pins per
publisher family (mint and unlock carry the plaintext in hand
one refactor away), so the record needs no redaction. A frame
no subscriber receives still journals — the record is the
publish, and the audience filter is a delivery concern.

A root leaf module (#408): the composition layer (``app.py``) and
the HTTP surface (``server``) both import it, so the package graph
between them stays one-way. The consent engine and interceptor
publish through ``app.state.hub`` at runtime without importing
this module; the watcher imports only the ``EventHub`` type.
"""

import asyncio
import contextlib
import json
import logging

logger = logging.getLogger(__name__)


class EventHub:
    """Fan-out to any number of websocket subscribers, each
    carrying the uid its owner-scoped frames answer to (#1090)."""

    def __init__(self) -> None:
        self._viewers: dict[asyncio.Queue, int | None] = {}
        # The loop the subscriber queues live on, captured at the
        # first subscribe: a publish that arrives on another loop
        # (the consent engine's holds cross threads) hops through
        # call_soon_threadsafe so this loop wakes and its relays
        # deliver. Without the hop the payload lands in the queue
        # from a foreign thread, and the home loop — parked in its
        # poll with nothing scheduled — stays asleep until some
        # unrelated timer fires; frames then deliver seconds late.
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, viewer: int | None = None) -> asyncio.Queue:
        """A bounded queue that will receive every published event
        the *viewer* may see (#1090): ``None`` is the unscoped
        subscription — every frame, the shape a bypassing caller
        (root, the daemon's own uid) takes — and a uid receives
        the unscoped frames beside the owner-scoped frames it
        owns.

        Subscribing requires a running loop — the queue's wakeups
        are scheduled onto it, and the hub remembers it as the home
        loop for cross-thread publishes."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if not self._viewers:
            # An empty hub re-homes: every subscriber leaving and a
            # new one arriving on a fresh loop (a restarted app
            # sharing the hub object) must not keep publishing onto
            # the dead loop the last set homed on.
            self._loop = asyncio.get_running_loop()
        self._viewers[queue] = viewer
        return queue

    def subscribers(self) -> set[asyncio.Queue]:
        """The live subscriber queues (for shutdown sweeps)."""
        return set(self._viewers)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Stop delivering to a queue (subscriber went away)."""
        self._viewers.pop(queue, None)

    async def publish(
        self, event_type: str, data: dict, owner: int | None = None
    ) -> None:
        """Deliver one event; a full (stalled) queue drops its oldest.

        *owner* is the frame's audience (#1090): a frame that
        names the uid its row belongs to reaches that uid's
        subscribers and the unscoped ones alone — the live tail's
        half of the owner filter the audit replay already takes
        (#1083) — while ``None`` (every non-secret frame, and the
        daemon's own internal publishes) broadcasts unchanged.

        The journal line (#762) writes here, on the publishing
        thread before any hop: the record is the publish, not the
        delivery — a publish with no subscriber still journals.

        A publish running on a loop other than the home loop hops
        the delivery through ``call_soon_threadsafe`` — the only
        wake-up a foreign thread can give the home loop's poll."""
        payload = json.dumps({"event": event_type, "data": data})
        logger.info("%s", payload)
        home = self._loop
        if home is not None and home is not asyncio.get_running_loop():
            home.call_soon_threadsafe(self._broadcast, payload, owner)
        else:
            self._broadcast(payload, owner)

    def _broadcast(self, payload: str, owner: int | None) -> None:
        """Enqueue the payload to every subscriber it answers to
        (home loop only): an unscoped frame reaches everyone, an
        owner-scoped one the uid's subscribers and the unscoped
        (#1090)."""
        for queue, viewer in list(self._viewers.items()):
            if owner is None or viewer is None or viewer == owner:
                deliver(queue, payload)


async def relay(queue: asyncio.Queue, send) -> None:
    """Pump events from a subscription queue into a websocket ``send``.

    An empty payload is the shutdown sentinel (``close_all``): the
    relay returns instead of waiting on a queue nothing more will
    arrive on.
    """
    while True:
        payload = await queue.get()
        if payload == "":
            return
        await send({"type": "websocket.send", "text": payload})


QUEUE_MAX = 100


def deliver(queue: asyncio.Queue, payload: str) -> None:
    """Enqueue without blocking; on a full queue drop the oldest first."""
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(payload)
        return
    with contextlib.suppress(asyncio.QueueEmpty):
        queue.get_nowait()
    queue.put_nowait(payload)


def close_all(hub: EventHub) -> None:
    """Wake every subscriber so their relays finish (shutdown/tests).

    The sentinel is appended, never swapped in: events already queued
    ahead of it still deliver before the relay exits.
    """
    for queue in list(hub.subscribers()):
        queue.put_nowait("")
