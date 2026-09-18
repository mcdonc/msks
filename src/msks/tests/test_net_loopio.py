"""The loop-portable datagram helpers (#144)."""

import asyncio
import socket

import pytest
from msks.net.loopio import ready_callback, recvfrom, sendto


class FlakySocket(socket.socket):
    """A UDP socket whose first recvfrom reports spurious readiness."""

    def __init__(self) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_DGRAM)
        self._calls = 0

    def recvfrom(self, size: int, *args):
        self._calls += 1
        if self._calls == 1:
            raise BlockingIOError
        return super().recvfrom(size, *args)


class FailingSocket(socket.socket):
    """A UDP socket whose recvfrom drains then reports OSError."""

    def __init__(self) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_DGRAM)

    def recvfrom(self, size: int, *args):
        super().recvfrom(size, *args)  # drain: the loop idles afterward
        raise OSError("read went away")


async def test_recvfrom_delivers_a_datagram() -> None:
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", 0))
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(recvfrom(loop, a, 128))
    await asyncio.sleep(0)
    b.sendto(b"hello", a.getsockname())
    data, addr = await asyncio.wait_for(task, timeout=2.0)
    assert data == b"hello"
    assert addr[1] == b.getsockname()[1]
    a.close()
    b.close()


async def test_recvfrom_on_a_closed_socket_raises_oserror() -> None:
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", 0))
    a.close()
    loop = asyncio.get_running_loop()
    with pytest.raises(OSError):
        await recvfrom(loop, a, 128)


async def test_recvfrom_rearms_on_spurious_readiness() -> None:
    """A readable socket with no datagram waits for the next one.

    Spurious readiness is the reader callback's own concern: it must
    leave the future pending so the loop calls it back when the
    datagram really arrives.
    """
    a = FlakySocket()
    a.bind(("127.0.0.1", 0))
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(recvfrom(loop, a, 128))
    await asyncio.sleep(0)
    b.sendto(b"late", a.getsockname())
    data, _addr = await asyncio.wait_for(task, timeout=2.0)
    assert data == b"late"
    a.close()
    b.close()


async def test_recvfrom_surfaces_a_read_oserror() -> None:
    a = FailingSocket()
    a.bind(("127.0.0.1", 0))
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(recvfrom(loop, a, 128))
    await asyncio.sleep(0)
    b.sendto(b"x", a.getsockname())
    with pytest.raises(OSError, match="read went away"):
        await asyncio.wait_for(task, timeout=2.0)
    a.close()
    b.close()


async def test_ready_callback_leaves_a_done_future_alone() -> None:
    """A reader firing after the awaiter resolved changes nothing.

    The done() guard keeps the callback from setting a result on a
    future the awaiter abandoned — a set_result on a resolved or
    cancelled future would raise InvalidStateError.
    """
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", 0))
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    future.set_result((b"first", ("127.0.0.1", 1)))
    ready = ready_callback(future, a, 128)
    ready()  # a second datagram arrived too late: no-op, no raise
    assert future.result() == (b"first", ("127.0.0.1", 1))
    a.close()


async def test_sendto_writes_the_datagram() -> None:
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", 0))
    sendto(a, b"sent", ("127.0.0.1", a.getsockname()[1]))
    a.settimeout(2.0)
    data, _addr = a.recvfrom(64)
    assert data == b"sent"
    a.close()
