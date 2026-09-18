"""Loop-portable datagram awaitables.

uvloop — pulled in by ``uvicorn[standard]``, so present in the
development venv the dev-tree and bare-host daemons run from — does
not implement ``loop.sock_recvfrom``/``loop.sock_sendto`` (a plain
``NotImplementedError``, observed with uvloop 0.22 under python
3.14). The nix-built appliance closure depends on plain ``uvicorn``
without the extra, so the same code served DHCP and DNS there while
silently dying on its first await under the venv: the receive task
crashed inside ``serve()`` and the workspace guest never got a lease.

These helpers use the reader/future pattern every event loop
implements, so datagram service code behaves identically under
asyncio and uvloop.
"""

import asyncio
import socket
from collections.abc import Callable


def ready_callback(
    future: asyncio.Future, sock: socket.socket, size: int
) -> Callable[[], None]:
    """The reader callback for :func:`recvfrom` (exposed for tests).

    One datagram resolves the future; spurious readiness re-arms by
    leaving it pending; a read error resolves exceptionally; a
    future already resolved (the awaiter gave up between events) is
    left untouched.
    """

    def ready() -> None:
        if future.done():
            return
        try:
            future.set_result(sock.recvfrom(size))
        except BlockingIOError, InterruptedError:
            return
        except OSError as exc:
            future.set_exception(exc)

    return ready


async def recvfrom(
    loop: asyncio.AbstractEventLoop, sock: socket.socket, size: int
) -> tuple[bytes, tuple[str, int]]:
    """Await one datagram; returns ``(data, address)``.

    Mirrors ``loop.sock_recvfrom``. A spurious readiness (readable
    but no datagram) simply re-arms — the loop calls back again.
    """
    future: asyncio.Future[tuple[bytes, tuple[str, int]]] = loop.create_future()
    fd = sock.fileno()
    if fd < 0:
        # Already closed: the caller's OSError path treats this as
        # teardown (the awaited form raised OSError the same way).
        raise OSError("socket closed before receive")

    ready = ready_callback(future, sock, size)
    loop.add_reader(fd, ready)
    try:
        return await future
    finally:
        # A closed socket has fd -1 and nothing registered; anything
        # else must be removed so the loop stops watching it.
        fd = sock.fileno()
        if fd >= 0:
            loop.remove_reader(fd)


def sendto(sock: socket.socket, data: bytes, addr: tuple[str, int]) -> None:
    """Send one datagram without awaiting.

    A UDP ``sendto`` on a non-blocking socket completes inline; a full
    send buffer raises ``OSError`` just as the awaited form would.
    """
    sock.sendto(data, addr)
