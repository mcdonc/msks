"""The console websocket (#21, #481): the interactive byte-stream
bridge and the #103 echo watchdog."""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket

from ...microvm.errors import MicrovmError
from ...microvm.local import close_console_stream
from .deps import authed_accept
from .streams import close_reason, pump_streams


async def bridge_console(
    socket: WebSocket, reader, writer, stall_timeout_s: float = 60.0
) -> None:
    """The console's pump: :func:`pump_streams` plus the echo
    watchdog.

    The guest pty echoes every input byte, so client input that draws
    zero guest bytes for ``stall_timeout_s`` names a wedged stream —
    the bridge closes the websocket with 4502 instead of hanging open
    and silent. An idle session (no input in flight) never trips it,
    and ``stall_timeout_s <= 0`` switches the watchdog off.

    Every ending is protocol-clean (#217): a guest stream that ends
    (the getty closes, the shell logs out) closes with 1000, and only a
    client disconnect ends without a close frame — the client is
    gone; there is nothing to tell.
    """
    clock = StallClock()

    def on_output(data: bytes) -> None:
        clock.disarm()

    watchdog = asyncio.create_task(echo_watchdog(socket, clock))
    pump = asyncio.create_task(
        pump_streams(
            socket,
            reader,
            writer,
            on_input=lambda: clock.arm(stall_timeout_s),
            on_output=on_output,
        )
    )
    done, pending = await asyncio.wait(
        {pump, watchdog}, return_when=asyncio.FIRST_COMPLETED
    )
    ended = None
    if pump in done:
        with contextlib.suppress(Exception):
            ended = pump.result()
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for task in done:
        with contextlib.suppress(Exception):
            task.result()
    await close_websocket(socket, ended)


async def close_websocket(socket, ended: str | None) -> None:
    """The bridge's protocol-clean ending (#217): a guest stream
    that ended (the getty closes, the shell logs out) closes with
    1000. A client disconnect (``ended == "socket"``) and a
    watchdog close (``ended is None``, the watchdog having already
    closed 4502) attempt nothing. The suppress covers the races
    where the close already happened or the client vanished
    mid-close.
    """
    if ended == "stream":
        with contextlib.suppress(Exception):
            await socket.close(
                code=1000, reason=close_reason("console closed")
            )


class StallClock:
    """The echo-deadline state (#103): armed by client input, cleared
    by any guest byte.

    A plain :class:`asyncio.Event` cannot carry the deadline: every
    input's ``set()`` resolves the watchdog's pending wait, and a
    later ``clear()`` from the guest-output task cannot cancel a
    timeout that ``wait_for`` already armed — the session would
    close one window after the last keystroke, echo or no echo.
    The clock holds the deadline itself; the watchdog sleeps until
    it and re-reads the state on every wake.
    """

    def __init__(self) -> None:
        self.deadline: float | None = None
        self.changed = asyncio.Event()

    def arm(self, timeout_s: float) -> None:
        """Input left the daemon: the guest has this long to answer.

        Armed only while no deadline is pending: the window runs from
        the FIRST unanswered input, not the last keystroke — a client
        that keeps sending into a wedged stream (the smoke probes'
        resend loop, a script pasting into a dead console) must not
        push its own close out of reach (#103).
        """
        if timeout_s > 0 and self.deadline is None:
            self.deadline = asyncio.get_running_loop().time() + timeout_s
            self.changed.set()

    def disarm(self) -> None:
        """Any guest byte: the stream is alive, no deadline pending."""
        self.deadline = None


#: Close code for a console stream that went silent with input in
#: flight (#103): input the guest never echoed means the stream (not
#: the workspace) is wedged; a reconnect gets a fresh session.
CONSOLE_STALLED_CLOSE_CODE = 4502


async def echo_watchdog(socket: WebSocket, clock: StallClock) -> None:
    """Close the session when the armed echo deadline expires."""
    loop = asyncio.get_running_loop()
    while True:
        # Clear before reading: an arm() racing this loop must leave
        # either a fresh deadline below or a set event to wake on —
        # clearing after the read could erase the wake and sleep
        # through an armed deadline.
        clock.changed.clear()
        deadline = clock.deadline
        if deadline is None:
            await clock.changed.wait()
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            await socket.close(
                code=CONSOLE_STALLED_CLOSE_CODE,
                reason=close_reason(
                    "console stalled: no guest bytes after input; "
                    "reconnect for a fresh session"
                ),
            )
            return
        try:
            await asyncio.wait_for(clock.changed.wait(), remaining)
            clock.changed.clear()
        except TimeoutError:
            # The sleep ran out: loop around, re-read the deadline
            # (input may have re-armed it, output cleared it).
            continue


def router(app) -> APIRouter:
    """The console websocket."""
    api = APIRouter()

    @api.websocket("/api/v1/workspaces/{workspace_id}/console")
    async def console(socket: WebSocket, workspace_id: str) -> None:
        # Byte-stream bridge into a running workspace (#21): the
        # client gets an interactive shell over the same TLS + token
        # as the REST surface, the token riding the handshake's
        # Authorization header (#216). Closing the websocket closes
        # exactly one guest shell session; the workspace keeps
        # running. Auth accepts or closes 4401: the client sees a
        # specific close reason (4401/4404/4501) instead of a
        # generic HTTP 403 rejection.
        if not await authed_accept(app, socket):
            return
        row = await app.state.model.get_workspace(workspace_id)
        if row is None:
            await socket.close(code=4404)
            return
        # The ref (name or id, #246) resolved: the console dial and
        # every keyed surface below use the row's immutable id.
        workspace_id = row["id"]
        try:
            reader, writer = await app.state.microvm.console(workspace_id)
        except MicrovmError as exc:
            # The client is token-authenticated by now: the cause is
            # not a secret, and the close reason is the only channel
            # an operator has for dead-VM vs refused vs deadline
            # (websocket close reasons cap at 123 bytes).
            await socket.close(code=4501, reason=close_reason(str(exc)))
            return
        try:
            await bridge_console(
                socket,
                reader,
                writer,
                app.state.settings.vmm.console_stall_timeout_s,
            )
        finally:
            # The half-close-and-drain teardown keeps the VMM's
            # serial-manager thread alive across client detaches
            # (close_console_stream's docstring names the upstream
            # defect this avoids).
            await close_console_stream(reader, writer)

    return api
