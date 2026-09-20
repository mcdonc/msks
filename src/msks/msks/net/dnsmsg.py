"""A minimal DNS wire codec: just what the naming layer needs (#69).

The consent engine names destinations by DNS name, so the forwarder
(:mod:`msks.net.dns`) must read the question out of a query, read
the A records (with their TTLs) out of an answer, and forge an
NXDOMAIN reply for an off-list name in static mode. A full resolver
library is not needed for those three operations — and pulling one
in would add a dependency the appliance image then carries.

Every parser is total: malformed input returns ``None`` (or an
empty list), never raises. A datagram the codec cannot read is
dropped by the caller, which is the fail-closed direction for a
resolver.
"""

import struct

# Header: id, flags, qd, an, ns, ar counts — 12 bytes.
HEADER_LEN = 12

# Resource-record fixed fields: type, class, ttl, rdlength.
RR_FIXED = struct.Struct(">HHIH")

# The rdlength an IPv4 A record carries.
A_RDATA_LEN = 4

# A name decoder bound: compression-pointer chains are capped so a
# hostile answer cannot loop the decoder through crafted pointers.
MAX_NAME_JUMPS = 16
MAX_NAME_LEN = 253


class Question:
    """One query's question section, decoded."""

    __slots__ = ("id", "name", "qtype", "qclass", "wire")

    def __init__(
        self, ident: int, name: str, qtype: int, qclass: int, wire: bytes
    ) -> None:
        self.id = ident
        self.name = name
        self.qtype = qtype
        self.qclass = qclass
        # The question section verbatim (header through the end of
        # the first question): the NXDOMAIN builder copies it, and
        # the answer cache keys on it.
        self.wire = wire


def decode_name(message: bytes, offset: int) -> tuple[str, int]:
    """The domain name at ``offset`` (lowercased, no trailing dot)
    and the offset after it.

    Follows compression pointers (an answer's owner names may use
    them). Returns ``("", 0)`` for a malformed or over-long name —
    the caller's "unreadable" signal.
    """
    labels: list[bytes] = []
    jumps = 0
    end = 0
    pos = offset
    while True:
        kind, value, after = name_token(message, pos)
        if kind is None:
            return "", 0
        if kind == "end":
            return finish_name(labels, end, after)
        pos, jumps, end = decode_step(
            message, kind, value, after, pos, labels, jumps, end
        )
        if pos == 0:
            return "", 0


def finish_name(labels: list[bytes], end: int, after: int) -> tuple[str, int]:
    """The finished name and its extent: ``after`` (the zero
    byte's own stop) unless a pointer already latched a wider
    one."""
    if end == 0:
        end = after
    return join_labels(labels), end


def decode_step(
    message: bytes,
    kind: str,
    value: int,
    after: int,
    pos: int,
    labels: list[bytes],
    jumps: int,
    end: int,
) -> tuple[int, int, int]:
    """One non-terminal token: a pointer hop or a label append.
    ``(0, 0, 0)`` is the malformed signal (a decoded name can never
    start at offset 0 — the header lives there)."""
    if kind == "ptr":
        return follow(value, after, pos, jumps, end)
    pos, ok = take_label(message, pos, value, labels)
    if not ok:
        return 0, jumps, end
    return pos, jumps, end


def follow(
    target: int, after: int, pos: int, jumps: int, end: int
) -> tuple[int, int, int]:
    """One compression-pointer hop — the jump bound is the loop
    detector, and ``end`` latches on the first hop (the name's
    extent in the original message)."""
    jumps += 1
    if end == 0:
        end = after
    if jumps > MAX_NAME_JUMPS:
        return 0, jumps, end
    return target, jumps, end


def name_token(message: bytes, pos: int) -> tuple[str, int, int]:
    """One name token at ``pos``: ``("end", 0, pos+1)`` for the
    zero byte, ``("ptr", target, pos+2)`` for a compression
    pointer, ``("label", length, pos)`` for a length byte, or
    ``(None, 0, 0)`` when the token is malformed."""
    if pos >= len(message):
        return None, 0, 0
    length = message[pos]
    if length == 0:
        return "end", 0, pos + 1
    if length & 0xC0 == 0xC0:
        return pointer_token(message, pos)
    if length & 0xC0:
        return None, 0, 0
    return "label", length, pos


def pointer_token(message: bytes, pos: int) -> tuple[str, int, int]:
    """The pointer token at ``pos``, or the malformed answer when
    the pointer itself is truncated."""
    if pos + 2 > len(message):
        return None, 0, 0
    target = int.from_bytes(message[pos : pos + 2], "big") & 0x3FFF
    return "ptr", target, pos + 2


def take_label(
    message: bytes, pos: int, length: int, labels: list[bytes]
) -> tuple[int, bool]:
    """Append one label's bytes; ``(next_pos, True)`` or
    ``(0, False)`` when it overruns the message or the length
    bound."""
    if pos + 1 + length > len(message):
        return 0, False
    if sum(len(label) + 1 for label in labels) + length > MAX_NAME_LEN:
        return 0, False
    labels.append(message[pos + 1 : pos + 1 + length])
    return pos + 1 + length, True


def join_labels(labels: list[bytes]) -> str:
    """The decoded, lowercased name."""
    return b".".join(labels).decode("latin-1").lower()


def parse_query(wire: bytes) -> Question | None:
    """The first question of a query datagram, or None when the
    message is too short, headerless, or carries no decodable
    question."""
    if len(wire) < HEADER_LEN:
        return None
    qdcount = int.from_bytes(wire[4:6], "big")
    if qdcount < 1:
        return None
    name, after = decode_name(wire, HEADER_LEN)
    if not name or after + 4 > len(wire):
        return None
    qtype = int.from_bytes(wire[after : after + 2], "big")
    qclass = int.from_bytes(wire[after + 2 : after + 4], "big")
    return Question(
        int.from_bytes(wire[0:2], "big"),
        name,
        qtype,
        qclass,
        bytes(wire[: after + 4]),
    )


def parse_a_records(wire: bytes) -> list[tuple[str, int]]:
    """``[(ip, ttl_seconds), ...]`` from an answer's A records.

    CNAME chains are transparent: the A records ride the same answer
    section, whatever owner names they carry. The TTL drives the
    learned-IP expiry, so it is returned per record. An empty list
    covers NODATA, malformed, and non-answer messages alike — the
    caller records nothing and relays verbatim.
    """
    if len(wire) < HEADER_LEN:
        return []
    pos = skip_questions(wire)
    if pos is None:
        return []
    return read_answers(wire, pos)


def read_answers(wire: bytes, pos: int) -> list[tuple[str, int]]:
    """Walk the answer section from ``pos``, collecting A records
    until the section ends or truncates."""
    ancount = int.from_bytes(wire[6:8], "big")
    records: list[tuple[str, int]] = []
    for _ in range(ancount):
        _, after = decode_name(wire, pos)
        if after == 0:
            return records
        record, pos = read_rr(wire, after)
        if record is not None:
            records.append(record)
        if pos is None:
            return records
    return records


def skip_questions(wire: bytes) -> int | None:
    """The offset past the question section, or None when it does
    not parse (the answer walk needs the real offset)."""
    pos = HEADER_LEN
    qdcount = int.from_bytes(wire[4:6], "big")
    for _ in range(qdcount):
        _, after = decode_name(wire, pos)
        if after == 0 or after + 4 > len(wire):
            return None
        pos = after + 4
    return pos


def read_rr(
    wire: bytes, after: int
) -> tuple[tuple[str, int] | None, int | None]:
    """One answer record at ``after`` (past its owner name):
    ``((ip, ttl), next_offset)`` for an A record, ``(None, next)``
    for any other type, and ``(None, None)`` when the record is
    truncated (the walk stops there)."""
    fixed = wire[after : after + RR_FIXED.size]
    if len(fixed) < RR_FIXED.size:
        return None, None
    rtype, _rclass, ttl, rdlength = RR_FIXED.unpack(fixed)
    rdata_at = after + RR_FIXED.size
    if rdata_at + rdlength > len(wire):
        return None, None
    return a_record(wire, rtype, ttl, rdlength, rdata_at), rdata_at + rdlength


def a_record(
    wire: bytes, rtype: int, ttl: int, rdlength: int, rdata_at: int
) -> tuple[str, int] | None:
    """The (ip, ttl) an A record carries at ``rdata_at``, or None
    for any other record type."""
    if rtype != 1 or rdlength != A_RDATA_LEN:
        return None
    raw = wire[rdata_at : rdata_at + A_RDATA_LEN]
    return (".".join(str(b) for b in raw), ttl)


def nxdomain_for(query: bytes) -> bytes:
    """An NXDOMAIN reply for ``query``.

    Copies the question verbatim and answers it with the header's QR
    and RD bits kept and RCODE 3 (NXDOMAIN) set. A malformed query
    (no decodable question) gets a bare header reply — the resolver
    on the guest treats that as a failure, which is the same
    fail-closed direction as dropping it.
    """
    question = parse_query(query)
    flags = 0x8183  # QR + RD + RCODE 3 (NXDOMAIN)
    if question is None:
        return query[:2] + struct.pack("!HHHHH", flags, 0, 0, 0, 0)
    header = struct.pack(
        "!HHHHHH",
        question.id,
        flags,
        1,  # qdcount: the copied question
        0,
        0,
        0,
    )
    return header + question.wire[HEADER_LEN:]


def rewrite_id(answer: bytes, ident: int) -> bytes:
    """``answer`` with its transaction id replaced by ``ident``.

    The answer cache stores one served answer per question; a second
    client asking the same question re-uses it under its own id (the
    id is the only per-client field of a cached reply)."""
    return struct.pack("!H", ident) + answer[2:]
