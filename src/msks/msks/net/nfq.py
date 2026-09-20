"""The per-VM NFQUEUE consumer (#69).

One consumer per interactive workspace, bound to that workspace's
own queue number (derived from its pool slice): the kernel hands
the first packet of every NEW flow to this loop, which holds it
(:meth:`msks.consent.ConsentEngine.hold`) until a verdict — allow,
deny, timeout — then applies it:

- allow → pin the destination in the per-VM ``allows`` set (the
  kernel expires it at the verdict's duration) and accept the SYN;
  conntrack carries the connection from there.
- deny → drop the SYN and pin a short REJECT (tcp-reset) element
  so the *retransmit* is answered with an RST — a dropped SYN alone
  leaves the guest's ``connect()`` hanging on the kernel's ~127 s
  retransmit timer, and the RST is what makes the refusal fast.
  (klangk's sidecar forged the RST itself through a raw socket; the
  appliance service user holds only CAP_NET_ADMIN and
  CAP_NET_BIND_SERVICE, so here the kernel forges it via the
  REJECT rule instead — same effect, no extra capability.)

Deferred verdicts: the callback never blocks — each held SYN is
retained and handed to a task, so distinct flows hold concurrently
and one slow decider cannot serialize another workspace's
verdicts. Retransmits of a held SYN drop (the in-flight set); of a
decided flow, they reuse the cached verdict (the verdict cache,
keyed by the connection tuple — which is what makes ``once``
per-connection: a new source port is a cache miss and re-prompts).

``netfilterqueue`` ships with every install (consent is the normal
posture for workspaces): the devenv shells build it against
nixpkgs' libnetfilter_queue/libnfnetlink, and the appliance closure
builds it via ``nix/netfilterqueue-pkg.nix``. The import stays
guarded — an exotic install without the library fails closed at
bind time as a named refusal (the workspace boot refuses rather
than running an unanswered queue), never silently.
"""

import asyncio
import contextlib
import logging
import time

from ..consent.coordinator import ONCE_REJECT_S, duration_ttl
from ..microvm.errors import MicrovmError

logger = logging.getLogger(__name__)

# The verdict cache bound: a denied-flow flood accumulates entries
# (allowed flows get pinned in the kernel and stop queueing), so
# the cache clears wholesale past the bound (klangk's blunt cap).
VERDICT_CACHE_MAX = 4096

VERDICT_CACHE_TTL = 120.0

try:
    from netfilterqueue import NetfilterQueue
except ImportError:  # pragma: no cover — an install without the
    # library (the binding ships with every install; dev/CI shells
    # and the appliance closure build it); the guard keeps that
    # install fail-closed at bind time instead of crashing import.
    NetfilterQueue = None


def ipv4_offsets(payload: bytes) -> tuple[int, int] | None:
    """(offset, ihl) of the IPv4 header, or None when the payload is
    not IPv4. NFQUEUE hands the loop L3 payloads; an Ethernet
    prefix (14 bytes) is sniffed the way klangk's sidecar did, in
    case a hook ever feeds L2."""
    off = ethernet_offset(payload)
    if not ipv4_header(payload, off):
        return None
    return off, (payload[off] & 0x0F) * 4


def ethernet_offset(payload: bytes) -> int:
    """14 when the payload carries an Ethernet header before the
    IPv4 packet (a hook that ever feeds L2), else 0."""
    if (
        len(payload) > 14
        and (payload[0] >> 4) != 4
        and (payload[14] >> 4) == 4
    ):
        return 14
    return 0


def ipv4_header(payload: bytes, off: int) -> bool:
    """Whether a well-formed IPv4 header starts at ``off``: the
    version nibble, a sane IHL, and the whole header in bounds."""
    if off + 20 > len(payload) or (payload[off] >> 4) != 4:
        return False
    ihl = (payload[off] & 0x0F) * 4
    return ihl >= 20 and off + ihl <= len(payload)


def parse_packet(
    payload: bytes,
) -> tuple[str, int, str, int, int] | None:
    """``(src_ip, src_port, dst_ip, dst_port, proto)`` of a queued
    packet, or None when unparseable (dropped — fail-closed). A
    non-TCP/UDP packet reports ports 0 (the portless verdict
    key)."""
    hdr = ipv4_offsets(payload)
    if hdr is None:
        return None
    off, ihl = hdr
    proto = payload[off + 9]
    src = ".".join(str(b) for b in payload[off + 12 : off + 16])
    dst = ".".join(str(b) for b in payload[off + 16 : off + 20])
    sport, dport = l4_ports(payload, off + ihl, proto)
    return src, sport, dst, dport, proto


def l4_ports(payload: bytes, l4: int, proto: int) -> tuple[int, int]:
    """The (source, destination) ports of a TCP/UDP payload, or
    (0, 0) for anything else (the portless verdict key)."""
    if proto not in (6, 17) or l4 + 4 > len(payload):
        return 0, 0
    sport = int.from_bytes(payload[l4 : l4 + 2], "big")
    dport = int.from_bytes(payload[l4 + 2 : l4 + 4], "big")
    return sport, dport


class FlowConsumer:
    """Drives one workspace's queue on the daemon's event loop."""

    def __init__(self, workspace_id: str, queue_num: int, net) -> None:
        self.workspace_id = workspace_id
        self.queue_num = queue_num
        self._net = net
        self._nfq = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # (src_port, dst_ip, dst_port) -> ("allow"|"deny", expire).
        self._verdicts: dict[tuple[int, str, int], tuple[str, float]] = {}
        self._inflight: set[tuple[int, str, int]] = set()
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        """Bind the queue and drive it from the loop.

        Binding precedes the chain install that references the
        queue (the manager's order), so no packet ever lands in an
        unbound queue — the fail-closed gap stays closed."""
        if NetfilterQueue is None:
            raise MicrovmError(
                f"consent for {self.workspace_id}: netfilterqueue is "
                "not installed (the appliance image installs "
                "msks[nfqueue]); refusing to run an unanswered queue"
            )
        self._nfq = NetfilterQueue()
        self._nfq.bind(self.queue_num, self.on_packet)
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self._nfq.get_fd(), self.drain)

    def stop(self) -> None:
        """Unbind; queued packets the kernel still holds drop (a
        queue without a listener is a drop, not a pass)."""
        nfq = self._nfq
        self._nfq = None
        if nfq is None:
            return
        if self._loop is not None:
            self._loop.remove_reader(nfq.get_fd())
            self._loop = None
        nfq.unbind()
        for task in list(self._tasks):
            task.cancel()

    def drain(self) -> None:
        """Process pending queue messages (readability callback)."""
        try:
            self._nfq.run(block=False)
        except Exception:
            pass  # a transient netlink read defers to the next event

    # --- per-packet --------------------------------------------------------

    def on_packet(self, pkt) -> None:
        """Classify and route one queued packet — non-blocking."""
        parsed = parse_packet(pkt.get_payload())
        if parsed is None:
            pkt.drop()  # unparseable: fail-closed
            return
        _src, sport, dst, dport, _proto = parsed
        flow = (sport, dst, dport)
        now = time.time()
        cached = self.cached_verdict(flow, now)
        if cached is not None:
            self.apply_cached(pkt, cached, dst, dport)
            return
        if flow in self._inflight:
            # A retransmit of a SYN still being held: the in-flight
            # task owns it; a second hold per retransmit would pile
            # up duplicates. Drop — the verdict's retransmit hits
            # the cache.
            pkt.drop()
            return
        if self.session_gate(pkt, flow, dst, dport, now):
            return
        pkt.retain()
        self._inflight.add(flow)
        self.spawn(self.decide(pkt, flow, dst, dport))

    def cached_verdict(
        self, flow: tuple[int, str, int], now: float
    ) -> tuple[str, float] | None:
        """The still-valid cached verdict for a flow, if any."""
        cached = self._verdicts.get(flow)
        if cached is not None and cached[1] > now:
            return cached
        return None

    def apply_cached(
        self, pkt, cached: tuple[str, float], dst: str, dport: int
    ) -> None:
        """Reuse a decided connection's verdict for its retransmit.
        A cached deny also refreshes the fail-fast RST pin: the
        original pin may have lapsed inside the cache window, and a
        retry that only drops hangs on the kernel's retransmit
        timer — the exact hang the RST exists to prevent."""
        if cached[0] == "allow":
            pkt.accept()
            return
        if dport:
            self.spawn(
                self._net.consent_reject(
                    self.workspace_id, dst, dport, ONCE_REJECT_S
                )
            )
        pkt.drop()

    def spawn(self, coro) -> None:
        """Track one background task (the held SYN's decide task, or
        a fire-and-forget rule pin from the packet callback, which
        must not await); errors are logged, and the strong ref
        keeps the task alive to that point."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def reaped(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error("nfq enforcement failed: %s", done.exception())

        task.add_done_callback(reaped)

    def session_gate(
        self,
        pkt,
        flow: tuple[int, str, int],
        dst: str,
        dport: int,
        now: float,
    ) -> bool:
        """The name-scoped gates before prompting (klangk's
        #2372/#2434/#2446): a SYN to a host an in-effect verdict
        already covers — including a CDN-rotated IP no fresh
        resolution learned — never re-prompts. Allow wins over deny
        (checked first). Returns True when the packet was handled."""
        engine = self._net.app.state.consent
        host = self._net.host_for(self.workspace_id, dst) or dst
        if dport:
            remaining = engine.session.allow_ttl(
                self.workspace_id, host, dport
            )
            if remaining is not None:
                self.accept_with_pin(pkt, flow, dst, dport, now, remaining)
                return True
            denied = engine.session.deny_ttl(self.workspace_id, host, dport)
            if denied is not None:
                self.deny_fast(pkt, flow, dst, dport, now, denied)
                return True
        return False

    def accept_with_pin(
        self,
        pkt,
        flow: tuple[int, str, int],
        dst: str,
        dport: int,
        now: float,
        remaining: float,
    ) -> None:
        """Accept a session-covered SYN and pin its IP for the
        verdict's remaining window (port-scoped — the consented
        port), so future connections skip the queue entirely."""
        self.spawn(
            self._net.consent_allow(
                self.workspace_id, dst, dport or None, remaining
            )
        )
        pkt.accept()
        self._verdicts[flow] = ("allow", now + VERDICT_CACHE_TTL)

    def deny_fast(
        self,
        pkt,
        flow: tuple[int, str, int],
        dst: str,
        dport: int,
        now: float,
        remaining: float,
    ) -> None:
        """Deny a session-covered SYN fast: RST element for the
        deny's remaining window, cached verdict, drop. The caller
        guarantees a ported flow (an RST needs a port)."""
        self.spawn(
            self._net.consent_reject(self.workspace_id, dst, dport, remaining)
        )
        pkt.drop()
        self._verdicts[flow] = ("deny", now + VERDICT_CACHE_TTL)

    async def decide(
        self,
        pkt,
        flow: tuple[int, str, int],
        dst: str,
        dport: int,
    ) -> None:
        """Hold the SYN for a verdict, then apply it (deferred)."""
        engine = self._net.app.state.consent
        host = self._net.host_for(self.workspace_id, dst) or dst
        try:
            future = await engine.hold(self.workspace_id, host, dport)
            verdict = await future
        except asyncio.CancelledError:
            # Teardown cancelled the hold task: the queue is going
            # away; drop the retained SYN and let the cancel run.
            with contextlib.suppress(Exception):
                pkt.drop()
            raise
        except Exception:
            # The engine's own gate never raises (it fail-closes),
            # but a bug there must not eat the packet: deny.
            verdict = {"decision": "deny", "reason": "error"}
        try:
            await self.apply_verdict(pkt, flow, dst, dport, verdict)
        except Exception:
            # An enforcement failure (a racing table teardown, a
            # transient nft error) must not eat the retained packet
            # either: deny it now and say so.
            logger.exception(
                "nfq: applying a verdict for %s:%s failed; dropping",
                dst,
                dport,
            )
            pkt.drop()
        finally:
            # Always discard, even if enforcement raised: a stuck
            # key would silently drop that flow's retransmits
            # forever.
            self._inflight.discard(flow)

    async def apply_verdict(
        self,
        pkt,
        flow: tuple[int, str, int],
        dst: str,
        dport: int,
        verdict: dict,
    ) -> None:
        """Apply one verdict to its held SYN (and cache it)."""
        now = time.time()
        if len(self._verdicts) > VERDICT_CACHE_MAX:
            self._verdicts.clear()
        duration = verdict.get("duration") or "once"
        self._verdicts[flow] = (
            "allow" if verdict["decision"] == "allow" else "deny",
            now + VERDICT_CACHE_TTL,
        )
        if verdict["decision"] == "allow":
            await self.apply_allow(pkt, dst, dport, duration)
            return
        await self.apply_deny(pkt, dst, dport, duration)

    async def apply_allow(
        self, pkt, dst: str, dport: int, duration: str
    ) -> None:
        """Accept the SYN, pinning the destination for a duration
        that outlives this connection (``once`` pins nothing — a
        reconnect re-prompts)."""
        ttl = duration_ttl(duration)
        if ttl is not None:
            await self._net.consent_allow(
                self.workspace_id, dst, dport or None, ttl
            )
        pkt.accept()

    async def apply_deny(
        self, pkt, dst: str, dport: int, duration: str
    ) -> None:
        """Drop the SYN and pin a fail-fast RST for the retransmit
        (TCP only — an RST is meaningless for anything else)."""
        if dport:
            reject_ttl = duration_ttl(duration)
            if reject_ttl is None:
                reject_ttl = ONCE_REJECT_S
            await self._net.consent_reject(
                self.workspace_id, dst, dport, reject_ttl
            )
        pkt.drop()
