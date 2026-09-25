"""The workspace page's decider connection (#309).

While the workspace screen is open, the page registers as the
workspace's decider — the same registration ``msks egress tui``
makes — so interactive holds land on the page (#69: holds exist
only while a decider watches) and the rules frames keep the
consent status line honest. The frame parsing is
:mod:`msks.client.tui.consent`'s; the reconnect ladder and the
close-code meanings are :mod:`msks.client.tui.consent_app`'s;
this module owns only the connection's lifecycle for a host that
is not the consent app itself.
"""

import asyncio

import websockets

from .consent import REJECTED as FRAME_REJECTED
from .consent import ConsentController
from .consent_app import (
    RECONNECT_DELAYS,
    REFUSED_RETRY_INTERVAL,
    backoff,
    close_ws,
    default_ws_factory,
    refused_close,
    registration_frame,
)

#: The link's labels, the states the workspace page renders.
CONNECTED = "connected"
RECONNECTING = "reconnecting"
REFUSED = "refused — bad token?"
REJECTED = "rejected"


class DeciderLink:
    """One workspace's events connection feeding a
    :class:`ConsentController`; start/stop bracket a screen's
    life."""

    def __init__(
        self,
        workspace_id: str,
        *,
        hold_timeout: float = 120.0,
        ws_factory=None,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
    ) -> None:
        self.workspace_id = workspace_id
        self.controller = ConsentController(
            hold_timeout=hold_timeout, workspace_id=workspace_id
        )
        self.state = RECONNECTING
        self.reject_reason = ""
        self.reconnect_delays = reconnect_delays
        self._ws_factory = ws_factory or default_ws_factory
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Run the connection loop, once (a second call is a
        no-op); the task is referenced so it cannot be collected
        mid-await."""
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        """End the loop now — the task's cancel lands inside the
        parked recv and closes the socket with it."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def run(self) -> None:
        """Connect, register, pump; reconnect until cancelled — the
        consent app's ladder: backoff on drops, one slow fixed
        retry on an auth refusal, the ladder resets after any
        connection that reached its serve loop."""
        attempt = 0
        while True:
            connected, refused, rejected = await self.pump_one()
            if rejected:
                return  # the daemon refused the registration for good
            if refused:
                await asyncio.sleep(REFUSED_RETRY_INTERVAL)
                continue
            attempt = 0 if connected else attempt + 1
            await asyncio.sleep(backoff(self.reconnect_delays, attempt))

    async def pump_one(self) -> tuple[bool, bool, bool]:
        """One connection's lifetime; ``(connected, refused,
        rejected)`` — whether the dial succeeded (resetting the
        backoff ladder on the next drop), whether the close was an
        auth refusal (retry slowly), and whether the daemon refused
        the registration outright (stop)."""
        try:
            ws = await self._ws_factory().__aenter__()
        except Exception:
            self.state = RECONNECTING
            return False, False, False
        try:
            return await self.serve(ws)
        finally:
            await close_ws(ws)

    async def serve(self, ws) -> tuple[bool, bool, bool]:
        """Register, then feed every frame to the controller; a
        clean close, a close at the registration send, or any error
        reconnects; an auth refusal retries slowly, a rejected
        registration ends the loop."""
        self.state = CONNECTED
        try:
            await ws.send(registration_frame(self.workspace_id))
            self.controller.reset()
            async for raw in ws:
                if self.land_frame(raw):
                    return True, False, True
        except websockets.ConnectionClosed as exc:
            return self.closed(exc)
        except Exception:
            self.state = RECONNECTING
            return True, False, False
        # The iterator ended on its own: a clean close (1000/1001 —
        # websockets exits the async-for normally on OK codes, it
        # does not raise), a restarting daemon among them.
        self.state = RECONNECTING
        return True, False, False

    def land_frame(self, raw: str) -> bool:
        """One frame into the controller; True when it was the
        daemon's registration refusal (the loop's stop signal)."""
        outcome, payload = self.controller.apply_frame(raw)
        if outcome != FRAME_REJECTED:
            return False
        self.state = REJECTED
        self.reject_reason = payload or "registration rejected"
        return True

    def closed(
        self, exc: websockets.ConnectionClosed
    ) -> tuple[bool, bool, bool]:
        """The connection's close: an auth refusal retries slowly,
        anything else takes the backoff ladder."""
        refused = refused_close(exc)
        self.state = REFUSED if refused else RECONNECTING
        return True, refused, False
