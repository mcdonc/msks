"""The DNS forwarder and its upstream resolution (#52)."""

import asyncio
import socket
from pathlib import Path

import pytest
from msks.net import dns

QUERY = bytes.fromhex(
    "abcd0100000100000000000003777777076578616d706c6503636f6d0000010001"
)
ANSWER = QUERY[:2] + bytes.fromhex(
    "8180000100010000000003777777076578616d706c6503636f6d00000100010000010000047f000001"
)


def test_upstream_from_resolv_reads_the_first_nameserver(tmp_path: Path) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text(
        "# comment\nsearch example.com\nnameserver 10.1.1.1\nnameserver 10.1.1.2\n"
    )
    assert dns.upstream_from_resolv(conf) == ("10.1.1.1", dns.DNS_PORT)


def test_upstream_from_resolv_without_nameservers(tmp_path: Path) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text("search example.com\n")
    assert dns.upstream_from_resolv(conf) is None
    assert dns.upstream_from_resolv(tmp_path / "missing.conf") is None


@pytest.fixture
async def pair(tmp_path: Path):
    """A forwarder on a localhost socket plus its client and upstream."""
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    # Non-blocking: the test's answerer awaits it on the event loop,
    # and a blocking socket handed to sock_recvfrom wedges the loop.
    upstream.setblocking(False)
    forwarder = dns.DnsForwarder(
        upstream.getsockname(), 1.0, bind=("127.0.0.1", 0), client_ip="127.0.0.1"
    )
    await forwarder.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(2.0)
    serve = asyncio.create_task(forwarder.serve())
    try:
        yield forwarder, client, upstream
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()


async def test_serve_relays_query_and_answer(pair) -> None:
    forwarder, client, upstream = pair

    async def answer_upstream() -> None:
        loop = asyncio.get_running_loop()
        data, requester = await loop.sock_recvfrom(upstream, 4096)
        assert data == QUERY
        await loop.sock_sendto(upstream, ANSWER, requester)

    relay = asyncio.create_task(answer_upstream())
    await asyncio.to_thread(client.sendto, QUERY, forwarder._sock.getsockname())
    reply, _addr = await asyncio.to_thread(client.recvfrom, 4096)
    assert reply == ANSWER
    await asyncio.wait_for(relay, 2.0)


async def test_serve_stays_silent_when_upstream_times_out(pair) -> None:
    forwarder, client, _upstream = pair
    await asyncio.to_thread(client.sendto, QUERY, forwarder._sock.getsockname())
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(client.recvfrom, 4096)


async def test_serve_stops_when_the_socket_closes() -> None:
    forwarder = dns.DnsForwarder(("127.0.0.1", 1), 1.0, bind=("127.0.0.1", 0))
    await forwarder.start()
    serve = asyncio.create_task(forwarder.serve())
    forwarder._sock.close()
    await asyncio.wait_for(serve, 2.0)
    forwarder.stop()


async def test_stop_cancels_in_flight_relays() -> None:
    forwarder = dns.DnsForwarder(
        ("127.0.0.1", 1), 30.0, bind=("127.0.0.1", 0), client_ip="127.0.0.1"
    )
    await forwarder.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    serve = asyncio.create_task(forwarder.serve())
    await asyncio.to_thread(client.sendto, QUERY, forwarder._sock.getsockname())
    await asyncio.sleep(0.1)  # the relay task exists and is waiting
    forwarder.stop()
    serve.cancel()
    client.close()
    await asyncio.sleep(0)  # cancellations observed; no exception surfaced


async def test_stop_is_idempotent_and_serve_ends_without_a_socket() -> None:
    forwarder = dns.DnsForwarder(("127.0.0.1", 1), 1.0)
    forwarder.stop()  # never started: a no-op, not an error
    await asyncio.wait_for(forwarder.serve(), 2.0)  # no socket: returns


async def test_serve_drops_queries_from_other_sources(tmp_path: Path) -> None:
    """The reflection guard (#70 review): only the tap's own guest is
    answered — a spoofed-source datagram is never relayed upstream."""
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.setblocking(False)
    forwarder = dns.DnsForwarder(
        upstream.getsockname(), 0.5, bind=("127.0.0.1", 0), client_ip="172.31.0.1"
    )
    await forwarder.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(0.5)
    serve = asyncio.create_task(forwarder.serve())
    try:
        # This client's source is 127.0.0.1, not the pinned guest IP.
        await asyncio.to_thread(client.sendto, QUERY, forwarder._sock.getsockname())
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(client.recvfrom, 4096)
        # And nothing reached the upstream either.
        loop = asyncio.get_running_loop()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(upstream, 4096), 0.5)
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()
