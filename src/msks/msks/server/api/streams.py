"""The raw byte pump the console and forward bridges share
(#21, #103, #217), and the close-reason trimmer."""

import asyncio
import contextlib

from fastapi import WebSocket


def close_reason(text: str, limit: int = 120) -> str:
    """A websocket close reason that fits its wire budget in bytes.

    Close reasons carry at most 123 bytes; a multibyte character at
    the cut makes a non-conformant frame, so the truncation happens
    on the UTF-8 bytes.
    """
    encoded = text.encode()[:limit]
    return encoded.decode(errors="ignore")


def noop(*_observed) -> None:
    """The observer a plain forward passes: nothing to observe."""
    return None


async def pump_streams(
    socket: WebSocket, reader, writer, *, on_input=None, on_output=None
) -> str:
    """Pump raw bytes between a websocket and a byte stream.

    Two tasks, no queue: backpressure is websocket/TCP flow control
    (the byte stream must not lose or buffer unboundedly, #21).
    Whichever side finishes first (client disconnect or stream EOF)
    cancels the other — and an outer cancellation (the console's
    watchdog closing first, #103) cancels both inner tasks here, so
    nothing outlives the bridge writing into a closed stream.
    ``on_input`` and ``on_output`` observe the traffic in flight —
    the console's echo watchdog (#103) arms and disarms its deadline
    through them, and the console's refusal scan (#217) reads the
    guest bytes through ``on_output``; a plain forward passes none.
    Returns which side ended the session — ``"stream"`` for a guest
    EOF (or stream error), ``"socket"`` for a client disconnect —
    so the console bridge can close the websocket protocol-clean
    instead of returning out of the handler (#217).
    """
    to_guest = asyncio.create_task(
        ws_to_stream(socket, writer, on_input or noop)
    )
    to_client = asyncio.create_task(stream_to_ws(reader, socket, on_output))
    try:
        done, pending = await asyncio.wait(
            {to_guest, to_client}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        await cancel_tasks((to_guest, to_client))
        raise
    ended = "stream" if to_client in done else "socket"
    await settle(done, pending)
    return ended


async def cancel_tasks(tasks) -> None:
    """Cancel and drain the tasks, retrieving their outcomes — the
    outer-cancellation exit leaves no task and no unretrieved
    exception behind."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def settle(done, pending) -> None:
    """End the two-way race: cancel the loser, then read both
    outcomes quietly (the survivor's ending is the session's)."""
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for task in done:
        with contextlib.suppress(Exception):
            task.result()


async def ws_to_stream(socket: WebSocket, writer, on_input=noop) -> None:
    """Client bytes to the stream; returns on disconnect."""
    while True:
        msg = await socket.receive()
        if msg["type"] != "websocket.receive":
            return
        data = msg.get("bytes")
        if data is None:
            data = msg.get("text", "").encode()
        if data:
            writer.write(data)
            # Input is now in flight: the console's echo deadline
            # starts if none is pending — one deadline per quiet
            # window, from the first unanswered input (#103).
            on_input()
            await writer.drain()


async def stream_to_ws(reader, socket: WebSocket, on_output=None) -> None:
    """Stream bytes to the client; returns on stream EOF."""
    while True:
        data = await reader.read(4096)
        if not data:
            return
        # Any stream byte proves the stream alive (the console's
        # watchdog disarm, #103; the refusal scan reads the same
        # chunk, #217).
        if on_output is not None:
            on_output(data)
        await socket.send_bytes(data)
