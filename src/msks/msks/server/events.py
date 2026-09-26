"""The WSS event channel hub (#8).

One pub/sub hub behind ``GET /api/v1/events``: the status watcher
publishes workspace lifecycle transitions, every connected client
socket receives them. Same listener/port as HTTPS — one surface.
"""

import asyncio
import contextlib
import json


class EventHub:
    """Fan-out to any number of websocket subscribers."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        # The loop the subscriber queues live on, captured at the
        # first subscribe: a publish that arrives on another loop
        # (the consent engine's holds cross threads) hops through
        # call_soon_threadsafe so this loop wakes and its relays
        # deliver. Without the hop the payload lands in the queue
        # from a foreign thread, and the home loop — parked in its
        # poll with nothing scheduled — stays asleep until some
        # unrelated timer fires; frames then deliver seconds late.
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue:
        """A bounded queue that will receive every published event.

        Subscribing requires a running loop — the queue's wakeups
        are scheduled onto it, and the hub remembers it as the home
        loop for cross-thread publishes."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if not self._queues:
            # An empty hub re-homes: every subscriber leaving and a
            # new one arriving on a fresh loop (a restarted app
            # sharing the hub object) must not keep publishing onto
            # the dead loop the last set homed on.
            self._loop = asyncio.get_running_loop()
        self._queues.add(queue)
        return queue

    def subscribers(self) -> set[asyncio.Queue]:
        """The live subscriber queues (for shutdown sweeps)."""
        return set(self._queues)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Stop delivering to a queue (subscriber went away)."""
        self._queues.discard(queue)

    async def publish(self, event_type: str, data: dict) -> None:
        """Deliver one event; a full (stalled) queue drops its oldest.

        A publish running on a loop other than the home loop hops
        the delivery through ``call_soon_threadsafe`` — the only
        wake-up a foreign thread can give the home loop's poll."""
        payload = json.dumps({"event": event_type, "data": data})
        home = self._loop
        if home is not None and home is not asyncio.get_running_loop():
            home.call_soon_threadsafe(self._broadcast, payload)
        else:
            self._broadcast(payload)

    def _broadcast(self, payload: str) -> None:
        """Enqueue the payload to every subscriber (home loop only)."""
        for queue in list(self._queues):
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
