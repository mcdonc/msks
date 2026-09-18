"""The appliance DNS forwarder (#52).

Each egress tap gets a forwarder bound to its appliance-side address
on port 53: the resolver DHCP offers, and the only one the guest can
reach. Queries relay to the configured upstream (the appliance's own
resolv.conf by default) and the answers return to the guest verbatim
— no filtering yet. The naming layer of #69 (cache, name↔IP
learning, the DoT/DoH lockout) grows inside this seam.

Relayed verbatim on purpose: a forwarder that does not parse the
message cannot break a query feature it never heard of, and the
transaction id inside the datagram already matches the reply to the
asking client.
"""

import asyncio
import contextlib
import socket
from pathlib import Path

from .loopio import recvfrom, sendto

DNS_PORT = 53

# A full-size DNS datagram: the biggest answer UDP carries.
MAX_DATAGRAM = 65535


def upstream_from_resolv(
    path: Path = Path("/etc/resolv.conf"),
) -> tuple[str, int] | None:
    """The first nameserver in a resolv.conf, as an upstream."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            return (fields[1], DNS_PORT)
    return None


class DnsForwarder:
    """One workspace's resolver on its tap address.

    ``client_ip`` is the one address queries may come from: the
    workspace's guest. A datagram from anything else — a spoofed
    source naming an off-tap victim, say — is dropped unread, which
    is what keeps the forwarder from serving as a reflection
    amplifier.
    """

    def __init__(
        self,
        upstream: tuple[str, int],
        timeout_s: float = 3.0,
        *,
        bind: tuple[str, int] | None = None,
        client_ip: str = "",
    ) -> None:
        self._upstream = upstream
        self._timeout_s = timeout_s
        self._bind = bind or ("0.0.0.0", DNS_PORT)
        self._client_ip = client_ip
        self._sock: socket.socket | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self, sock: socket.socket | None = None) -> None:
        """Bind the service socket (a caller-provided one wins)."""
        server = (
            sock
            if sock is not None
            else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        )
        server.bind(self._bind)
        server.setblocking(False)
        self._sock = server

    def stop(self) -> None:
        """Cancel in-flight relays and close the socket (idempotent).

        The reader goes first: a socket closed while its reader is
        still registered leaves the selector watching a dead fd
        number, and the next fd to reuse it inherits a bogus
        registration.
        """
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._sock is None:
            return
        with contextlib.suppress(RuntimeError, ValueError):
            asyncio.get_running_loop().remove_reader(self._sock)
        self._sock.close()
        self._sock = None

    async def serve(self) -> None:
        """Dispatch datagrams until the socket closes."""
        loop = asyncio.get_running_loop()
        while True:
            sock = self._sock
            if sock is None:
                return
            try:
                data, client = await recvfrom(loop, sock, MAX_DATAGRAM)
            except OSError:
                return  # the socket closed underneath the loop
            if client[0] != self._client_ip:
                continue  # not this tap's guest: dropped unread
            task = asyncio.create_task(self._relay(sock, data, client))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _relay(
        self, sock: socket.socket, query: bytes, client: tuple[str, int]
    ) -> None:
        upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        upstream.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            sendto(upstream, query, self._upstream)
            answer = await asyncio.wait_for(
                recvfrom(loop, upstream, MAX_DATAGRAM), self._timeout_s
            )
            # The captured reference, not self._sock: a stop() between
            # dispatch and reply would otherwise race the reply onto a
            # closed socket (an AttributeError past the OSError guard).
            sendto(sock, answer[0], client)
        except TimeoutError, OSError:
            # No upstream answer inside the window, or a datagram send
            # that failed outright (a full buffer on the reply, a
            # vanished tap): silence either way. The client's own
            # resolver timeout retries or fails; an empty reply would
            # only confuse it.
            return
        finally:
            upstream.close()
