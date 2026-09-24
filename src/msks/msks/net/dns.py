"""The egress DNS forwarder (#52), the consent naming layer (#69).

Each egress tap gets a forwarder bound to its host-side
address on port 53: the resolver DHCP offers, and — with the DNS
lockout in the per-VM chain — the only one the guest can reach.
Without a consent policy the forwarder relays verbatim, exactly as
#52 shipped it. With one (the :class:`ResolverGate` the manager
builds) it becomes the naming half of consent:

- static mode: allowlisted names resolve and their addresses are
  pinned as allowed (for the answer's TTL); every other name
  NXDOMAINs — no resolution oracle, no DNS exfil channel — and the
  denial is recorded.
- interactive mode: every name resolves; allowlisted, verdict-
  covered, and forever-allowed names pin their addresses, the rest
  only feed the name→address memory that names prompts
  (``api.anthropic.com:443``, not an IP).
- allow mode: every name resolves and feeds the memory; off-list
  names are recorded as allowed (the audit the default-permit
  posture can give).

Co-residency (#304): two names may resolve to the SAME address
(shared hosting, a CDN front). The naming memory keeps every
live name per address (the most recent names a flow), and an
address that carries more than one live name is *shared* — the
kernel's verdict pins are address-keyed and would let one name's
verdict cover its co-resident, so on a shared address the manager
pins nothing (verdicts gate at the queue, keyed by the most
recent resolution) and a deny answers only its own connection
(a per-flow RST). The pins an address already carried are
retracted the moment a second name resolves to it.

The query cache answers repeats locally (id rewritten per client),
and the name→address memory is what the NFQUEUE consumer's prompts
and revocations key on. Names expire with their DNS TTL (floored —
a 0-TTL answer must not yank the name a held SYN is about to
prompt with).
"""

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from ..consent.specs import MODE_ALLOW, MODE_STATIC, ports_for
from . import dnsmsg
from .loopio import recvfrom, sendto

DNS_PORT = 53

# A full-size DNS datagram: the biggest answer UDP carries.
MAX_DATAGRAM = 65535

# The name-memory floor: how long a learned name→address pairing
# lives even when the answer's TTL is 0 (klangk's MIN_TTL rule —
# the pairing must outlive the connection it is about to name).
NAME_TTL_FLOOR = 30.0

# The answer-cache bound (a blunt clear, like klangk's verdict
# cache: a flood must not grow the cache unboundedly).
CACHE_MAX = 512

# The naming-memory bound: one entry per resolved address, cleared
# wholesale past the bound — a guest resolving rotating wildcard
# names must not grow the daemon's memory for the workspace's
# life. The cost of a clear is prompt naming (addresses fall back
# to keying by IP until re-resolved), never enforcement.
NAMES_MAX = 4096

# A cached answer's floor TTL: a 0-TTL answer still serves the
# millisecond-scale repeats a resolver retry storm makes.
CACHE_TTL_FLOOR = 5.0


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


@dataclass(frozen=True)
class QueryDecision:
    """What the resolver gate decided for one query's name."""

    # "learn": resolve and pin the answer's addresses as allowed.
    # "record": resolve, feed the name memory, pin nothing.
    # "nxdomain": the name does not resolve here.
    action: str
    # The ports a learn pins (None = all ports).
    ports: set[int] | None = None
    # The rule-TTL cap for a learn (None = the answer's own TTL):
    # a verdict-covered name must not pin addresses past its
    # verdict (klangk's #2465 cap).
    cap: float | None = None


LEARN = "learn"
RECORD = "record"
NXDOMAIN = "nxdomain"


def covers(ports: set[int] | None) -> bool:
    """Whether a ports answer is a match: None is an all-ports
    match, a non-empty set matches those ports, and an empty set
    matches nothing (the fail-closed distinction klangk pinned —
    inverting it is the classic gate bug)."""
    return ports is None or bool(ports)


class ResolverGate:
    """The per-workspace DNS policy: what each queried name may do.

    Owned by the manager, handed to the workspace's forwarder. All
    decisions consult live state — the workspace's static specs,
    the engine's session memory, and the table's forever verdicts —
    so nothing here caches policy."""

    def __init__(self, policy, engine, model, net) -> None:
        self.policy = policy
        self.engine = engine
        self.model = model
        self.net = net

    @property
    def workspace_id(self) -> str:
        return self.policy.workspace_id

    async def classify(self, qname: str) -> QueryDecision:
        """The decision for one queried name (records policy rows —
        the audit trail of static denials and allow-mode allows —
        as a side effect of the answer they will get)."""
        workspace_id = self.workspace_id
        if await self.name_denied(workspace_id, qname):
            return QueryDecision(NXDOMAIN)
        ports = ports_for(qname, self.policy.host_specs)
        if covers(ports):
            return QueryDecision(LEARN, ports)
        return await self.session_or_mode(qname)

    async def name_denied(self, workspace_id: str, qname: str) -> bool:
        """Whether a deny covers the whole name: an all-ports
        session deny or a forever deny row (klangk's rejected-name
        rule — block more is the safe direction)."""
        if self.engine.session.deny_ttl(workspace_id, qname, 0) is not None:
            return True
        forever = await self.model.forever_verdict_for(workspace_id, qname)
        return forever is not None and forever["decision"] == "denied"

    async def session_or_mode(self, qname: str) -> QueryDecision:
        """Past the static specs: a session allow learns under its
        remaining window, then the forever/mode tail decides."""
        workspace_id = self.workspace_id
        session_ports = self.engine.session.allow_ports(workspace_id, qname)
        if covers(session_ports):
            cap = self.engine.session.min_allow_ttl(workspace_id, qname)
            return QueryDecision(LEARN, session_ports, cap)
        return await self.forever_or_mode(qname)

    async def forever_or_mode(self, qname: str) -> QueryDecision:
        """A forever allow row learns all ports; anything else is
        the mode tail's business."""
        if await self.model.forever_verdict_for(self.workspace_id, qname):
            # The only verdict left at this point is an allow.
            return QueryDecision(LEARN, None)
        return await self.mode_decision(qname)

    async def mode_decision(self, qname: str) -> QueryDecision:
        """The mode tail: static records + NXDOMAINs off-list names;
        allow records them; interactive resolves unpinned."""
        workspace_id = self.workspace_id
        if self.policy.mode == MODE_STATIC:
            await self.record_quietly("denied", workspace_id, qname)
            return QueryDecision(NXDOMAIN)
        if self.policy.mode == MODE_ALLOW:
            await self.record_quietly("allowed", workspace_id, qname)
        return QueryDecision(RECORD)

    async def record_quietly(
        self, decision: str, workspace_id: str, qname: str
    ) -> None:
        """Record a policy row, best-effort: an audit write that
        fails must not break the answer the guest is waiting for."""
        with contextlib.suppress(Exception):
            await self.model.record_policy(decision, workspace_id, qname, 0)

    async def retract(self, ips: list[str]) -> None:
        """Drop the kernel pins addresses carried before a second
        name resolved to them (#304): an address-keyed pin on a
        shared address enforces one name's verdict on its
        co-resident, so the pin goes and the next SYN gates at the
        queue under the naming memory instead. Interactive only:
        a static chain has no queue to gate a withdrawn pin's
        connections — its allowlist pins ARE the enforcement, so
        they stand."""
        if not self.policy.interactive:
            return
        await self.net.retract_consent_pins(self.workspace_id, ips)

    async def learn(
        self,
        records: list[tuple[str, int]],
        ports: set[int] | None,
        cap: float | None,
    ) -> None:
        """Pin one answer's addresses for each allowed port (or all
        ports), each for ``min(ttl, cap)`` seconds — the kernel
        expires the pin, and a verdict-capped pin dies with its
        verdict."""
        for ip, ttl in records:
            lifetime = min(float(ttl), cap) if cap is not None else float(ttl)
            for port in ports if ports is not None else {None}:
                await self.net.consent_allow(
                    self.workspace_id, ip, port, max(lifetime, 1.0)
                )


class DnsForwarder:
    """One workspace's resolver on its tap address.

    ``client_ip`` is the one address queries may come from: the
    workspace's guest. A datagram from anything else — a spoofed
    source naming an off-tap victim, say — is dropped unread, which
    is what keeps the forwarder from serving as a reflection
    amplifier.

    ``gate`` (the manager attaches one for every consent-enabled
    workspace) turns the relay into the naming layer; without it
    the forwarder is #52's verbatim relay.
    """

    def __init__(
        self,
        upstream: tuple[str, int],
        timeout_s: float = 3.0,
        *,
        bind: tuple[str, int] | None = None,
        client_ip: str = "",
        gate: ResolverGate | None = None,
    ) -> None:
        self._upstream = upstream
        self._timeout_s = timeout_s
        self._bind = bind or ("0.0.0.0", DNS_PORT)
        self._client_ip = client_ip
        self._gate = gate
        self._sock: socket.socket | None = None
        self._tasks: set[asyncio.Task] = set()
        # question wire -> (answer wire, expire epoch)
        self._cache: dict[bytes, tuple[bytes, float]] = {}
        # ip -> [(name, expire), ...] most-recent last: the naming
        # memory, one entry per live name per address (a shared
        # address keeps every co-resident's pairing, #304).
        self._names: dict[str, list[tuple[str, float]]] = {}

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

    # --- naming memory -------------------------------------------------------

    def live_names(self, ip: str) -> list[tuple[str, float]]:
        """The address's live pairings, most-recent last; expired
        ones are pruned as a side effect (every reader pays the
        same lazy sweep)."""
        now = time.time()
        entries = [e for e in self._names.get(ip, ()) if e[1] > now]
        if entries:
            self._names[ip] = entries
        else:
            self._names.pop(ip, None)
        return entries

    def host_for(self, ip: str) -> str | None:
        """The name that most recently resolved to ``ip``, while
        its pairing lives — the flow-namer for prompts and the
        session gate (a co-resident address names the flow by its
        latest resolution, #304)."""
        entries = self.live_names(ip)
        return entries[-1][0] if entries else None

    def ips_for(self, host: str) -> list[str]:
        """Every live address a name resolved to (revocation's
        target set)."""
        now = time.time()
        return [
            ip
            for ip, entries in self._names.items()
            if any(name == host and expire > now for name, expire in entries)
        ]

    def shared(self, ip: str) -> bool:
        """Whether more than one live name resolved to ``ip`` —
        the co-residency mark: kernel pins on such an address are
        over-broad by construction, so the manager installs none
        (#304)."""
        return len({name for name, _expire in self.live_names(ip)}) > 1

    def forget(self, host: str) -> None:
        """Drop a name's pairings and its cached answers
        (revocation): a revoke must not keep serving the name the
        verdict it undid. The answer cache has no name index, so it
        clears wholesale — revokes are rare and the next query
        repopulates it."""
        kept: dict[str, list[tuple[str, float]]] = {}
        for ip, entries in self._names.items():
            remaining = [e for e in entries if e[0] != host]
            if remaining:
                kept[ip] = remaining
        self._names = kept
        self._cache.clear()

    def remember(self, name: str, records: list[tuple[str, int]]) -> list[str]:
        """Record one answer's name→address pairings, floored so a
        0-TTL answer cannot unname a destination mid-flight, and
        return the addresses that just became shared — a second
        live name arrived, so every kernel pin the address carries
        is over-broad now and the caller retracts them (#304). The
        bound clears wholesale: a flood of unique names must not
        grow this dict for the workspace's life."""
        if len(self._names) >= NAMES_MAX:
            self._names.clear()
        expire = time.time() + NAME_TTL_FLOOR
        became_shared: list[str] = []
        for ip, ttl in records:
            if self.remember_one(ip, name, ttl, expire):
                became_shared.append(ip)
        return became_shared

    def remember_one(
        self, ip: str, name: str, ttl: int, expire: float
    ) -> bool:
        """Record one address's pairing, most-recent last; True when
        a second live name arrived on the address (the co-residency
        mark, #304)."""
        entries = self.live_names(ip)
        floor = pairing_floor(self, ip, ttl, expire, name)
        live = {entry_name for entry_name, _expire in entries}
        self._names[ip] = [
            *(e for e in entries if e[0] != name),
            (name, floor),
        ]
        return bool(live) and name not in live

    # --- the relay ---------------------------------------------------------

    async def _relay(
        self, sock: socket.socket, query: bytes, client: tuple[str, int]
    ) -> None:
        if self._gate is None:
            await self.relay_verbatim(sock, query, client)
            return
        await self.relay_gated(sock, query, client)

    async def relay_verbatim(
        self, sock: socket.socket, query: bytes, client: tuple[str, int]
    ) -> None:
        """The #52 relay: verbatim, no parsing (a forwarder that
        does not parse the message cannot break a query feature it
        never heard of)."""
        answer = await self.exchange(query)
        if answer is not None:
            sendto(sock, answer, client)

    async def relay_gated(
        self, sock: socket.socket, query: bytes, client: tuple[str, int]
    ) -> None:
        """One gated query: malformed drops, then the gate decides
        — classify runs BEFORE the cache, so a fresh deny (a forever
        row, a session memory) overrides a cached positive answer —
        and a cache hit answers without the upstream round-trip."""
        parsed = dnsmsg.parse_query(query)
        if parsed is None or not parsed.name:
            return  # malformed: dropped (fail-closed)
        decision = await self._gate.classify(parsed.name)
        if decision.action == NXDOMAIN:
            sendto(sock, dnsmsg.nxdomain_for(query), client)
            return
        if (cached := self.cache_get(parsed)) is not None:
            sendto(sock, dnsmsg.rewrite_id(cached, parsed.id), client)
            return
        await self.gated_exchange(sock, query, parsed, client, decision)

    async def gated_exchange(
        self,
        sock: socket.socket,
        query: bytes,
        parsed: dnsmsg.Question,
        client: tuple[str, int],
        decision: QueryDecision,
    ) -> None:
        """A fresh gated query (the classify already ran): forward,
        learn/record, cache, answer."""
        answer = await self.exchange(query)
        if answer is None:
            return
        records = dnsmsg.parse_a_records(answer)
        await self.note_resolution(parsed.name, records)
        if decision.action == LEARN and records:
            await self.learn_quietly(records, decision)
        self.cache_put(parsed, answer, records)
        sendto(sock, answer, client)

    async def note_resolution(
        self, name: str, records: list[tuple[str, int]]
    ) -> None:
        """Feed the naming memory and retract what a newly shared
        address carries (#304) — the caller owns the answer."""
        shared = self.remember(name, records)
        if shared and self._gate is not None:
            await self.retract_quietly(shared)

    async def learn_quietly(
        self, records: list[tuple[str, int]], decision: QueryDecision
    ) -> None:
        """Pin a learn decision's addresses, best-effort: a failed
        pin re-prompts at the SYN; DNS itself still works."""
        with contextlib.suppress(Exception):
            await self._gate.learn(records, decision.ports, decision.cap)

    async def retract_quietly(self, ips: list[str]) -> None:
        """Retract the pins addresses carried before becoming
        shared, best-effort: a missed retraction is a leak window
        bounded by the pin's own timeout, and DNS still works."""
        with contextlib.suppress(Exception):
            await self._gate.retract(ips)

    async def exchange(self, query: bytes) -> bytes | None:
        """One upstream round-trip; None on timeout or send
        failure (silence — the client's own resolver timeout
        retries or fails; an empty reply would only confuse it)."""
        upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        upstream.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            sendto(upstream, query, self._upstream)
            answer = await asyncio.wait_for(
                recvfrom(loop, upstream, MAX_DATAGRAM), self._timeout_s
            )
            return answer[0]
        except TimeoutError, OSError:
            return None
        finally:
            upstream.close()

    # --- the answer cache ---------------------------------------------------

    def cache_get(self, question: dnsmsg.Question) -> bytes | None:
        """A cached answer for this exact question (id-insensitive:
        the key drops the id because a second client asks the same
        question under its own), or None."""
        entry = self._cache.get(question.wire[2:])
        if entry is None:
            return None
        if entry[1] <= time.time():
            self._cache.pop(question.wire[2:], None)
            return None
        return entry[0]

    def cache_put(
        self,
        question: dnsmsg.Question,
        answer: bytes,
        records: list[tuple[str, int]],
    ) -> None:
        """Cache one answered question. Only A-record answers cache
        (an empty or malformed answer section has no TTL to bound
        it); a cache past its bound clears wholesale."""
        if not records:
            return
        ttl = min(ttl for _ip, ttl in records)
        expire = time.time() + max(float(ttl), CACHE_TTL_FLOOR)
        key = question.wire[2:]
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[key] = (answer, expire)


def pairing_floor(
    forwarder, ip: str, ttl: int, floor: float, name: str
) -> float:
    """One pairing's expiry: the answer's TTL (floored), never
    shortened below the same name's prior pairing on that
    address."""
    if ttl > NAME_TTL_FLOOR:
        floor = time.time() + ttl
    prior = max(
        (
            expire
            for entry_name, expire in forwarder._names.get(ip, ())
            if entry_name == name
        ),
        default=0.0,
    )
    return max(prior, floor)  # a re-resolve never shortens
