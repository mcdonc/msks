"""The WSS event channel hub (#8).

One pub/sub hub behind ``GET /api/v1/events``: the status watcher
publishes workspace lifecycle transitions, every connected client
socket receives them. Same listener/port as HTTPS — one surface.
"""

import asyncio
import json


class EventHub:
    """Fan-out to any number of websocket subscribers."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        """A queue that will receive every published event."""
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Stop delivering to a queue (subscriber went away)."""
        self._queues.discard(queue)

    async def publish(self, event_type: str, data: dict) -> None:
        """Deliver one event to every subscriber."""
        for queue in list(self._queues):
            queue.put_nowait(json.dumps({"event": event_type, "data": data}))


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


def close_all(hub: EventHub) -> None:
    """Wake every subscriber so their relays finish (shutdown/tests).

    The sentinel is appended, never swapped in: events already queued
    ahead of it still deliver before the relay exits.
    """
    for queue in list(hub._queues):
        queue.put_nowait("")
