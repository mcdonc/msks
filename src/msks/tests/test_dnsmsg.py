"""The DNS wire codec (#69)."""

import struct

from msks.net import dnsmsg


def query(name: str, qtype: int = 1, ident: int = 0x1234) -> bytes:
    """A well-formed query datagram for one name."""
    qname = (
        b"".join(
            bytes([len(label)]) + label.encode() for label in name.split(".")
        )
        + b"\x00"
    )
    header = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0)
    return header + qname + struct.pack("!HH", qtype, 1)


def answer(
    name: str, ips: list[tuple[str, int]], ident: int = 0x1234
) -> bytes:
    """An answer datagram: one question plus A records with TTLs."""
    qname = (
        b"".join(
            bytes([len(label)]) + label.encode() for label in name.split(".")
        )
        + b"\x00"
    )
    header = struct.pack("!HHHHHH", ident, 0x8180, 1, len(ips), 0, 0)
    body = qname + struct.pack("!HH", 1, 1)
    for ip, ttl in ips:
        rec = (
            b"".join(
                bytes([len(label)]) + label.encode()
                for label in name.split(".")
            )
            + b"\x00"
        )
        addr = bytes(int(part) for part in ip.split("."))
        body += rec + struct.pack("!HHIH", 1, 1, ttl, 4) + addr
    return header + body


def test_parse_query_reads_the_question() -> None:
    parsed = dnsmsg.parse_query(query("Api.Example.COM"))
    assert parsed is not None
    assert parsed.name == "api.example.com"
    assert parsed.qtype == 1
    assert parsed.id == 0x1234
    assert parsed.wire.endswith(struct.pack("!HH", 1, 1))


def test_parse_query_rejects_malformed() -> None:
    assert dnsmsg.parse_query(b"") is None
    assert dnsmsg.parse_query(b"\x00" * 11) is None
    # No question: qdcount 0.
    empty = struct.pack("!HHHHHH", 1, 0x0100, 0, 0, 0, 0)
    assert dnsmsg.parse_query(empty) is None
    # A name that runs off the datagram.
    truncated = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + b"\x04api"
    assert dnsmsg.parse_query(truncated) is None


def test_parse_a_records_walks_answers() -> None:
    wire = answer(
        "cdn.example.com", [("203.0.113.7", 300), ("203.0.113.8", 60)]
    )
    assert dnsmsg.parse_a_records(wire) == [
        ("203.0.113.7", 300),
        ("203.0.113.8", 60),
    ]


def test_parse_a_records_skips_cnames_via_compression() -> None:
    """A CNAME answer whose A records use compression pointers still
    yields the A records (the answer section carries them)."""
    qname = b"\x03www\x07example\x03com\x00"
    header = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 2, 0, 0)
    question = qname + struct.pack("!HH", 1, 1)
    # CNAME: owner is a pointer to offset 12 (www.example.com);
    # rdata is a pointer to offset 16 (the "example" length byte,
    # i.e. example.com).
    cname = b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 300, 2) + b"\xc0\x10"
    a_record = (
        b"\xc0\x10"
        + struct.pack("!HHIH", 1, 1, 120, 4)
        + bytes([198, 51, 100, 4])
    )
    wire = header + question + cname + a_record
    assert dnsmsg.parse_a_records(wire) == [("198.51.100.4", 120)]


def test_parse_a_records_tolerates_garbage() -> None:
    assert dnsmsg.parse_a_records(b"nope") == []
    empty = struct.pack("!HHHHHH", 1, 0x8180, 1, 0, 0, 0)
    assert dnsmsg.parse_a_records(empty) == []


def test_nxdomain_copies_the_question_and_flags() -> None:
    q = query("off.list.example")
    reply = dnsmsg.nxdomain_for(q)
    assert reply[:2] == q[:2]  # the id
    assert reply[2:4] == b"\x81\x83"  # QR+RD, RCODE 3
    assert reply[12:] == q[12:]  # the question, verbatim


def test_nxdomain_for_malformed_stays_header_only() -> None:
    reply = dnsmsg.nxdomain_for(b"\x11\x22junk")
    assert len(reply) == dnsmsg.HEADER_LEN
    assert reply[:2] == b"\x11\x22"


def test_rewrite_id_swaps_the_transaction_id() -> None:
    cached = answer("x.example", [("10.0.0.1", 60)], ident=0xAAAA)
    rewritten = dnsmsg.rewrite_id(cached, 0xBBBB)
    assert rewritten[:2] == b"\xbb\xbb"
    assert rewritten[2:] == cached[2:]


def test_decode_name_bounds_pointer_chains() -> None:
    """A pointer loop is a decode error, not a hang."""
    looping = b"\x00" * 12 + b"\xc0\x0c"
    assert dnsmsg.decode_name(looping, 12) == ("", 0)


def test_decoder_arms() -> None:
    """The malformed-name guards: each truncation answers the
    unreadable signal, never an exception."""
    # A label that runs off the message.
    truncated = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + b"\x04api"
    assert dnsmsg.decode_name(truncated, 12) == ("", 0)
    assert dnsmsg.parse_query(truncated) is None
    # A name over the length bound.
    long_name = b"\x3f" + b"a" * 63
    long_name = long_name * 5  # 5 x 64 > MAX_NAME_LEN
    over = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + long_name + b"\x00"
    assert dnsmsg.decode_name(over, 12) == ("", 0)
    # A pointer past the message end.
    short_ptr = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + b"\xc0"
    assert dnsmsg.decode_name(short_ptr, 12) == ("", 0)
    # A bogus top-bit length byte (not a pointer, not a label).
    bogus = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + b"\x40"
    assert dnsmsg.decode_name(bogus, 12) == ("", 0)


def test_answer_walk_truncations() -> None:
    """Truncated answer sections yield what parsed, never raise."""
    good = answer("x.example", [("10.0.0.1", 60)])
    # A fixed-fields header that stops mid-record.
    cut = good[: len(good) - 2]
    assert dnsmsg.parse_a_records(cut) == []
    # An owner name that stops mid-decode in the answer section:
    # two claimed answers but only one present.
    two = answer("x.example", [("10.0.0.1", 60), ("10.0.0.2", 60)])
    trailing = two + b"\xc0"  # a dangling pointer for the second walk
    assert dnsmsg.parse_a_records(trailing) == [
        ("10.0.0.1", 60),
        ("10.0.0.2", 60),
    ]
    # A question section that does not parse answers nothing.
    bad_q = two[:4] + b"\x00\x02" + two[6:]
    assert dnsmsg.parse_a_records(bad_q) == []


def test_answer_walk_stops_at_dangling_names() -> None:
    """An answer section that claims more records than it carries:
    the dangling owner name answers empty-handed, the walk returns
    what it already parsed."""
    two = answer("x.example", [("10.0.0.1", 60), ("10.0.0.2", 60)])
    claimed_three = two[:6] + b"\x00\x03" + two[8:] + b"\xc0"
    assert dnsmsg.parse_a_records(claimed_three) == [
        ("10.0.0.1", 60),
        ("10.0.0.2", 60),
    ]


def test_read_rr_stops_inside_truncated_fixed_fields() -> None:
    """A record cut inside its type/class/ttl block yields nothing
    more from the walk."""
    good = answer("x.example", [("10.0.0.1", 60)])
    cut = good[: len(good) - 6]  # into the rdata tail + fixed block
    assert dnsmsg.parse_a_records(cut) == []


def test_multi_question_queries_are_refused() -> None:
    """The gate classifies one name; a datagram asking two must not
    relay verbatim past it (the daemon decides names, not the
    upstream)."""
    q1 = query("allowlisted.example")
    q2 = query("evil.example")
    packed = q1[:6] + b"\x00\x02" + q1[12:] + q2[12:]
    assert dnsmsg.parse_query(packed) is None
