"""Tests over the vendored NFQUEUE binding (#542).

The kernel-facing paths (bind against a real queue) need
CAP_NET_ADMIN — the egress smokes exercise them end to end. These
tests pin the binding's own logic deterministically: the fail-closed
default verdict, retain's deferral, the one-shot verdict rule, the
closed-queue no-op, the trampoline's exception plumbing, and the
bind/run/unbind sequence against a fake library and a pipe fd.
"""

import ctypes
import errno
import os
import re
import sys
from ctypes import c_void_p
from pathlib import Path

import pytest

if sys.platform != "linux":
    pytest.skip(
        "nfqbind loads libnetfilter_queue at import, and the Linux "
        "kernel interface is absent here — shipped code guards the "
        "same import (nfq.py), and these tests pin that binding",
        allow_module_level=True,
    )

from msks.net import nfqbind
from msks.net.nfqbind import (
    ACCEPT,
    DROP,
    NetfilterQueue,
    Packet,
    deliver,
    open_library,
)


class RecordingQueue:
    """The queue seam the binding verdicts through."""

    def __init__(self) -> None:
        self.verdicts: list[tuple[int, int]] = []
        self.closed = False

    def issue_verdict(self, packet_id: int, verdict: int) -> None:
        if self.closed:
            raise AssertionError("verdict after close")
        self.verdicts.append((packet_id, verdict))


def test_open_library_loads_the_platform_library() -> None:
    """The dev/CI shells carry the library (#542): the loader
    answers a CDLL, the same object the module bound at import."""
    assert isinstance(open_library(), ctypes.CDLL)


def test_verdicts_are_one_shot() -> None:
    rec = RecordingQueue()
    packet = Packet(rec, 5, b"x")
    packet.accept()
    packet.drop()
    assert rec.verdicts == [(5, ACCEPT)]


def test_deliver_drops_an_unanswered_packet() -> None:
    """The fail-closed default: a callback that neither verdicts
    nor retains drops — an unanswered queue is a drop, not a
    pass."""
    rec = RecordingQueue()
    deliver(rec, lambda pkt: None, 3, b"x")
    assert rec.verdicts == [(3, DROP)]


def test_deliver_honors_the_verdict_issued_in_callback() -> None:
    rec = RecordingQueue()

    def accept(pkt: Packet) -> None:
        pkt.accept()

    deliver(rec, accept, 3, b"x")
    assert rec.verdicts == [(3, ACCEPT)]


def test_deliver_retain_defers_the_verdict() -> None:
    """retain() holds the packet for a later verdict — the deferred
    decide path: nothing issues during the callback."""
    rec = RecordingQueue()
    held: list[Packet] = []

    def hold(pkt: Packet) -> None:
        pkt.retain()
        held.append(pkt)

    deliver(rec, hold, 3, b"x")
    assert rec.verdicts == []
    assert held[0].get_payload() == b"x"
    held[0].drop()
    assert rec.verdicts == [(3, DROP)]


def test_deliver_propagates_the_callback_exception_after_the_drop() -> None:
    """A buggy handler cannot strand the packet: the default drop
    lands first, then the exception reaches run()'s caller (the
    consumer's drain guard depends on that order)."""
    rec = RecordingQueue()

    def boom(pkt: Packet) -> None:
        raise ValueError("handler bug")

    with pytest.raises(ValueError, match="handler bug"):
        deliver(rec, boom, 3, b"x")
    assert rec.verdicts == [(3, DROP)]


class FakeLib:
    """The ctypes seam NetfilterQueue drives, recording every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.handle = ctypes.c_void_p(101)
        self.queue_handle = ctypes.c_void_p(202)
        self.fd: int | None = None
        self.nfq_open = lambda: self._open()
        self.nfq_close = lambda h: self.calls.append("close")
        self.nfq_unbind_pf = lambda h, pf: self.calls.append("unbind_pf")
        self.nfq_bind_pf = lambda h, pf: self._bind_pf()
        self.copy_range: int | None = None
        self.nfq_create_queue = lambda h, num, cb, data: self._create_queue(
            num, cb
        )
        self.nfq_destroy_queue = lambda qh: self.calls.append("destroy")

        def set_mode(qh, mode, rng):
            self.copy_range = rng
            self.calls.append("set_mode")
            return 0

        self.nfq_set_mode = set_mode
        self.nfq_set_verdict = lambda qh, pid, verdict, ln, buf: (
            self.calls.append(f"verdict:{pid}:{verdict}")
        )
        self.nfq_fd = lambda h: self._fd()
        self.nfq_handle_packet = lambda h, buf, ln: self.calls.append(
            "handle_packet"
        )

    def _open(self):
        self.calls.append("open")
        return self.handle

    def _bind_pf(self):
        self.calls.append("bind_pf")
        return 0

    def _create_queue(self, num, cb):
        self.calls.append(f"create_queue:{num}")
        self.trampoline = cb
        return self.queue_handle

    def _fd(self):
        if self.fd is None:
            self.read_fd, self.write_fd = os.pipe()
            self.fd = self.read_fd
        return self.fd

    def close_pipe(self) -> None:
        if self.fd is not None:
            os.close(self.read_fd)
            os.close(self.write_fd)
            self.fd = None


@pytest.fixture()
def fake_lib(monkeypatch: pytest.MonkeyPatch):
    lib = FakeLib()
    monkeypatch.setattr(nfqbind, "lib", lib)
    yield lib
    lib.close_pipe()


def bound_queue(fake_lib: FakeLib, callback) -> NetfilterQueue:
    queue = NetfilterQueue()
    queue.bind(4, callback)
    return queue


def test_bind_drives_the_setup_sequence(fake_lib: FakeLib) -> None:
    """bind opens the library, clears and re-binds the family,
    attaches the queue, clamps the copy range to what one read
    carries, and wraps the fd the event loop will poll."""
    queue = bound_queue(fake_lib, lambda pkt: None)
    assert fake_lib.calls == [
        "open",
        "unbind_pf",
        "bind_pf",
        "create_queue:4",
        "set_mode",
    ]
    assert fake_lib.copy_range == nfqbind.COPY_RANGE
    # The derivation, not just the constant: a revert to the
    # unclamped 0xFFFF passes the line above and hangs every
    # packet the read cannot carry whole.
    assert nfqbind.COPY_RANGE == nfqbind.READ_BUFFER - 80
    assert queue.get_fd() == fake_lib.fd
    queue.unbind()
    assert fake_lib.calls[-2:] == ["destroy", "close"]


def test_run_processes_pending_messages_and_returns(fake_lib: FakeLib) -> None:
    """A readable fd hands its bytes to nfq_handle_packet until the
    read drains; run then returns (block=False), leaving the fd for
    the next readability callback."""
    queue = bound_queue(fake_lib, lambda pkt: None)
    os.write(fake_lib.write_fd, b"message")
    queue.run()
    assert "handle_packet" in fake_lib.calls
    queue.run()  # drained: returns on BlockingIOError
    queue.unbind()


def test_run_raises_the_stored_callback_exception(fake_lib: FakeLib) -> None:
    """A callback exception raised inside handle_packet propagates
    from run — the drain guard's contract."""
    queue = bound_queue(fake_lib, lambda pkt: None)
    os.write(fake_lib.write_fd, b"message")
    queue._pending_exc = ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        queue.run()
    queue.unbind()


def test_bind_refuses_an_unavailable_queue(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_lib.nfq_create_queue = lambda h, num, cb, data: None
    queue = NetfilterQueue()
    with pytest.raises(OSError, match="in use or CAP_NET_ADMIN"):
        queue.bind(4, lambda pkt: None)


def test_issue_verdict_no_ops_after_close(fake_lib: FakeLib) -> None:
    """A verdict on an already-delivered packet after unbind no-ops:
    the kernel dropped the destroyed queue's contents."""
    queue = bound_queue(fake_lib, lambda pkt: None)
    queue.unbind()
    queue.issue_verdict(9, DROP)
    assert not [c for c in fake_lib.calls if c.startswith("verdict")]


def test_on_message_delivers_and_drops_by_default(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trampoline extracts id and payload and runs the callback;
    an exception is stored (ctypes closures swallow them) for run
    to raise."""
    seen: list[bytes] = []
    queue = bound_queue(fake_lib, lambda pkt: seen.append(pkt.get_payload()))
    header = nfqbind.PacketHeader(packet_id=7)
    fake_lib.nfq_get_msg_packet_hdr = lambda nfa: ctypes.cast(
        ctypes.pointer(header), c_void_p
    )
    monkeypatch.setattr(nfqbind, "payload_of", lambda nfa: b"\x45\x00\x00")
    assert queue.on_message(None, None, object(), None) == 0
    assert seen == [b"\x45\x00\x00"]
    queue.unbind()


def test_on_message_stores_the_callback_exception(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(pkt: Packet) -> None:
        raise ValueError("handler bug")

    queue = bound_queue(fake_lib, boom)
    header = nfqbind.PacketHeader(packet_id=8)
    fake_lib.nfq_get_msg_packet_hdr = lambda nfa: ctypes.cast(
        ctypes.pointer(header), c_void_p
    )
    monkeypatch.setattr(nfqbind, "payload_of", lambda nfa: b"")
    assert queue.on_message(None, None, object(), None) == 0
    assert isinstance(queue._pending_exc, ValueError)
    queue.unbind()


def test_on_message_skips_a_headerless_message(fake_lib: FakeLib) -> None:
    """A malformed message (no packet header) reaches no callback —
    the kernel drops it with the queue's own default."""
    queue = bound_queue(fake_lib, lambda pkt: None)
    fake_lib.nfq_get_msg_packet_hdr = lambda nfa: None
    assert queue.on_message(None, None, object(), None) == 0
    assert fake_lib.calls.count("handle_packet") == 0
    queue.unbind()


def test_open_library_names_every_candidate_when_none_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import-time failure names both sonames — the refusal
    nfq.py's guard reports."""

    def refuse(soname, use_errno):
        raise OSError(f"no {soname}")

    monkeypatch.setattr(nfqbind.ctypes, "CDLL", refuse)
    with pytest.raises(OSError, match="libnetfilter_queue.so.1.*.so"):
        open_library()


def test_bind_refuses_when_the_library_handle_fails(
    fake_lib: FakeLib,
) -> None:
    fake_lib.nfq_open = lambda: None
    with pytest.raises(OSError, match="nfq_open failed"):
        NetfilterQueue().bind(4, lambda pkt: None)


def test_bind_refuses_when_the_family_bind_fails(
    fake_lib: FakeLib,
) -> None:
    fake_lib.nfq_bind_pf = lambda h, pf: -1
    with pytest.raises(OSError, match="nfq_bind_pf"):
        NetfilterQueue().bind(4, lambda pkt: None)
    # The half-open handle closed: no queue was ever created.
    assert "destroy" not in fake_lib.calls


def test_bind_refuses_when_the_copy_mode_fails(
    fake_lib: FakeLib,
) -> None:
    fake_lib.nfq_set_mode = lambda qh, mode, rng: -1
    with pytest.raises(OSError, match="nfq_set_mode"):
        NetfilterQueue().bind(4, lambda pkt: None)
    # The partial setup tore down: queue destroyed, handle closed.
    assert "destroy" in fake_lib.calls and "close" in fake_lib.calls


def test_run_retries_an_interrupted_read(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EINTR read retries — the loop's only allowed
    restart."""

    def interrupted(fd, n):
        interrupted.calls += 1
        if interrupted.calls == 1:
            raise InterruptedError()
        raise BlockingIOError()

    interrupted.calls = 0
    monkeypatch.setattr(nfqbind.os, "read", interrupted)
    queue = bound_queue(fake_lib, lambda pkt: None)
    queue.run()
    assert interrupted.calls == 2
    queue.unbind()


def test_on_message_keeps_the_first_stored_exception(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second callback exception while one is pending does not
    displace the first — the one run() will raise."""

    def boom(pkt: Packet) -> None:
        raise ValueError("second")

    queue = bound_queue(fake_lib, boom)
    header = nfqbind.PacketHeader(packet_id=9)
    fake_lib.nfq_get_msg_packet_hdr = lambda nfa: ctypes.cast(
        ctypes.pointer(header), c_void_p
    )
    monkeypatch.setattr(nfqbind, "payload_of", lambda nfa: b"")
    queue._pending_exc = ValueError("first")
    assert queue.on_message(None, None, object(), None) == 0
    assert isinstance(queue._pending_exc, ValueError)
    assert queue._pending_exc.args == ("first",)
    queue.unbind()


def test_unbind_on_a_never_bound_queue_is_a_no_op(fake_lib: FakeLib) -> None:
    """Teardown is idempotent: a queue that never bound (the
    consumer guards its stop path the same way) closes cleanly."""
    NetfilterQueue().unbind()
    assert fake_lib.calls == []


def test_packet_id_reads_big_endian(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id is a __be32 on the wire: the kernel's packet id 1
    sits in memory as 0x01000000 on a little-endian host, and the
    verdict must carry the host-order value (the swap discards
    every verdict — found against a real kernel queue in #543's
    review)."""
    queue = bound_queue(fake_lib, lambda pkt: None)
    header = nfqbind.PacketHeader(packet_id=0x01000000)
    fake_lib.nfq_get_msg_packet_hdr = lambda nfa: ctypes.cast(
        ctypes.pointer(header), c_void_p
    )
    monkeypatch.setattr(nfqbind, "payload_of", lambda nfa: b"")
    queue.on_message(None, None, object(), None)
    # deliver's default drop verdicted the swapped id's
    # host-order twin: the recorded verdict names 1.
    verdicts = [c for c in fake_lib.calls if c.startswith("verdict:")]
    assert verdicts == ["verdict:1:0"]
    queue.unbind()


def test_run_continues_past_a_buffer_overflow(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOBUFS is the kernel dropping queue messages under a
    burst: the kernel released the missed packets (fail-closed,
    upstream's posture), so the loop reads on instead of surfacing
    a drain failure."""

    def overflowing(fd, n):
        overflowing.calls += 1
        if overflowing.calls == 1:
            raise OSError(errno.ENOBUFS, "no buffer space")
        raise BlockingIOError()

    overflowing.calls = 0
    monkeypatch.setattr(nfqbind.os, "read", overflowing)
    queue = bound_queue(fake_lib, lambda pkt: None)
    queue.run()
    assert overflowing.calls == 2
    queue.unbind()


def test_run_surfaces_an_unrelated_read_error(
    fake_lib: FakeLib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EINTR, EAGAIN, and ENOBUFS have meanings; every other read
    error surfaces out of run — the drain guard logs it."""

    def failing(fd, n):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(nfqbind.os, "read", failing)
    queue = bound_queue(fake_lib, lambda pkt: None)
    with pytest.raises(OSError, match="input/output error"):
        queue.run()
    queue.unbind()


def test_every_library_call_has_a_prototype() -> None:
    """Every lib.nfq_* name the module calls carries argtypes (and
    the pointer-returning trio carries a c_void_p restype): a call
    without argtypes passes a Python int where the library takes a
    pointer, and ctypes truncates it — the missing nfq_close
    prototype segfaulted every unbind (#543's round-2 review). The
    FakeLib-based tests cannot see this; this pin reads the
    module's own source."""
    source = nfqbind.__file__
    called = set(re.findall(r"lib\.(nfq_\w+)\(", Path(source).read_text()))
    assert called, "the source scan found no calls — the pin broke"
    for name in sorted(called):
        func = getattr(nfqbind.lib, name, None)
        assert func is not None, f"lib.{name} is called but missing"
        assert func.argtypes is not None, (
            f"lib.{name} carries no argtypes — ctypes passes a "
            "truncated pointer where the library takes a handle"
        )
    for name in ("nfq_open", "nfq_create_queue", "nfq_get_msg_packet_hdr"):
        assert getattr(nfqbind.lib, name).restype == ctypes.c_void_p, (
            f"lib.{name} must return c_void_p"
        )
