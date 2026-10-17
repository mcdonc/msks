"""The vendored NFQUEUE binding (#542): ctypes over the platform's
``libnetfilter_queue`` — the five-call slice the consent loop uses,
with the same observable semantics the Cython ``netfilterqueue``
package had:

- ``bind(queue_num, callback)`` attaches one queue; the callback
  receives a :class:`Packet` per queued message.
- ``get_fd()`` is the queue's socket fd (drive it from an event
  loop); ``run(block=False)`` processes pending messages and
  returns when none remain.
- ``unbind()`` tears the queue down; queued packets the kernel
  still holds drop, as they do at the Cython binding's teardown.
- A packet carries ``get_payload()``/``accept()``/``drop()``/
  ``retain()``. A callback that issues no verdict and retains
  nothing is DROPped — the fail-closed default; ``retain()`` holds
  the packet for a later verdict (the deferred decide path).
- An exception the callback raises propagates out of ``run()``
  (the consumer's drain guard depends on it).

The library loads once at import (``open_library``): a host
without it — every non-Linux one — fails the import with the
OSError naming the sonames, which ``nfq.py``'s guard turns into
the named refusal at bind time. Nothing here compiles, so the msks
wheel ships pure on every platform; the runtime contract is the
soname load (``msksd doctor`` probes it), and the library stays
the platform's own (the FIPS posture: msks pins no crypto, and
this binding adds no algorithm of its own).
"""

import ctypes
import os
import socket
from ctypes import (
    CFUNCTYPE,
    POINTER,
    c_char_p,
    c_int,
    c_ubyte,
    c_uint8,
    c_uint16,
    c_uint32,
    c_void_p,
)

# The verdicts the kernel knows; the values are the module's own
# vocabulary, not the library's headers' (NF_DROP/NF_ACCEPT).
DROP = 0
ACCEPT = 1

# Copy whole packets (the consent loop parses the L3 payload);
# 0xFFFF is the "no snap length" range the library documents.
COPY_PACKET = 2
COPY_RANGE = 0xFFFF

# One queued SYN fits many times over; a truncated read surfaces
# as a handle_packet error in the drain guard, never a hang.
READ_BUFFER = 16384

_SONAMES = ("libnetfilter_queue.so.1", "libnetfilter_queue.so")

# int cb(struct nfq_q_handle *, struct nfgenmsg *,
#        struct nfq_data *, void *) — the message trampoline.
QUEUE_CALLBACK = CFUNCTYPE(c_int, c_void_p, c_void_p, c_void_p, c_void_p)


def open_library():
    """Load the platform's libnetfilter_queue, trying each soname.

    Raises the OSError naming every candidate when none loads —
    the import-time failure ``nfq.py``'s guard catches.
    """
    errors = []
    for soname in _SONAMES:
        try:
            return ctypes.CDLL(soname, use_errno=True)
        except OSError as exc:
            errors.append(f"{soname}: {exc}")
    detail = "libnetfilter_queue not loadable (" + "; ".join(errors) + ")"
    raise OSError(detail)


lib = open_library()

# --- prototypes -----------------------------------------------------------

lib.nfq_open.restype = c_void_p
lib.nfq_open.argtypes = []

lib.nfq_unbind_pf.restype = c_int
lib.nfq_unbind_pf.argtypes = [c_void_p, c_uint16]

lib.nfq_bind_pf.restype = c_int
lib.nfq_bind_pf.argtypes = [c_void_p, c_uint16]

lib.nfq_create_queue.restype = c_void_p
lib.nfq_create_queue.argtypes = [
    c_void_p,
    c_uint16,
    QUEUE_CALLBACK,
    c_void_p,
]

lib.nfq_destroy_queue.restype = c_int
lib.nfq_destroy_queue.argtypes = [c_void_p]

lib.nfq_set_mode.restype = c_int
lib.nfq_set_mode.argtypes = [c_void_p, c_uint8, c_uint32]

lib.nfq_set_verdict.restype = c_int
lib.nfq_set_verdict.argtypes = [
    c_void_p,
    c_uint32,
    c_uint32,
    c_uint32,
    c_char_p,
]

lib.nfq_handle_packet.restype = c_int
lib.nfq_handle_packet.argtypes = [c_void_p, c_char_p, c_int]

lib.nfq_fd.restype = c_int
lib.nfq_fd.argtypes = [c_void_p]

lib.nfq_get_msg_packet_hdr.restype = c_void_p
lib.nfq_get_msg_packet_hdr.argtypes = [c_void_p]

lib.nfq_get_payload.restype = c_int
lib.nfq_get_payload.argtypes = [c_void_p, POINTER(POINTER(c_ubyte))]

lib.nfq_close.restype = c_int
lib.nfq_close.argtypes = [c_void_p]


class PacketHeader(ctypes.Structure):
    """``struct nfq_msg_packet_hdr`` — the packet id alone is read
    (the verdict key); hw_protocol and hook ride unread."""

    _fields_ = [
        ("packet_id", c_uint32),
        ("hw_protocol", c_uint16),
        ("hook", c_uint16),
    ]


def packet_id_of(nfa):
    """The nfq_data's packet id, or None when the header is absent
    (a malformed message — the caller drops it)."""
    header = lib.nfq_get_msg_packet_hdr(nfa)
    if not header:
        return None
    return ctypes.cast(header, POINTER(PacketHeader)).contents.packet_id


def payload_of(nfa):  # pragma: no cover — needs a real nfq_data
    # from the kernel queue; the egress smokes run this path and
    # the unit suite stubs it (the trampoline tests inject bytes).
    """The nfq_data's L3 payload as bytes — copied at callback
    time, so the bytes outlive the message the way the Cython
    binding's Packet did."""
    data = POINTER(c_ubyte)()
    length = lib.nfq_get_payload(nfa, ctypes.byref(data))
    if length < 0:
        return b""
    return ctypes.string_at(data, length)


class Packet:
    """One queued packet. Verdicts are one-shot; after ``unbind``
    they no-op (the kernel drops a destroyed queue's contents, so a
    late verdict has nothing to act on)."""

    def __init__(self, queue, packet_id: int, payload: bytes) -> None:
        self._queue = queue
        self._packet_id = packet_id
        self._payload = payload
        self.verdict_issued = False
        self.retained = False

    def get_payload(self) -> bytes:
        return self._payload

    def retain(self) -> None:
        self.retained = True

    def accept(self) -> None:
        self.issue(ACCEPT)

    def drop(self) -> None:
        self.issue(DROP)

    def issue(self, verdict: int) -> None:
        if self.verdict_issued:
            return
        self.verdict_issued = True
        self._queue.issue_verdict(self._packet_id, verdict)


def deliver(queue, callback, packet_id: int, payload: bytes) -> None:
    """One callback delivery: run the callback, then apply the
    fail-closed default — a packet the callback neither verdicted
    nor retained drops (an unanswered queue is a drop, not a
    pass). An exception the callback raises propagates after the
    default drop, so a buggy handler cannot strand the packet."""
    packet = Packet(queue, packet_id, payload)
    try:
        callback(packet)
    finally:
        if not packet.verdict_issued and not packet.retained:
            packet.drop()


class NetfilterQueue:
    """One queue: bind, drive the fd, process pending messages."""

    def __init__(self) -> None:
        self._handle = None
        self._queue_handle = None
        self._callback = None
        self._trampoline = None
        self._fd = -1
        self._pending_exc = None

    def bind(self, queue_num: int, callback) -> None:
        """Open the library, attach queue ``queue_num``, copy whole
        packets, and hand every message to ``callback``. The
        unbind/bind PF pair is the library's own setup idiom (it
        clears a previous registration before installing this
        one)."""
        handle = lib.nfq_open()
        if not handle:
            raise OSError(ctypes.get_errno(), "nfq_open failed")
        self._handle = handle
        lib.nfq_unbind_pf(handle, socket.AF_INET)
        if lib.nfq_bind_pf(handle, socket.AF_INET) < 0:
            self.close()
            raise OSError(ctypes.get_errno(), "nfq_bind_pf(AF_INET) failed")
        self._callback = callback
        self._trampoline = QUEUE_CALLBACK(self.on_message)
        queue_handle = lib.nfq_create_queue(
            handle, queue_num, self._trampoline, None
        )
        if not queue_handle:
            errno = ctypes.get_errno()
            self.close()
            raise OSError(
                errno,
                f"nfq_create_queue({queue_num}) failed — the queue is "
                "in use or CAP_NET_ADMIN is missing",
            )
        self._queue_handle = queue_handle
        if lib.nfq_set_mode(queue_handle, COPY_PACKET, COPY_RANGE) < 0:
            self.close()
            raise OSError(
                ctypes.get_errno(), "nfq_set_mode(COPY_PACKET) failed"
            )
        self._fd = lib.nfq_fd(handle)
        # Nonblocking, as an event loop wants its readers: an empty
        # queue raises BlockingIOError out of os.read.
        os.set_blocking(self._fd, False)

    def get_fd(self) -> int:
        """The queue's fd — readable when messages pend."""
        return self._fd

    def run(self, block: bool = True) -> None:
        """Read queue messages until none pend (``block=False``) or
        forever (``block=True``). A callback exception raised inside
        ``nfq_handle_packet`` propagates from here."""
        while True:
            try:
                buf = os.read(self._fd, READ_BUFFER)
            except InterruptedError:
                continue
            except BlockingIOError:
                return
            lib.nfq_handle_packet(self._handle, buf, len(buf))
            if self._pending_exc is not None:
                exc, self._pending_exc = self._pending_exc, None
                raise exc

    def unbind(self) -> None:
        """Tear the queue down. Queued packets the kernel still
        holds drop; verdicts on already-delivered packets no-op."""
        self.close()

    def close(self) -> None:
        queue_handle, self._queue_handle = self._queue_handle, None
        if queue_handle:
            lib.nfq_destroy_queue(queue_handle)
        handle, self._handle = self._handle, None
        if handle:
            lib.nfq_close(handle)
        self._trampoline = None
        self._callback = None

    def issue_verdict(self, packet_id: int, verdict: int) -> None:
        if self._queue_handle is None:
            return
        lib.nfq_set_verdict(self._queue_handle, packet_id, verdict, 0, None)

    def on_message(self, _qh, _nfmsg, nfa, _data):
        """The C trampoline: extract the message, deliver it, keep
        the callback's exception for ``run`` to raise (ctypes
        closures swallow exceptions — storing is the only way
        out)."""
        packet_id = packet_id_of(nfa)
        if packet_id is None:
            return 0
        try:
            deliver(self, self._callback, packet_id, payload_of(nfa))
        except BaseException as exc:  # re-raised by run()
            if self._pending_exc is None:
                self._pending_exc = exc
        return 0


lib.nfq_close.restype = c_int
lib.nfq_close.argtypes = [c_void_p]
