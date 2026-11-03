"""The shared EAGAIN-safe stdout write path (#725, #728).

The drain shape lives in ``msks.client.stdio``: the console pump
writes through it (#725's crash), and the ProxyCommand bridge and
``home export -`` sinks ride the same shape through
``DrainSink`` (#728). These tests pin its semantics against live
non-blocking pipes and fd-less buffers.
"""

import asyncio
import io
import os
import threading
import time

from msks.client import stdio


async def test_write_draining_flushes_a_held_buffer_first() -> None:
    """The drain's first half: a buffer that already holds bytes
    when EAGAIN arrives keeps them, and the retry lands them
    whole before the message's own raw write — verified against
    a non-blocking pipe, held full by a stalled drainer."""
    r, w = os.pipe()
    os.set_blocking(w, False)
    buffered = open(w, "wb", buffering=100)
    buffered.write(b"held")  # buffered, not yet flushed
    try:
        while True:
            os.write(w, b"x" * 4096)
    except BlockingIOError:
        pass
    received = bytearray()

    def drain() -> None:
        time.sleep(0.3)  # hold the pipe full: the flush meets EAGAIN
        while True:
            data = os.read(r, 65536)
            if not data:
                return
            received.extend(data)

    drainer = threading.Thread(target=drain)
    drainer.start()
    try:
        await asyncio.wait_for(
            stdio.write_draining(buffered, b"msg"), timeout=30
        )
    finally:
        buffered.close()  # closes the write end: the drainer exits
        drainer.join(timeout=30)
        os.close(r)
    payload = bytes(received).replace(b"x", b"")
    assert payload == b"heldmsg"


async def test_write_draining_plain_path_for_an_fdless_buffer() -> None:
    """A buffer with no fd behind it cannot sit in non-blocking
    mode: the write lands through the plain write-and-flush, in
    order and whole."""
    buffered = io.BytesIO()
    await stdio.write_draining(buffered, b"plain")
    assert buffered.getvalue() == b"plain"


async def test_drain_sink_lands_a_burst_on_a_nonblocking_pipe() -> None:
    """The ProxyCommand shape (#728), live pipes: ``write()``
    buffers, the awaited ``drain()`` lands the bytes, and a burst
    that outruns the pipe waits for writability instead of
    crashing — every byte once, in order."""
    r, w = os.pipe()
    os.set_blocking(w, False)
    buffered = open(w, "wb", buffering=4096)
    sink = stdio.DrainSink(buffered)
    chunks = [bytes([65 + i % 26]) * 4096 for i in range(64)]
    expected = b"".join(chunks)
    received = bytearray()

    def drain_pipe() -> None:
        # Hold the pipe full first: the drain provably meets EAGAIN
        # and parks in wait-for-writability; the release then lets
        # the rest of the burst flow through.
        time.sleep(0.3)
        while True:
            data = os.read(r, 65536)
            if not data:  # the write end closed: the burst is whole
                return
            received.extend(data)

    drainer = threading.Thread(target=drain_pipe)
    drainer.start()
    try:
        for chunk in chunks:
            sink.write(chunk)
            await asyncio.wait_for(sink.drain(), timeout=30)
    finally:
        buffered.close()  # closes the write end: the drainer exits
        drainer.join(timeout=30)
        os.close(r)
    assert bytes(received) == expected


async def test_drain_sink_plain_path_and_noop_close() -> None:
    """An fd-less sink drains through the plain flush, and the
    StreamWriter-shaped no-ops (``close``, ``wait_closed``) stay
    no-ops — the pipe pair's cleanup can call them freely."""
    buffered = io.BytesIO()
    sink = stdio.DrainSink(buffered)
    sink.write(b"bytes")
    await sink.drain()
    sink.close()
    await sink.wait_closed()
    assert buffered.getvalue() == b"bytes"


async def test_download_awaits_a_sink_provided_drain() -> None:
    """``rest.download`` honors the asyncio StreamWriter shape: a
    sink with ``drain`` has it awaited after every chunk (the
    stdout export sink), a plain file sink streams untouched."""

    class RecordingSink:
        def __init__(self) -> None:
            self.written: list[bytes] = []
            self.drains = 0

        def write(self, chunk: bytes) -> None:
            self.written.append(chunk)

        async def drain(self) -> None:
            self.drains += 1

    class Client:
        def stream(self, method: str, path: str):
            assert (method, path) == ("GET", "/api/v1/stream")

            class Response:
                is_error = False

                async def aiter_bytes(self):
                    yield b"one"
                    yield b"two"

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return None

            return Response()

    from msks.client.rest import download

    recording = RecordingSink()
    total = await download(Client(), "/api/v1/stream", recording)  # type: ignore[arg-type]
    assert total == 6
    assert recording.written == [b"one", b"two"]
    assert recording.drains == 2

    plain = io.BytesIO()
    total = await download(Client(), "/api/v1/stream", plain)
    assert total == 6
    assert plain.getvalue() == b"onetwo"
