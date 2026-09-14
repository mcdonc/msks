"""The DHCP codec and per-workspace service (#52).

The codec tests build wire bytes by hand; the service tests run the
real UDP loop against injected localhost sockets — no privileges
needed, since a caller-provided socket skips SO_BINDTODEVICE.
"""

import asyncio
import socket
import struct

import pytest
from msks.net import dhcp


def message(
    kind: int, xid: bytes, mac: bytes, flags: bytes = b"\0\0", options: bytes = b""
) -> bytes:
    """A client datagram with hand-built options."""
    head = struct.pack(
        "!BBBBIHH",
        dhcp.BOOTREQUEST,
        1,
        6,
        0,
        int.from_bytes(xid, "big"),
        0,
        int.from_bytes(flags[:2], "big"),
    )
    head += b"\0" * 16  # ciaddr, yiaddr, siaddr, giaddr
    head += mac + b"\0" * 10  # chaddr (16)
    head += b"\0" * 64 + b"\0" * 128  # sname, file
    head += dhcp.MAGIC
    tail = bytes((dhcp.OPT_MSG_TYPE, 1, kind)) + options
    return (head + tail).ljust(dhcp.MIN_PACKET, b"\0")


def discover(xid: bytes, mac: bytes, flags: bytes = b"\0\0") -> bytes:
    return message(dhcp.DISCOVER, xid, mac, flags)


def request(xid: bytes, mac: bytes, server_id: str, wanted_ip: str) -> bytes:
    """A SELECTING REQUEST naming a server and an address."""
    options = (
        bytes((dhcp.OPT_SERVER_ID, 4))
        + socket.inet_aton(server_id)
        + bytes((dhcp.OPT_REQUESTED_IP, 4))
        + socket.inet_aton(wanted_ip)
    )
    return message(dhcp.REQUEST, xid, mac, options=options)


def parse_options_of(data: bytes) -> dict[int, bytes]:
    """parse_options over a full packet's tail."""
    return dhcp.parse_options(data[240:])


def test_parse_options_handles_pad_end_and_overrun() -> None:
    # pad byte, one TLV, end marker, then junk that must be ignored.
    blob = b"\0" + bytes((53, 1, 1)) + bytes((255,)) + b"\xff\xff"
    assert dhcp.parse_options(blob) == {53: b"\x01"}
    # a truncated TLV (length runs past the blob) stops the walk.
    assert dhcp.parse_options(bytes((53, 4, 1))) == {}


def test_parse_request_accepts_only_client_dhcp() -> None:
    good = discover(b"\x01\x02\x03\x04", b"\xaa\xbb\xcc\xdd\xee\xff")
    parsed = dhcp.parse_request(good)
    assert parsed is not None
    assert parsed.xid == b"\x01\x02\x03\x04"
    assert parsed.mac == b"\xaa\xbb\xcc\xdd\xee\xff"
    assert parsed.options[53] == b"\x01"
    # Not BOOTP, not DHCP magic, too short: all rejected.
    assert dhcp.parse_request(b"\x02" + good[1:]) is None
    assert dhcp.parse_request(good[:235] + b"XXXX" + good[239:]) is None
    assert dhcp.parse_request(good[:100]) is None


def test_build_reply_carries_the_offer_shape() -> None:
    packet = dhcp.DhcpRequest(
        xid=b"\x09\x08\x07\x06",
        mac=b"\x01\x02\x03\x04\x05\x06",
        flags=b"\x80\0",
        options={},
    )
    reply = dhcp.build_reply(
        packet, dhcp.OFFER, "172.31.0.1", "172.31.0.2", "255.255.255.252", 3600
    )
    assert len(reply) >= dhcp.MIN_PACKET
    assert reply[0] == dhcp.BOOTREPLY
    assert reply[4:8] == b"\x09\x08\x07\x06"
    assert reply[10:12] == b"\x80\0"
    assert socket.inet_ntoa(reply[16:20]) == "172.31.0.1"
    assert socket.inet_ntoa(reply[20:24]) == "172.31.0.2"
    assert reply[28:34] == b"\x01\x02\x03\x04\x05\x06"
    assert reply[236:240] == dhcp.MAGIC
    options = parse_options_of(reply)
    assert options[53] == bytes((dhcp.OFFER,))
    assert socket.inet_ntoa(options[54]) == "172.31.0.2"
    assert socket.inet_ntoa(options[1]) == "255.255.255.252"
    assert socket.inet_ntoa(options[3]) == "172.31.0.2"
    assert socket.inet_ntoa(options[6]) == "172.31.0.2"
    assert struct.unpack("!I", options[51])[0] == 3600


def test_reply_dest_broadcast_rules() -> None:
    broadcast = ("255.255.255.255", dhcp.CLIENT_PORT)
    assert dhcp.reply_dest(("0.0.0.0", 68), b"\0\0") == broadcast
    assert dhcp.reply_dest(("172.31.0.1", 54321), b"\x80\0") == broadcast
    assert dhcp.reply_dest(("172.31.0.1", 54321), b"\0\0") == ("172.31.0.1", 54321)


def server() -> dhcp.DhcpServer:
    return dhcp.DhcpServer("172.31.0.2", "172.31.0.1", "255.255.255.252", 3600)


def test_reply_for_answers_discover_and_our_requests() -> None:
    service = server()
    mac = b"\xaa\xbb\xcc\xdd\xee\xff"
    offer = service.reply_for(discover(b"\x01\x00\x00\x01", mac))
    assert offer is not None
    assert parse_options_of(offer)[53] == bytes((dhcp.OFFER,))
    # SELECTING for us: acknowledged.
    ack = service.reply_for(
        request(b"\x01\x00\x00\x02", mac, "172.31.0.2", "172.31.0.1")
    )
    assert ack is not None
    assert parse_options_of(ack)[53] == bytes((dhcp.ACK,))
    # SELECTING for someone else, and garbage: silence.
    assert (
        service.reply_for(request(b"\x01\x00\x00\x03", mac, "10.9.9.9", "172.31.0.1"))
        is None
    )
    assert service.reply_for(b"not-dhcp-at-all") is None


def test_request_for_us_honors_init_reboot_renewal() -> None:
    service = server()
    mac = b"\xaa\xbb\xcc\xdd\xee\xff"
    # INIT-REBOOT asking for our address (or nothing): ours.
    for wanted in ("172.31.0.1", None):
        options = {50: socket.inet_aton(wanted)} if wanted else {}
        packet = dhcp.DhcpRequest(
            xid=b"\1\1\1\1", mac=mac, flags=b"\0\0", options=options
        )
        assert service.request_for_us(packet)
    # INIT-REBOOT asking for an address outside the /30: refused.
    packet = dhcp.DhcpRequest(
        xid=b"\1\1\1\1",
        mac=mac,
        flags=b"\0\0",
        options={50: socket.inet_aton("10.0.0.5")},
    )
    assert not service.request_for_us(packet)


@pytest.fixture
async def loop_pair():
    """A started server on a localhost socket plus its client peer."""
    service = dhcp.DhcpServer("127.0.0.1", "127.0.0.2", "255.255.255.252", 3600)
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_sock.bind(("127.0.0.1", 0))
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(2.0)
    await service.start(sock=server_sock)
    task = asyncio.create_task(service.serve())
    try:
        yield service, client
    finally:
        task.cancel()
        service.stop()
        client.close()


async def test_serve_answers_a_discover(loop_pair) -> None:
    service, client = loop_pair
    client.sendto(
        discover(b"\x0a\x0b\x0c\x0d", b"\xaa\xbb\xcc\xdd\xee\xff"),
        service._sock.getsockname(),
    )
    reply, _addr = await asyncio.to_thread(client.recvfrom, 4096)
    assert parse_options_of(reply)[53] == bytes((dhcp.OFFER,))


async def test_serve_ignores_foreign_requests(loop_pair) -> None:
    service, client = loop_pair
    client.sendto(
        request(
            b"\x0a\x0b\x0c\x0e", b"\xaa\xbb\xcc\xdd\xee\xff", "10.9.9.9", "172.31.0.1"
        ),
        service._sock.getsockname(),
    )
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(client.recvfrom, 4096)


async def test_serve_stops_when_the_socket_closes() -> None:
    service = dhcp.DhcpServer("127.0.0.1", "127.0.0.2", "255.255.255.252", 3600)
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_sock.bind(("127.0.0.1", 0))
    await service.start(sock=server_sock)
    task = asyncio.create_task(service.serve())
    server_sock.close()
    await asyncio.wait_for(task, 2.0)
    service.stop()


def test_parse_options_stops_at_a_missing_length_byte() -> None:
    # A trailing option code with no length byte: dropped, not choked on.
    assert dhcp.parse_options(bytes((53,))) == {}


async def test_start_binds_its_own_socket() -> None:
    service = dhcp.DhcpServer(
        "127.0.0.1", "127.0.0.2", "255.255.255.252", 3600, bind=("127.0.0.1", 0)
    )
    await service.start()  # the fresh path: creates and binds its own
    assert service._sock is not None
    serve = asyncio.create_task(service.serve())
    service.stop()  # idempotent, and ends the serve loop
    service.stop()
    await asyncio.wait_for(serve, 2.0)
    assert service._sock is None


class FakeSock:
    """A socket-shaped recorder for the privilege-needing options."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def setsockopt(self, *args) -> None:
        self.calls.append(("setsockopt", *args))

    def bind(self, *args) -> None:
        self.calls.append(("bind", *args))

    def setblocking(self, mode: bool) -> None:
        self.calls.append(("setblocking", mode))


async def test_start_sets_the_device_option() -> None:
    service = dhcp.DhcpServer(
        "172.31.0.2", "172.31.0.1", "255.255.255.252", 3600, device="msks-x"
    )
    sock = FakeSock()
    await service.start(sock=sock)
    assert sock.calls[0] == ("setsockopt", socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    assert sock.calls[1] == ("setsockopt", socket.SOL_SOCKET, 25, b"msks-x\0")
    assert ("setblocking", False) in sock.calls
    assert ("bind", ("", 67)) not in sock.calls  # injected: pre-bound
