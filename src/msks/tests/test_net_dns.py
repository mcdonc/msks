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


@pytest.mark.filterwarnings(
    # uvloop 0.22.1 itself calls asyncio.iscoroutinefunction (removed
    # in 3.16); our code does not. Ignore until uvloop ships the fix.
    "ignore:.*'asyncio.iscoroutinefunction' is deprecated.*:DeprecationWarning"
)
def test_relay_timeout_teardown_under_uvloop() -> None:
    """A relay timeout cancels the recvfrom await under uvloop.

    The cancellation path — wait_for cancels, recvfrom's finally
    removes the reader — is exactly the machinery uvloop lacked a
    native awaitable for; this pins reader teardown on cancellation
    under the loop that broke, complementing the asyncio-side
    timeout tests.
    """
    import threading

    uvloop = pytest.importorskip("uvloop")

    async def scenario() -> bool:
        # An upstream that never answers: bound, never reading.
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        forwarder = dns.DnsForwarder(
            sink.getsockname(), 0.2, bind=("127.0.0.1", 0), client_ip="127.0.0.1"
        )
        await forwarder.start()
        serve = asyncio.create_task(forwarder.serve())
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        client.settimeout(2.0)
        client.sendto(QUERY, forwarder._sock.getsockname())
        # No reply arrives (upstream silent): the relay times out and
        # its reader must be gone — the client sees nothing.
        replied = True
        try:
            await asyncio.to_thread(client.recvfrom, 4096)
        except TimeoutError:
            replied = False
        # A second query proves the loop still dispatches after the
        # timed-out relay: the forwarder is alive, not wedged.
        client.sendto(QUERY, forwarder._sock.getsockname())
        still_alive = not serve.done()
        serve.cancel()
        forwarder.stop()
        client.close()
        sink.close()
        return replied is False and still_alive

    result: dict[str, bool] = {}

    def runner() -> None:
        result["ok"] = uvloop.run(scenario())

    t = threading.Thread(target=runner)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    assert result.get("ok") is True


async def test_relay_drops_a_failed_reply_send() -> None:
    """A failed reply send silences one query, not the forwarder.

    The reply sendto is synchronous now: a full buffer (BlockingIOError)
    must leave the forwarder dispatching — the client's resolver
    retries, and the retry gets its answer.
    """
    relayed: list[bytes] = []

    class DroppingReplySend(socket.socket):
        """The forwarder's socket: the first reply send fails."""

        def __init__(self) -> None:
            super().__init__(socket.AF_INET, socket.SOCK_DGRAM)
            self._sends = 0

        def sendto(self, data, *args):
            self._sends += 1
            if self._sends == 1:
                raise BlockingIOError("send buffer full")
            relayed.append(data)
            return super().sendto(data, *args)

    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.setblocking(False)
    forwarder = dns.DnsForwarder(
        upstream.getsockname(), 2.0, bind=("127.0.0.1", 0), client_ip="127.0.0.1"
    )
    await forwarder.start(sock=DroppingReplySend())
    serve = asyncio.create_task(forwarder.serve())
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(3.0)

    answers = {"n": 0}

    async def answer_upstream() -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, requester = await loop.sock_recvfrom(upstream, 4096)
            answers["n"] += 1
            await loop.sock_sendto(upstream, data, requester)

    responder = asyncio.create_task(answer_upstream())
    try:
        client.sendto(QUERY, forwarder._sock.getsockname())
        # First relay's reply send fails: silence, not a crash.
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(client.recvfrom, 4096)
        assert not serve.done()
        client.sendto(QUERY, forwarder._sock.getsockname())
        reply, _addr = await asyncio.to_thread(client.recvfrom, 4096)
        assert reply == QUERY  # the responder echoes the query back
        assert len(relayed) == 1  # exactly one reply made it out
    finally:
        responder.cancel()
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()
