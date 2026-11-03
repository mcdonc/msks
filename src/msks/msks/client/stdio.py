"""The EAGAIN-safe stdout write path (#725, #728).

A client surface that writes data to stdout cannot assume the
fd blocks: the console's tty sat in non-blocking mode between
the TUI's exit and the console fallback, and a boot log's burst
outrunning the tty buffer made the write answer EAGAIN (errno
35) — the session crashed (#725). These helpers treat EAGAIN as
wait-for-writability and retry, and the sinks here adapt that
shape for stdout used as a data sink (ssh's ProxyCommand bridge,
``home export -``), where the fd's blocking mode is likewise not
an invariant this client owns (#728).
"""

import asyncio
import os


async def write_draining(buffered, message: bytes) -> None:
    """Write one message through a stream whose fd may sit in
    non-blocking mode: a tty the terminal plumbing left that way
    plus a burst that outruns its buffer makes the write or its
    flush answer EAGAIN — the caller's flow waits for
    writability and retries instead of crashing (#725). The
    message itself goes out through the raw fd: ``os.write``
    reports exactly how much it took, so a retry resumes where it
    stopped — a buffered write that raises may already have taken
    a prefix of the caller's bytes (verified empirically: a
    100-byte buffer holding 50, asked for 60, raises with 50 of
    the new bytes buffered), so re-writing the message through
    the buffer would duplicate that prefix. A raising buffered
    flush keeps its bytes, so the drain of anything the buffer
    already holds is safe to retry. A buffer with no fd behind it
    cannot sit in non-blocking mode; the plain path serves it.
    One conscious trade: a consumer that opens and then never
    drains parks this write for as long as a blocking stdout
    would park the same wedged consumer."""
    loop = asyncio.get_running_loop()
    fd = resolve_fd(buffered)
    if fd is None:
        buffered.write(message)
        buffered.flush()
        return
    await flush_draining(buffered, fd, loop)
    await write_raw(fd, message, loop)


def resolve_fd(buffered) -> int | None:
    """The fd behind the buffer, or None for a buffer with no fd
    — one that cannot sit in non-blocking mode and needs no
    draining."""
    try:
        return buffered.fileno()
    except OSError, ValueError:
        return None


async def flush_draining(buffered, fd: int, loop) -> None:
    """The buffer's own held bytes out first: a raising flush
    keeps them, so the retry lands them whole."""
    while True:
        try:
            buffered.flush()
            return
        except BlockingIOError:
            await await_writable(loop, fd)


async def write_raw(fd: int, message: bytes, loop) -> None:
    """The message through the raw fd: ``os.write`` reports
    exactly how much it took, so a retry resumes where it
    stopped — the buffered layer is never re-fed."""
    view = memoryview(message)
    while view:
        try:
            view = view[os.write(fd, view) :]
        except BlockingIOError:
            await await_writable(loop, fd)


async def await_writable(loop, fd: int) -> None:
    """One EAGAIN's worth of patience: park until the fd can take
    more, then let the caller's retry run."""
    writable = asyncio.Event()
    loop.add_writer(fd, writable.set)
    try:
        await writable.wait()
    finally:
        loop.remove_writer(fd)


class DrainSink:
    """A sink whose ``write()`` holds the chunk in memory and
    whose awaited ``drain()`` lands it through the EAGAIN-safe
    shape: stdout used as a data sink — ssh's ProxyCommand
    bridge, ``home export -`` — where the fd's blocking mode is
    not an invariant this client owns (#725 falsified it once,
    #728 spread the fix). ``write()`` never touches the buffered
    layer, so no chunk size can meet its capacity and raise
    (#729's review demonstrated that failure on a chunk above
    the buffer's size); ``drain()`` lands what is held raw, in
    order, and empty-handed against a stalled consumer it parks
    — a parked drain the caller cancels abandons the sink,
    strictly better than the crash the same fd state produced
    before (#725). Abandon means abandon: a drain interrupted by
    exception or cancellation may have landed a prefix of its
    chunk already, and nothing may write into or re-drain the
    sink afterwards — the shipped callers (the bridge's pump and
    the export download) end the session on any interruption.
    The sink owns its wrapped buffer exclusively: bytes another
    writer left in the buffered layer would land after the held
    chunk, so the wrapped buffer is never written through
    elsewhere (every shipped notice goes to stderr)."""

    def __init__(self, buffered) -> None:
        self._buffer = buffered
        self._held = bytearray()

    def write(self, data) -> None:
        self._held += data

    async def drain(self) -> None:
        if not self._held:
            return
        loop = asyncio.get_running_loop()
        fd = resolve_fd(self._buffer)
        if fd is None:
            self._buffer.write(self._held)
            self._buffer.flush()
        else:
            await write_raw(fd, self._held, loop)
        self._held.clear()
