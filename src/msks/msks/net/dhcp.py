"""The per-workspace DHCP service (#52).

msksd is the only DHCP server a workspace guest ever sees: each tap
gets a server task that answers on the appliance-side address of the
workspace's own /30, offering exactly that network's guest address,
the tap as gateway, and the tap as resolver — the naming layer of
#69 slots in behind this single offered resolver.

The wire codec is pure (RFC 951 BOOTP + RFC 2132 options); the
server task is a UDP loop that can run against any socket the caller
provides, so the protocol is unit-testable without privileges.

Fail posture: garbage datagrams are ignored. A guest that asks for
an address outside its /30 gets no answer at all — its connect()
attempts then go nowhere, which is the fail-closed direction.
"""

import asyncio
import contextlib
import socket
import struct
from dataclasses import dataclass

SERVER_PORT = 67
CLIENT_PORT = 68
MAGIC = b"\x63\x82\x53\x63"
BOOTREQUEST = 1
BOOTREPLY = 2
HTYPE_ETHERNET = 6  # hlen: one MAC's worth of chaddr

OPT_SUBNET = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_LEASE = 51
OPT_MSG_TYPE = 53
OPT_SERVER_ID = 54
OPT_REQUESTED_IP = 50
OPT_END = 255

DISCOVER = 1
OFFER = 2
REQUEST = 3
ACK = 5

BROADCAST_FLAG = 0x8000
MIN_PACKET = 300  # BOOTP minimum; pad replies out to it


@dataclass(frozen=True)
class DhcpRequest:
    """The fields of one client datagram the reply needs."""

    xid: bytes
    mac: bytes
    flags: bytes
    options: dict[int, bytes]


def parse_options(blob: bytes) -> dict[int, bytes]:
    """The options tail as ``{code: value}`` (pad, end, overrun safe).

    A TLV whose declared length runs past the blob ends the walk
    un-recorded — a truncated value must not masquerade as an
    option.
    """
    options: dict[int, bytes] = {}
    i = 0
    while (step := _option_step(blob, i, options)) is not None:
        i = step
    return options


def _option_step(blob: bytes, i: int, options: dict[int, bytes]) -> int | None:
    """Consume one option at ``i``; the next index, or None to stop."""
    if i >= len(blob):
        return None
    code, i = blob[i], i + 1
    if code == OPT_END:
        return None
    if code:
        return _option_value(blob, i, code, options)
    return i  # the pad byte


def _option_value(
    blob: bytes, i: int, code: int, options: dict[int, bytes]
) -> int | None:
    """Record one TLV's value (length byte at ``i``); next index."""
    end = i + 1 + blob[i] if i < len(blob) else len(blob) + 1
    if end > len(blob):
        return None
    options[code] = blob[i + 1 : i + 1 + blob[i]]
    return end


def parse_request(data: bytes) -> DhcpRequest | None:
    """One BOOTP request datagram, or None when it is not DHCP."""
    if len(data) < 240 or data[236:240] != MAGIC:
        return None
    if data[0] != BOOTREQUEST:
        return None
    return DhcpRequest(
        xid=data[4:8],
        mac=data[28:34],
        flags=data[10:12],
        options=parse_options(data[240:]),
    )


def encode_options(pairs: list[tuple[int, bytes]]) -> bytes:
    """One options tail: the pairs as TLVs, then the end marker."""
    out = b""
    for code, value in pairs:
        out += bytes((code, len(value))) + value
    return out + bytes((OPT_END,))


def build_reply(
    request: DhcpRequest,
    msg_type: int,
    guest_ip: str,
    tap_ip: str,
    netmask: str,
    lease_s: int,
) -> bytes:
    """One server reply datagram for ``request``."""
    head = struct.pack(
        "!BBBBIHH",
        BOOTREPLY,
        1,
        HTYPE_ETHERNET,
        0,
        int.from_bytes(request.xid, "big"),
        0,
        int.from_bytes(request.flags, "big"),
    )
    head += b"\0" * 4  # ciaddr
    head += socket.inet_aton(guest_ip)  # yiaddr
    head += socket.inet_aton(tap_ip)  # siaddr (the server)
    head += b"\0" * 4  # giaddr
    head += request.mac + b"\0" * 10  # chaddr (16)
    head += b"\0" * 64 + b"\0" * 128  # sname, file
    head += MAGIC
    options = encode_options(
        [
            (OPT_MSG_TYPE, bytes((msg_type,))),
            (OPT_SERVER_ID, socket.inet_aton(tap_ip)),
            (OPT_LEASE, struct.pack("!I", lease_s)),
            (OPT_SUBNET, socket.inet_aton(netmask)),
            (OPT_ROUTER, socket.inet_aton(tap_ip)),
            (OPT_DNS, socket.inet_aton(tap_ip)),
        ]
    )
    packet = head + options
    return packet.ljust(MIN_PACKET, b"\0")


def reply_dest(addr: tuple[str, int], flags: bytes) -> tuple[str, int]:
    """Where a reply goes: broadcast when asked or still unconfigured.

    An INIT-REBOOT-less client that has not configured its address
    yet (source 0.0.0.0) cannot receive unicast; the broadcast flag
    says the same for configured clients.
    """
    broadcast = addr[0] == "0.0.0.0" or bool(flags[0] & 0x80)
    if broadcast:
        return ("255.255.255.255", CLIENT_PORT)
    return (addr[0], addr[1])


class DhcpServer:
    """One workspace's DHCP service on its tap."""

    def __init__(
        self,
        tap_ip: str,
        guest_ip: str,
        netmask: str,
        lease_s: int,
        *,
        bind: tuple[str, int] = ("", SERVER_PORT),
        device: str = "",
    ) -> None:
        self._tap_ip = tap_ip
        self._guest_ip = guest_ip
        self._netmask = netmask
        self._lease_s = lease_s
        self._bind = bind
        self._device = device
        self._sock: socket.socket | None = None

    async def start(self, sock: socket.socket | None = None) -> None:
        """Bind the service socket (a caller-provided one wins).

        A provided socket is assumed already bound — the test path
        hands over a localhost-bound socket; the real path creates
        and binds its own.
        """
        fresh = sock is None
        server = (
            sock
            if sock is not None
            else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        )
        server.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self._device:
            server.setsockopt(
                socket.SOL_SOCKET,
                25,  # SO_BINDTODEVICE
                self._device.encode() + b"\0",
            )
        if fresh:
            server.bind(self._bind)
        server.setblocking(False)
        self._sock = server

    def stop(self) -> None:
        """Close the service socket (idempotent).

        The reader goes first: a socket closed while its reader is
        still registered leaves the selector watching a dead fd
        number, and the next fd to reuse it (a subprocess pipe, say)
        inherits a bogus registration.
        """
        if self._sock is None:
            return
        with contextlib.suppress(RuntimeError, ValueError):
            asyncio.get_running_loop().remove_reader(self._sock)
        self._sock.close()
        self._sock = None

    async def serve(self) -> None:
        """Answer datagrams until the socket closes."""
        loop = asyncio.get_running_loop()
        while True:
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = await loop.sock_recvfrom(sock, 4096)
            except OSError:
                return  # the socket closed underneath the loop
            await self._answer(sock, data, addr)

    async def _answer(
        self, sock: socket.socket, data: bytes, addr: tuple[str, int]
    ) -> None:
        reply = self.reply_for(data)
        if reply is None:
            return
        loop = asyncio.get_running_loop()
        request = parse_request(data)
        assert request is not None  # reply_for parsed it already
        await loop.sock_sendto(sock, reply, reply_dest(addr, request.flags))

    def reply_for(self, data: bytes) -> bytes | None:
        """The reply datagram for one client message, or None."""
        request = parse_request(data)
        if request is None:
            return None
        kind = request.options.get(OPT_MSG_TYPE, b"")
        if kind == bytes((DISCOVER,)):
            return self._offer(request)
        if kind == bytes((REQUEST,)) and self.request_for_us(request):
            return self._ack(request)
        return None

    def _offer(self, request: DhcpRequest) -> bytes:
        return build_reply(
            request, OFFER, self._guest_ip, self._tap_ip, self._netmask, self._lease_s
        )

    def _ack(self, request: DhcpRequest) -> bytes:
        return build_reply(
            request, ACK, self._guest_ip, self._tap_ip, self._netmask, self._lease_s
        )

    def request_for_us(self, request: DhcpRequest) -> bool:
        """Whether a REQUEST selects this server and its address.

        SELECTING (server-id present) must name us; INIT-REBOOT /
        RENEWING (no server-id) is honored when it asks for this
        guest address — the one address this server ever offers.
        """
        server_id = request.options.get(OPT_SERVER_ID)
        if server_id is not None:
            return server_id == socket.inet_aton(self._tap_ip)
        wanted = request.options.get(OPT_REQUESTED_IP)
        return wanted is None or wanted == socket.inet_aton(self._guest_ip)
