"""Egress spec validation and matching (#69), ported from klangk,
plus the consent lifecycle vocabulary every consent speaker shares
(#387): decision and duration words and the decider-facing row
shape, kept below the model so the layers above — model, consent,
net, server — import one vocabulary instead of each other. #401
added the decided side: the verdict TTL resolution and the pin
shape consent hands the data plane, so ``net`` consumes final
enforcement values and no consent module.

A workspace's static allowlist is a list of specs:

- ``host`` / ``host:port`` — a DNS name (or an IPv4 literal); a
  bare host is exact (apex only), a leading ``.`` includes
  subdomains, ``*.`` matches subdomains only.
- ``10.0.0.0/8`` / ``10.0.0.0/8:443`` — an IPv4 CIDR, optionally
  port-scoped.

The grammar and the matching semantics are klangk's
(``netfilter.py`` + the sidecar's ``allowlist.py``): permissive
enough for the API boundary to reject only gross mistakes, exact
enough that a typo cannot broaden a rule. The enforcement split is
msks's own: name specs gate at the daemon's resolver, CIDR and
IP-literal specs accept in the per-VM nftables chain.
"""

import ipaddress
import re
from dataclasses import dataclass

# The three egress modes (#69). ``allow`` is the default-permit
# posture #52 shipped (egress unconstrained; the naming layer still
# records). ``static`` allows only the allowlist. ``interactive``
# holds each new flow's first packet for a decider verdict.
MODE_STATIC = "static"
MODE_INTERACTIVE = "interactive"
MODE_ALLOW = "allow"
EGRESS_MODES = (MODE_STATIC, MODE_INTERACTIVE, MODE_ALLOW)

# nginx-style host scopes, klangk #2377: bare = apex only, ``.`` =
# apex + subdomains, ``*.`` = subdomains only.
SCOPE_EXACT = "exact"
SCOPE_INCLUSIVE = "inclusive"
SCOPE_SUBDOMAINS = "subdomains"

# A hostname or IPv4 literal with an optional trailing ``:port``.
_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?"  # hostname / IPv4
    r"(?::[0-9]{1,5})?$"  # optional :port
)


def is_port_digits(text: str) -> bool:
    """True for 1–5 ASCII digits — the port grammar.

    ``str.isdigit()`` alone admits Unicode digit forms that iptables
    and nft reject; ASCII-only keeps the grammar to what the kernel
    parses (klangk #3274)."""
    return bool(text) and text.isascii() and text.isdigit() and len(text) <= 5


def valid_port_suffix(spec: str) -> bool:
    """Whether a ``:port`` suffix (when present) names a real port."""
    if ":" not in spec:
        return True
    port = spec.rsplit(":", 1)[1]
    return is_port_digits(port) and int(port) <= 65535


def strip_scope_sigil(spec: str) -> tuple[str, str]:
    """``(host, scope)`` for a sigil-stripped host spec.

    A bare ``*``, ``*.``, or ``.`` has no matchable base and returns
    ``("", …)`` — invalid."""
    if spec.startswith("*."):
        return spec[2:], SCOPE_SUBDOMAINS
    if spec.startswith("."):
        return spec[1:], SCOPE_INCLUSIVE
    return spec, SCOPE_EXACT


def valid_cidr_spec(spec: str) -> bool:
    """Whether ``<ip>/<plen>[:port]`` is a valid IPv4 CIDR spec.

    IPv6 is refused: the guest's only route is the host-side
    /30, so a v6 destination is neither reachable nor enforceable —
    the same posture klangk took (#1936). Host bits are kept
    as-typed (nft masks them). A spec without a prefix length is
    not a CIDR (the host grammar owns bare addresses)."""
    if "/" not in spec:
        return False
    cidr, port = split_port(spec)
    if port is not None and port > 65535:
        return False
    try:
        ipaddress.IPv4Network(cidr, strict=False)
    except ValueError:
        return False
    return True


def valid_spec(spec: str) -> bool:
    """Whether one raw allowlist entry parses."""
    stripped = spec.strip()
    if not stripped or has_space(stripped):
        return False
    if "/" in stripped:
        return valid_cidr_spec(stripped)
    return valid_host_spec(stripped)


def has_space(text: str) -> bool:
    """Whether any character is whitespace (the one gross mistake
    the boundary rejects; the resolver and chain do the real
    matching)."""
    return any(ch.isspace() for ch in text)


def valid_host_spec(stripped: str) -> bool:
    """A name (or IP-literal) spec against the host grammar."""
    host, _scope = strip_scope_sigil(stripped)
    if not host:
        return False
    return bool(_DOMAIN_RE.match(host)) and valid_port_suffix(host)


def parse_allowlist(values: list[str]) -> tuple[str, ...]:
    """Validate, strip, and de-duplicate an allowlist.

    Preserves first-seen order; raises :class:`ValueError` naming
    every invalid entry so the API surfaces a precise error instead
    of silently skipping one. A ``/0`` CIDR (all of IPv4) is valid
    but logged loudly by the caller's chain builder — an operator
    stumbling into it should see it, not have it slip through."""
    out: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    for raw in values:
        collect_entry(raw, out, seen, invalid)
    if invalid:
        raise ValueError(
            "invalid egress allowlist entries: "
            + ", ".join(repr(s) for s in invalid)
        )
    return tuple(out)


def collect_entry(
    raw: str, out: list[str], seen: set[str], invalid: list[str]
) -> None:
    """Fold one raw entry into the parse's accumulators."""
    spec = raw.strip()
    if not spec:
        return
    if not valid_spec(spec):
        invalid.append(raw)
        return
    if spec not in seen:
        seen.add(spec)
        out.append(spec)


def is_ipv4(text: str) -> bool:
    """True when ``text`` is a literal IPv4 address."""
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address)
    except ValueError:
        return False


def split_port(spec: str) -> tuple[str, int | None]:
    """``(host_or_cidr, port)`` with a trailing ``:port`` split off.

    A non-numeric suffix stays part of the host (klangk's rule: the
    grammar already rejected such specs at the boundary; this is the
    trusted-input re-split)."""
    if ":" not in spec:
        return spec, None
    host, port = spec.rsplit(":", 1)
    if is_port_digits(port):
        return host, int(port)
    return spec, None


@dataclass(frozen=True)
class HostSpec:
    """One name-based spec, pre-split for the resolver gate."""

    host: str
    port: int | None
    scope: str


@dataclass(frozen=True)
class IpSpec:
    """One address-based spec (CIDR or literal), for the chain."""

    network: ipaddress.IPv4Network
    port: int | None

    def matches(self, address: str) -> bool:
        return ipaddress.IPv4Address(address) in self.network


@dataclass(frozen=True)
class EgressPolicy:
    """A workspace's enforcement posture, parsed from its row.

    ``mode`` picks the chain shape and the resolver gate; the specs
    split into name specs (the resolver's business) and address
    specs (the chain's), so each enforcement point sees only the
    specs it can actually enforce."""

    workspace_id: str
    mode: str
    specs: tuple[str, ...]

    @classmethod
    def from_row(cls, row: dict | None) -> EgressPolicy | None:
        """The policy for a workspace row, or None when the row is
        absent (the caller's not-found signal)."""
        if row is None:
            return None
        return cls(
            workspace_id=row["id"],
            mode=row.get("egress_mode") or MODE_ALLOW,
            specs=tuple(row.get("egress_allowlist") or ()),
        )

    @property
    def gated(self) -> bool:
        """Whether this posture gates egress at all (static and
        interactive do; allow is the default-permit #52 shape)."""
        return self.mode in (MODE_STATIC, MODE_INTERACTIVE)

    @property
    def interactive(self) -> bool:
        return self.mode == MODE_INTERACTIVE

    @property
    def host_specs(self) -> tuple[HostSpec, ...]:
        """The name specs, sigil-split and lowercased."""
        out: list[HostSpec] = []
        for spec in self.specs:
            if "/" in spec:
                continue
            host, port = split_port(spec)
            stripped, scope = strip_scope_sigil(host.lower())
            if stripped and not is_ipv4(stripped):
                out.append(HostSpec(stripped, port, scope))
        return tuple(out)

    @property
    def ip_specs(self) -> tuple[IpSpec, ...]:
        """The address specs (CIDRs and IP literals) for the chain."""
        out: list[IpSpec] = []
        for spec in self.specs:
            base, port = split_port(spec)
            if "/" in base:
                out.append(IpSpec(ipaddress.IPv4Network(base), port))
            elif is_ipv4(base):
                out.append(IpSpec(ipaddress.IPv4Network(base), port))
        return tuple(out)


def host_matches(qname: str, host: str, scope: str) -> bool:
    """Whether ``qname`` matches ``host`` under ``scope``.

    The suffix check requires the dot, so ``evilexample.com`` does
    not match ``example.com``. An unknown scope falls back to exact
    — the narrow direction."""
    if scope == SCOPE_SUBDOMAINS:
        return qname.endswith("." + host)
    if scope == SCOPE_INCLUSIVE:
        return qname == host or qname.endswith("." + host)
    return qname == host


def ports_for(qname: str, specs: tuple[HostSpec, ...]) -> set[int] | None:
    """The ports a name is allowed on under ``specs``.

    ``None`` — a port-less spec matched (all ports). ``set()`` — no
    spec matched (deny). ``{443, …}`` — exactly these ports. The
    caller (the resolver gate) treats an empty set as deny; keeping
    the two apart is the fail-closed bug klangk unit-tested against
    (#2256)."""
    ports: set[int] = set()
    for spec in specs:
        if not host_matches(qname, spec.host, spec.scope):
            continue
        if spec.port is None:
            return None
        ports.add(spec.port)
    return ports


def covers(ports: set[int] | None) -> bool:
    """Whether a ports answer is a match: None is an all-ports
    match, a non-empty set matches those ports, and an empty set
    matches nothing (the fail-closed distinction klangk pinned —
    inverting it is the classic gate bug)."""
    return ports is None or bool(ports)


def allow_all_cidrs(specs: tuple[IpSpec, ...]) -> tuple[str, ...]:
    """The ``/0`` specs in an address-spec list (for the loud log)."""
    return tuple(
        f"{spec.network}/{spec.port}" if spec.port else f"{spec.network}"
        for spec in specs
        if spec.network.prefixlen == 0
    )


# --- consent lifecycle (#69, relocated #387) --------------------------------
#
# One row per consented destination moves
# ``pending → allowed | denied | expired | revoked``:
# ``allowed``/``denied`` a decider's verdict or a policy record,
# ``expired`` a hold that timed out, ``revoked`` a verdict undone.

DECISION_PENDING = "pending"
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
DECISION_EXPIRED = "expired"
DECISION_REVOKED = "revoked"
DECISIONS = (
    DECISION_PENDING,
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_EXPIRED,
    DECISION_REVOKED,
)

# Durations (how long enforcement honors a verdict): ``once`` (this
# connection), ``5m``, ``15m``, ``tilrestart`` (until the workspace
# VM stops — the flow rules die with the per-VM table), ``forever``
# (the workspace's lifetime — replayed at every attach).
DURATION_ONCE = "once"
DURATION_5M = "5m"
DURATION_15M = "15m"
DURATION_TILRESTART = "tilrestart"
DURATION_FOREVER = "forever"
DURATIONS = (
    DURATION_ONCE,
    DURATION_5M,
    DURATION_15M,
    DURATION_TILRESTART,
    DURATION_FOREVER,
)
DURATION_DEFAULT = DURATION_TILRESTART

#: Timed durations in seconds; ``once``/``tilrestart``/``forever``
#: are not time-bounded (they are governed by connection, table
#: lifetime, and the row itself).
DURATION_SECONDS = {DURATION_5M: 300, DURATION_15M: 900}

#: How long a ``once`` deny's fail-fast reject rule lives: enough to
#: catch the SYN's retransmit (one RTO), short enough that a new
#: connection to the same destination is not refused above the
#: queue (klangk's CONSENT_REJECT_TTL).
ONCE_REJECT_S = 10.0

#: The kernel's connect timeout is ~127 s (tcp_syn_retries); a hold
#: must answer inside it, and a session/flow rule standing in for
#: ``forever`` needs only outlive the table it dies with anyway.
LONG_TTL_S = 30 * 86400.0


def duration_ttl(duration: str) -> float | None:
    """Seconds a verdict's enforcement lives, or None for ``once``.

    ``tilrestart``/``forever`` map to a long TTL — the real boundary
    for the first is the per-VM table's deletion at stop, and for
    the second the row's replay at every attach."""
    if duration in DURATION_SECONDS:
        return float(DURATION_SECONDS[duration])
    if duration in (DURATION_TILRESTART, DURATION_FOREVER):
        return LONG_TTL_S
    return None


@dataclass(frozen=True)
class VerdictPin:
    """One decided verdict's kernel enforcement (#401): consent
    resolves a verdict to its final enforcement shape — action,
    destination, TTL — and the data plane installs it without
    speaking the verdict vocabulary it came from.

    Address-literal by construction: a name-keyed verdict enforces
    at the resolver gate, which reads its row live, so it never
    becomes a pin."""

    allowed: bool
    ip: str
    port: int | None
    ttl_s: float


def public_row(row: dict) -> dict:
    """A row dict without the verification-internal ``hmac`` tag —
    the shape that crosses to deciders."""
    return {k: v for k, v in row.items() if k != "hmac"}
