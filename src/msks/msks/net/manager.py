"""The egress attachment lifecycle: one tap + services per workspace (#52).

NetManager is the state object on ``app.state.net``. Enabled and
privileged, ``start()`` arms the shared plumbing once (the NAT
base table, after verifying the kernel's ip_forward sysctl — the
deployment ships it as a boot-time setting, and the daemon never
writes it); every egress workspace boot then ``attach()``s
— tap, per-VM chain, DHCP service, DNS forwarder — and every stop,
kill, or delete ``detach()``es all of it.

Fail-closed: a daemon that cannot arm the plumbing records itself
unavailable, and each egress workspace boot then refuses with a
named cause instead of running with a half-open path. Workspaces
without egress never touch any of this.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from ipaddress import IPv4Network
from pathlib import Path

from ..consent.coordinator import LONG_TTL_S
from ..consent.specs import EgressPolicy, is_ipv4
from ..microvm.errors import MicrovmError
from ..model.egress_consent import DECISION_ALLOWED
from . import alloc, conntrack, dns, nft, taps
from .dhcp import DhcpServer
from .dns import DnsForwarder, ResolverGate
from .nfq import FlowConsumer

logger = logging.getLogger(__name__)

FORWARDING = Path("/proc/sys/net/ipv4/ip_forward")
SYSCTL_KEY = "net.ipv4.ip_forward"

#: Between forward-dial retries (#109): the console bring-up poll's
#: cadence — a just-booted guest's services answer in this rhythm.
FORWARD_POLL_S = 0.05

#: The cause named when every attempt was silent (each bound's
#: TimeoutError carries no message of its own).
DIAL_DEADLINE_EXPIRED = "dial deadline expired"


async def dial_with_retry(dialer, host: str, port: int, timeout_s: float):
    """Dial until the deadline, naming the last real refusal.

    Each attempt is bounded by the deadline's remainder — a silent
    peer (a dropped SYN) names the deadline, not the kernel's ~130 s
    SYN retry — and the last refusal a dialer raised is the cause the
    operator reads when the deadline finally closes the question.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last_cause = ""
    while loop.time() < deadline:
        try:
            return await asyncio.wait_for(
                dialer(host, port), deadline - loop.time()
            )
        except (OSError, TimeoutError) as exc:
            last_cause = str(exc) or last_cause
            await asyncio.sleep(FORWARD_POLL_S)
    raise MicrovmError(
        f"forward to {host}:{port} unavailable: "
        f"{last_cause or DIAL_DEADLINE_EXPIRED}"
    )


NOT_READY_CAUSES = {
    "init": "the egress subsystem never started",
    "disabled": (
        "egress is not enabled (MSKSD_EGRESS_ENABLED, read at startup "
        "— set it and restart)"
    ),
    "unavailable": (
        "the daemon could not arm egress (it needs CAP_NET_ADMIN and "
        "CAP_NET_BIND_SERVICE — the deployment grants both, and only "
        "those, to its service user — plus " + SYSCTL_KEY + "=1 from "
        "sysctl.d; a dev-shell daemon has none of them)"
    ),
}


class WorkspaceGuard:
    """A per-workspace re-entrant async guard (#280 review, round
    2): attach, detach, and the live mode swap hold it, and the
    interceptor's arm/disarm — which run under those callers and
    also standalone, from the watcher's placeholder sweep and the
    mint/renew/revoke routes — hold it through
    ``apply_interception``. Re-entrant by task: the nested table
    swap a holder drives is the mutation itself, not an
    interloper; every other writer waits."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._holder: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self) -> WorkspaceGuard:
        task = asyncio.current_task()
        if self._holder is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._holder = task
        self._depth = 1
        return self

    async def __aexit__(self, *exc) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._holder = None
            self._lock.release()


@dataclass(frozen=True)
class NetAttachment:
    """What the VM spec needs from an armed egress workspace."""

    workspace_id: str
    tap: str
    mac: str
    guest_ip: str
    tap_ip: str
    slice: int


@dataclass
class NetServices:
    """One workspace's live DHCP + DNS (+ consent consumer) tasks,
    and the firewall shape a table swap re-applies (#199). The
    gate is the resolver's live policy cell: a mode switch (#280)
    rewrites it in the same step that swaps the table, so the
    naming layer and the chain never disagree."""

    dhcp: DhcpServer
    dns: DnsForwarder
    tasks: list[asyncio.Task]
    gate: ResolverGate
    consumer: FlowConsumer | None = None
    policy: EgressPolicy | None = None
    queue_num: int | None = None

    def stop_consumer(self) -> None:
        """Unbind the consent queue first: an unbound queue drops
        (fail-closed), so no packet passes while the table waits
        for its own delete."""
        if self.consumer is not None:
            self.consumer.stop()
            self.consumer = None


def verify_forwarding(path: Path = FORWARDING) -> None:
    """Refuse egress unless the kernel already routes packets (#101).

    ``ip_forward`` is part of the machine's identity: the deployment
    ships ``net.ipv4.ip_forward=1`` as a boot-time sysctl.d setting,
    and the daemon — a service user without write access to
    /proc/sys — verifies it and fails closed. A daemon that finds
    ``0`` records itself unavailable, and every egress workspace
    boot refuses with a cause naming the sysctl key.
    """
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise MicrovmError(
            f"could not read {SYSCTL_KEY} ({path}): {exc}"
        ) from exc
    if value != "1":
        raise MicrovmError(
            f"{SYSCTL_KEY} is not enabled (reads {value!r}); enable it "
            "at boot with sysctl.d and restart the daemon"
        )


def reject_is_per_flow(
    services, ip: str, sport: int | None, named: bool
) -> bool:
    """Whether a deny's RST element is per-flow (#304): a NAME
    verdict on a shared address — the element would otherwise
    answer the co-resident's connections too."""
    return bool(named and sport and services.dns.shared(ip))


class NetManager:
    """Owns every workspace's egress plumbing."""

    def __init__(
        self,
        app,
        *,
        dhcp_factory=DhcpServer,
        dns_factory=DnsForwarder,
        dialer=None,
        consumer_factory=FlowConsumer,
        llm_factory=None,
    ) -> None:
        self.app = app
        self._dhcp_factory = dhcp_factory
        self._dns_factory = dns_factory
        self._consumer_factory = consumer_factory
        # The per-tap LLM listener seam (#259): the default asks the
        # llm subsystem; the tests inject one that records the ask.
        self._llm_factory = llm_factory
        # The guest-dial seam for the forward websocket (#109): the
        # default dials real TCP; the tests inject one that answers
        # from a listener they control (fake_ch has no NIC).
        self.dialer = dialer or asyncio.open_connection
        self._attachments: dict[str, NetAttachment] = {}
        self._services: dict[str, NetServices] = {}
        self._guards: dict[str, WorkspaceGuard] = {}
        self._forwards: dict[str, list] = {}
        self._used_slices: set[int] = set()
        self._state = "init"  # init | disabled | ready | unavailable

    async def start(self) -> None:
        """Arm the shared plumbing once, or record why not."""
        settings = self.app.state.settings
        if not settings.net.enabled:
            self._state = "disabled"
            return
        try:
            verify_forwarding()
            await nft.apply_base(settings)
        except (MicrovmError, OSError) as exc:
            # Loud, not fatal: workspaces without egress are unaffected;
            # every egress boot below refuses with this cause.
            print(f"msksd: egress unavailable: {exc}", flush=True)
            self._state = "unavailable"
            return
        self._state = "ready"

    async def stop(self) -> None:
        """Detach every workspace (daemon shutdown; idempotent)."""
        for workspace_id in list(self._attachments):
            await self.detach(workspace_id)
        self._state = "init"

    async def attach(
        self,
        workspace_id: str,
        *,
        want: bool,
        policy: EgressPolicy | None = None,
    ) -> NetAttachment | None:
        """Arm one workspace's egress; None when it asked for none.

        Idempotent per workspace: an existing attachment is returned
        as-is, so a boot racing a stop/kill cycle converges on the
        one live attachment.
        """
        if not want:
            return None
        self.require_ready(workspace_id)
        async with self.workspace_guard(workspace_id):
            existing = self._attachments.get(workspace_id)
            if existing is not None:
                return existing
            return await self._build(
                workspace_id, policy or EgressPolicy(workspace_id, "allow", ())
            )

    def workspace_guard(self, workspace_id: str) -> WorkspaceGuard:
        """The per-workspace net-mutation guard: attach, detach,
        the live mode swap, and the interceptor's table swap
        serialize against each other whatever the route above them
        holds (#280 review) — a detach landing between a swap's
        table probe and its install would leave a table and a
        bound queue nothing cleans."""
        guard = self._guards.get(workspace_id)
        if guard is None:
            guard = self._guards.setdefault(workspace_id, WorkspaceGuard())
        return guard

    async def forward_stream(self, workspace_id: str, port: int):
        """(reader, writer) dialed to the guest's address on ``port``.

        The dial retries under one deadline (#109): a freshly booted
        guest races DHCP against its services, and connection-refused
        during that window is the bring-up state, not a failure. Each
        attempt is bounded by the deadline's remainder — a silent peer
        (a dropped SYN) names the deadline, not the kernel's ~130 s
        SYN retry. A workspace with no live attachment — not running,
        or created without egress — refuses immediately with a named
        cause.
        """
        attachment = self._attachments.get(workspace_id)
        if attachment is None:
            raise MicrovmError(
                f"workspace {workspace_id} has no live network attachment "
                "(not running, or created without egress)"
            )
        return await dial_with_retry(
            self.dialer,
            attachment.guest_ip,
            port,
            self.app.state.settings.vmm.forward_wait_timeout_s,
        )

    def track_forward(self, workspace_id: str, writer) -> None:
        """Remember a live forward's stream so detach can end it (#113).

        Stopping, killing, or deleting a workspace tears its tap down;
        without this, the daemon side of an open forward retransmits
        into the void for minutes while the client waits.
        """
        self._forwards.setdefault(workspace_id, []).append(writer)

    def untrack_forward(self, workspace_id: str, writer) -> None:
        """Forget a forward stream the route closed itself."""
        writers = self._forwards.get(workspace_id)
        if writers is None:
            return
        with contextlib.suppress(ValueError):
            writers.remove(writer)
        if not writers:
            self._forwards.pop(workspace_id, None)

    def close_forwards(self, workspace_id: str) -> None:
        """End the workspace's live forwards: closing the stream sends
        the FIN the torn-down tap no longer can."""
        for writer in self._forwards.pop(workspace_id, []):
            writer.close()

    async def stop_llm(self, workspace_id: str) -> None:
        """Stop the workspace's LLM listener when one serves (#259).
        Unconditional and idempotent: the services' own stop is the
        backstop, this one keeps the proxy's mapping honest."""
        if self.app.state.llm is not None:
            await self.app.state.llm.stop_listener(workspace_id)

    async def detach(self, workspace_id: str) -> None:
        """Tear one workspace's egress down (idempotent).

        Releasing the slice keeps the workspace on its own /30 across
        stop/start cycles — the address derives from it, and nothing
        else remembers the pairing. The consent stop hook runs first
        (holds fail-close while the table still exists to receive
        their drops), the consumer unbinds before the table delete
        (an unbound queue drops — fail-closed), and the plumbing
        subprocesses run before the service sockets close, so a
        closed-socket fd number is never reused by a fresh
        subprocess pipe underneath a stale selector entry.
        """
        async with self.workspace_guard(workspace_id):
            attachment = self._attachments.pop(workspace_id, None)
            services = self._services.pop(workspace_id, None)
            self.close_forwards(workspace_id)
            with contextlib.suppress(Exception):
                # Best-effort like the rest of the teardown: a listener
                # whose serve task already died badly must not abort the
                # table and tap cleanup below it.
                await self.stop_llm(workspace_id)
            if attachment is None:
                return
            await self.app.state.consent.on_workspace_stop(workspace_id)
            # The interceptor's listener drops before the table dies: an
            # armed workspace's redirected flows must not reach a proxy
            # whose entries are already gone (the table deletion below
            # needs no swap of its own).
            await self.app.state.interceptor.on_detach(workspace_id)
            if services is not None:
                services.stop_consumer()
            settings = self.app.state.settings
            await nft.delete_vm_table(settings, workspace_id)
            await taps.remove_tap(attachment.tap, settings)
            if services is not None:
                await stop_services(services)
            self._used_slices.discard(attachment.slice)

    def require_ready(self, workspace_id: str) -> None:
        """Refuse an egress boot unless the plumbing is armed."""
        if self._state == "ready":
            return
        cause = NOT_READY_CAUSES.get(self._state, self._state)
        raise MicrovmError(
            f"workspace {workspace_id} requests egress but {cause}"
        )

    def netmask(self) -> str:
        """The dotted-quad mask every /30 slice carries."""
        return str(IPv4Network((0, alloc.SLICE_PREFIX)).netmask)

    async def claim_slice(self, workspace_id: str) -> int:
        """The workspace's slice: the recorded one, or a fresh claim
        recorded on the row (#70 review).

        Recording is what makes the /30 stable — across stop/start,
        daemon restarts, and digest collisions between workspace ids
        (the fresh-claim walk only sees live attachments; the row
        remembers forever).
        """
        recorded = await self.app.state.model.egress_slice(workspace_id)
        if recorded is not None:
            self._claim_live(recorded, workspace_id)
            return recorded
        slice_ = self.free_slice(workspace_id)
        await self.app.state.model.set_egress_slice(workspace_id, slice_)
        return slice_

    def _claim_live(self, slice_: int, workspace_id: str) -> None:
        """Mark a recorded slice live, refusing a conflicting holder."""
        if slice_ in self._used_slices:
            raise MicrovmError(
                f"egress slice {slice_} recorded for {workspace_id} is "
                "held by another live workspace; delete one of them"
            )
        self._used_slices.add(slice_)

    def free_slice(self, workspace_id: str) -> int:
        """Pick and claim a fresh slice (stable start, walked forward
        past live collisions)."""
        count = alloc.slice_count(self.app.state.settings.net.pool)
        start = alloc.slice_index(workspace_id, count)
        for step in range(count):
            candidate = (start + step) % count
            if candidate not in self._used_slices:
                self._used_slices.add(candidate)
                return candidate
        raise MicrovmError("egress address pool exhausted")

    def queue_for(self, slice_: int) -> int:
        """The per-VM NFQUEUE number for a pool slice (#69): one
        workspace's SYN flood is a self-DoS only — it cannot starve
        another workspace's verdicts. A queue number is 16 bits, so
        a pool too large for the base refuses by name."""
        queue = self.app.state.settings.net.queue_base + slice_
        if queue > nft.QUEUE_MAX:
            raise MicrovmError(
                "egress pool too large for consent queues: slice "
                f"{slice_} maps to queue {queue} past "
                f"{nft.QUEUE_MAX}; shrink MSKSD_EGRESS_SUBNET or "
                "lower MSKSD_EGRESS_QUEUE_BASE"
            )
        return queue

    # --- consent enforcement helpers (#69) ---------------------------------

    def attachment_for(self, workspace_id: str) -> NetAttachment | None:
        """The workspace's live attachment, None when not running
        (the interceptor's arming predicate reads this)."""
        return self._attachments.get(workspace_id)

    async def apply_policy(
        self, workspace_id: str, policy: EgressPolicy
    ) -> bool:
        """Switch a live workspace's consent posture (#280): the
        whole table re-applies in one nft transaction carrying the
        consent elements across (established flows survive), the
        resolver gate flips with the chain, and the NFQUEUE
        consumer binds before a chain references it and unbinds
        after the chain stops referencing it — the same ordering
        rules ``_build`` follows, run against a workspace whose
        tap never goes down.

        Returns whether a live swap ran: a workspace without an
        attachment keeps the row's change for its next boot."""
        async with self.workspace_guard(workspace_id):
            return await self.switch_live(policy, workspace_id)

    async def switch_live(
        self, policy: EgressPolicy, workspace_id: str
    ) -> bool:
        """The swap itself, under the workspace's net lock: the
        detach a stop or delete drives cannot interleave the table
        probe and the install (a table and a bound queue nothing
        cleans would be the residue)."""
        pair = self.switch_pair(workspace_id)
        if pair is None:
            return False
        attachment, services = pair
        old = services.policy
        await self.retire_interactive_holds(old, policy, workspace_id)
        elements = await self.switch_elements(
            old, policy, workspace_id, services
        )
        fresh, queue_num = self.bind_switch_consumer(
            workspace_id, attachment, services, policy
        )
        try:
            await self.install_swapped_table(
                workspace_id, attachment, policy, queue_num, elements
            )
        except BaseException:
            self.abort_switch(fresh)
            raise
        self.commit_switch(services, policy, old, fresh, queue_num)
        await self.replay_switch_verdicts(policy, old, workspace_id)
        return True

    def switch_pair(self, workspace_id: str) -> tuple | None:
        """``(attachment, services)`` for a live workspace, None
        when it is not attached — the row's change is then the
        whole switch, and the next boot builds it."""
        attachment = self._attachments.get(workspace_id)
        services = self._services.get(workspace_id)
        if attachment is None or services is None:
            return None
        return attachment, services

    def abort_switch(self, fresh) -> None:
        """Unbind a freshly-bound consumer whose swap failed: the
        old table still enforces, and a bound queue no chain
        references is a leak."""
        if fresh is not None:
            fresh.stop()

    async def replay_switch_verdicts(
        self, policy: EgressPolicy, old: EgressPolicy, workspace_id: str
    ) -> None:
        """Pin the durable address verdicts when the swap enters a
        gated posture from one that kept no consent sets (allow):
        the fresh sets start empty, and the rows say what belongs
        in them."""
        if policy.gated and not old.gated:
            await self.replay_forever(workspace_id)

    async def retire_interactive_holds(
        self, old: EgressPolicy, policy: EgressPolicy, workspace_id: str
    ) -> None:
        """Fail-close the workspace's holds when the swap drops the
        queue rule (#280): a held SYN must not queue into a table
        that is about to lose its queue — it answers deny now, not
        at the kernel's retransmit timer."""
        if old.interactive and not policy.interactive:
            await self.app.state.consent.fail_close_workspace(
                workspace_id, reason="mode switch"
            )

    async def switch_elements(
        self,
        old: EgressPolicy,
        policy: EgressPolicy,
        workspace_id: str,
        services,
    ) -> str:
        """The #260 carry, mode-to-mode: verdict pins and
        resolver-learned allows ride the swap between gated
        postures, filtered to the sets the TARGET table defines
        (#280 review) — a carried ``rejects`` element meeting a
        static table (no such set) would fail the whole
        transaction. A switch to allow carries nothing at all."""
        if not (old.gated and policy.gated):
            return ""
        snapshot = await self.consent_snapshot(workspace_id, services)
        carried = {
            name: scopes
            for name, scopes in snapshot.items()
            if name in nft.posture_sets(policy)
        }
        return nft.element_statements(alloc.table_name(workspace_id), carried)

    def bind_switch_consumer(
        self, workspace_id, attachment, services, policy
    ) -> tuple:
        """``(fresh consumer or None, queue_num)`` for the swapped
        table. Entering interactive binds a NEW consumer before the
        chain references the queue (the _build rule: an unbound
        queue drops); staying interactive keeps the bound one and
        its queue number."""
        if not policy.interactive:
            return None, None
        if services.consumer is not None:
            return None, services.queue_num
        queue_num = self.queue_for(attachment.slice)
        fresh = self._consumer_factory(workspace_id, queue_num, self)
        fresh.start()
        return fresh, queue_num

    async def install_swapped_table(
        self, workspace_id, attachment, policy, queue_num, elements
    ) -> None:
        """The one-transaction table swap with the interceptor's
        armed half exactly as it stands (#199's swap, retargeted
        by #280)."""
        await nft.install_vm(
            self.app.state.settings,
            workspace_id,
            attachment.tap,
            attachment.guest_ip,
            attachment.tap_ip,
            policy=policy,
            queue_num=queue_num,
            interceptor_port=self.interceptor_port(workspace_id),
            elements=elements,
        )

    def commit_switch(
        self,
        services,
        policy: EgressPolicy,
        old: EgressPolicy,
        fresh,
        queue_num,
    ) -> None:
        """Land the swap's bookkeeping: the consumer slot, the
        recorded shape, and the resolver gate's live policy cell —
        all after the fresh table applied, so a mid-swap read sees
        one whole posture."""
        self.retire_switched_consumer(services, policy, fresh)
        services.policy = policy
        services.queue_num = queue_num
        services.gate.policy = policy

    def retire_switched_consumer(
        self, services, policy: EgressPolicy, fresh
    ) -> None:
        """The consumer slot after the swap: a fresh bind takes it;
        leaving interactive unbinds the old one AFTER the chain
        stopped referencing the queue — stopped-first would drop
        every packet still headed for it (an unbound queue
        drops)."""
        if fresh is not None:
            services.consumer = fresh
            return
        if not policy.interactive and services.consumer is not None:
            services.consumer.stop()
            services.consumer = None

    def interceptor_port(self, workspace_id: str) -> int | None:
        """The interceptor's armed port for a workspace, None while
        disarmed — a table swap keeps the redirect half exactly as
        it found it (#199's swap, retargeted by #280)."""
        return self.app.state.interceptor.armed_port(workspace_id)

    async def apply_interception(
        self, workspace_id: str, port: int | None
    ) -> None:
        """Swap one workspace's table with or without the
        interceptor's rules (#199): the whole table re-applies in one
        nft transaction, so the redirect and its absence never leave
        a window where the table is gone. The swap carries the
        kernel-side consent elements across itself — verdict pins
        and resolver-learned allows would die with the table
        otherwise (#260 review), and a static workspace's learned
        egress would drop until its DNS cache expired.

        Under the workspace's net guard (#280 review, round 2): the
        watcher's placeholder sweep and the placeholder routes call
        this with no route lock, and an unserialized install here
        can land after a mode switch — restoring a chain that
        references a queue whose consumer the switch already
        stopped (an unbound queue drops every new flow), or
        clobbering a fresh interactive chain with a queue-less
        one. The guard is re-entrant, so the arm/disarm an attach
        or detach itself drives still runs."""
        async with self.workspace_guard(workspace_id):
            await self.swap_interception(workspace_id, port)

    async def swap_interception(
        self, workspace_id: str, port: int | None
    ) -> None:
        """The interception swap's body (guard held by the
        caller)."""
        attachment = self._attachments.get(workspace_id)
        services = self._services.get(workspace_id)
        if attachment is None or services is None:
            return  # not attached: the table does not exist to swap
        settings = self.app.state.settings
        snapshot = await self.consent_snapshot(workspace_id, services)
        elements = nft.element_statements(
            alloc.table_name(workspace_id), snapshot
        )
        await nft.install_vm(
            settings,
            workspace_id,
            attachment.tap,
            attachment.guest_ip,
            attachment.tap_ip,
            policy=services.policy,
            queue_num=services.queue_num,
            interceptor_port=port,
            elements=elements,
        )

    async def consent_snapshot(self, workspace_id: str, services) -> dict:
        """The consent elements a swap must carry: gated modes only —
        an allow-mode workspace has no sets to carry."""
        if services.policy is None or not services.policy.gated:
            return {}
        return await nft.dump_consent_elements(
            self.app.state.settings, workspace_id
        )

    async def consent_allow(
        self,
        workspace_id: str,
        ip: str,
        port: int | None,
        ttl_s: float,
        *,
        named: bool = True,
    ) -> None:
        """Pin one destination as allowed (the verdict/DNS-learn
        path). A workspace without a live attachment has no table
        to pin into, and a live one in allow mode carries no
        consent sets — nothing to enforce, nothing to do (the
        resolver gate still LEARNs under a carried forever allow,
        #280 review: the pin must not spawn a doomed nft run per
        answer). A NAME pin on a shared address — more than one
        live name resolved to it (#304) — installs nothing: the
        element is keyed by address alone and would let the
        verdict cover its co-resident, so the next SYN gates at
        the queue under the naming memory instead. Address-literal
        verdicts (``named=False``) pin regardless — the verdict
        was given on the address itself."""
        services = self._services.get(workspace_id)
        if services is None or not services.policy.gated:
            return
        if named and services.dns.shared(ip):
            logger.debug(
                "consent: shared address %s unpinned for %s (#304)",
                ip,
                workspace_id,
            )
            return
        await nft.allow_element(
            self.app.state.settings, workspace_id, ip, port, ttl_s
        )

    async def consent_reject(
        self,
        workspace_id: str,
        ip: str,
        port: int,
        ttl_s: float,
        sport: int | None = None,
        *,
        named: bool = True,
    ) -> None:
        """Pin one destination port for RST-refusal (the deny path)
        — gated postures only, the same shape as an allow pin. A
        NAME deny on a shared address refuses only its own
        connection: the element lands in the per-flow set keyed by
        the connection's source port (#304), so the co-resident's
        connections never see the refusal. An address-literal
        verdict (``named=False``) pins the blanket element — the
        verdict was given on the address itself."""
        services = self._services.get(workspace_id)
        if services is None or not services.policy.gated:
            return
        settings = self.app.state.settings
        if reject_is_per_flow(services, ip, sport, named):
            await nft.reject_flow_element(
                settings, workspace_id, ip, sport, port, ttl_s
            )
            return
        await nft.reject_element(settings, workspace_id, ip, port, ttl_s)

    async def clear_consent_dest(
        self, workspace_id: str, host: str, port: int
    ) -> None:
        """Revocation's enforcement clear: drop the flow elements
        and the tracked connections for one verdict's destination —
        every address its name resolved to (the naming memory),
        plus the host itself when the verdict was given by address.
        New connections re-gate; established ones die with their
        conntrack entries (klangk never needed this — its sidecar's
        netns died with the container). The per-flow RST set
        flushes wholesale with it: its elements name connections,
        not verdicts, and a dropped one re-pins on the flow's next
        retransmit (#304)."""
        attachment = self._attachments.get(workspace_id)
        if attachment is None:
            return
        await self.flush_flow_rejects(workspace_id)
        for ip in self.dest_addresses(workspace_id, host):
            await nft.clear_elements(
                self.app.state.settings,
                workspace_id,
                ip,
                None if port == 0 else port,
            )
            await self.drop_flows(workspace_id, attachment.guest_ip, ip)

    async def flush_flow_rejects(self, workspace_id: str) -> None:
        """Empty the per-flow RST set on revocation (#304): its
        elements name connections, not verdicts, and a dropped one
        re-pins on the flow's next retransmit through the session
        gate — self-healing, so the flush is safe. Only an
        interactive table defines the set."""
        services = self._services.get(workspace_id)
        if services is not None and services.policy.interactive:
            await nft.flush_set(
                self.app.state.settings, workspace_id, "rejects_flow"
            )

    async def retract_consent_pins(
        self, workspace_id: str, ips: list[str]
    ) -> None:
        """Drop the consent elements addresses carried before a
        second name resolved to them (the naming layer's
        co-residency retraction, #304): an address-keyed element
        on a shared address enforces one name's verdict on its
        co-resident. Best-effort — a missed retraction is a window
        bounded by the element's own timeout."""
        for ip in ips:
            await nft.clear_ip_elements(
                self.app.state.settings, workspace_id, ip
            )

    def dest_addresses(self, workspace_id: str, host: str) -> list[str]:
        """The addresses a verdict's destination covers: the
        name's live pairings from the forwarder's memory, plus the
        host itself for an address-literal verdict. The name's
        pairings are forgotten as a side effect."""
        services = self._services.get(workspace_id)
        targets: list[str] = []
        if services is not None:
            targets = services.dns.ips_for(host)
            services.dns.forget(host)
        if is_ipv4(host):
            targets.append(host)
        return list(dict.fromkeys(targets))

    async def drop_flows(
        self, workspace_id: str, guest_ip: str, ip: str
    ) -> None:
        """Delete the guest's tracked connections to one address
        (best-effort: the tool is a setting; a missing entry or a
        missing tool logs, never fails the revoke)."""
        tool = self.app.state.settings.net.conntrack_tool
        try:
            await conntrack.delete_flows(tool, guest_ip, ip)
        except (MicrovmError, TimeoutError) as exc:
            logger.warning(
                "consent revoke for %s: conntrack clear to %s skipped (%s)",
                workspace_id,
                ip,
                exc,
            )

    def host_for(self, workspace_id: str, ip: str) -> str | None:
        """The DNS name that resolved to an address (the prompts'
        naming), if this workspace's forwarder remembers one."""
        services = self._services.get(workspace_id)
        if services is None:
            return None
        return services.dns.host_for(ip)

    async def replay_boot_verdicts(
        self, workspace_id: str, policy: EgressPolicy
    ) -> None:
        """Pin a fresh boot's durable verdicts, gated modes only:
        an allow-mode table carries no consent sets, so the pins
        would fail the boot — reachable once a switch leaves a
        workspace in allow mode carrying forever verdicts
        (#280)."""
        if policy.gated:
            await self.replay_forever(workspace_id)

    async def replay_forever(self, workspace_id: str) -> None:
        """Pin a fresh boot's durable verdicts: every in-effect
        ``forever`` allow/deny given by *address* re-pins its flow
        element (name-keyed verdicts need nothing here — the
        resolver gate reads their rows live). Best-effort: a missed
        pin re-prompts, which is the correct fallback, not a leak."""
        rows = await self.forever_rows_quietly(workspace_id)
        for row in rows:
            await self.replay_row(workspace_id, row)

    async def forever_rows_quietly(self, workspace_id: str) -> list[dict]:
        """The workspace's forever verdicts, or [] when the read
        fails (a missed pin re-prompts — the correct fallback, not
        a leak)."""
        try:
            return await self.app.state.model.egress_consent.forever_rows(
                workspace_id
            )
        except Exception:
            logger.exception(
                "consent replay for %s failed; verdicts re-prompt",
                workspace_id,
            )
            return []

    async def replay_row(self, workspace_id: str, row: dict) -> None:
        """Re-pin one forever verdict given by address."""
        if not is_ipv4(row["dest_host"]):
            return  # a name: the resolver gate reads its row live
        port = None if row["dest_port"] == 0 else row["dest_port"]
        if row["decision"] == DECISION_ALLOWED:
            await self.consent_allow(
                workspace_id,
                row["dest_host"],
                port,
                LONG_TTL_S,
                named=False,
            )
        elif port is not None:
            await self.consent_reject(
                workspace_id,
                row["dest_host"],
                port,
                LONG_TTL_S,
                named=False,
            )

    async def _build(
        self, workspace_id: str, policy: EgressPolicy
    ) -> NetAttachment:
        """Create tap + chain + services (+ consent queue) for one
        workspace."""
        settings = self.app.state.settings
        slice_ = await self.claim_slice(workspace_id)
        consumer = None
        try:
            net = alloc.slice_net(settings.net.pool, slice_)
            attachment = NetAttachment(
                workspace_id=workspace_id,
                tap=alloc.tap_name(workspace_id),
                mac=alloc.guest_mac(workspace_id),
                guest_ip=str(alloc.guest_addr(net)),
                tap_ip=str(alloc.tap_addr(net)),
                slice=slice_,
            )
            queue_num = None
            if policy.interactive:
                queue_num = self.queue_for(slice_)
                # Bind the queue BEFORE the chain references it: an
                # unbound queue drops, and a boot that cannot bind
                # (no netfilterqueue binding) refuses outright with
                # a named error rather than running a queue nothing
                # answers.
                consumer = self._consumer_factory(
                    workspace_id, queue_num, self
                )
                consumer.start()
            try:
                await taps.create_tap(
                    attachment.tap,
                    f"{attachment.tap_ip}/{alloc.SLICE_PREFIX}",
                    settings,
                )
                await nft.install_vm(
                    settings,
                    workspace_id,
                    attachment.tap,
                    attachment.guest_ip,
                    attachment.tap_ip,
                    policy=policy,
                    queue_num=queue_num,
                )
            except BaseException:
                if consumer is not None:
                    consumer.stop()
                raise
            await self._start_services(attachment, policy, consumer, queue_num)
            self._attachments[workspace_id] = attachment
            await self.replay_boot_verdicts(workspace_id, policy)
            # The interceptor arms last (#199): its listener and the
            # table's redirect half appear together, only when a
            # placeholder wants them.
            await self.app.state.interceptor.refresh(workspace_id)
            return attachment
        except BaseException:
            self._used_slices.discard(slice_)
            await self._unwind(workspace_id)
            raise

    async def _start_services(
        self,
        attachment: NetAttachment,
        policy: EgressPolicy,
        consumer: FlowConsumer | None = None,
        queue_num: int | None = None,
    ) -> None:
        """Bring up DHCP + DNS on the tap and start serving.

        The services record registers before anything starts, so a
        failed start still gets a teardown path: ``_build``'s unwind
        stops whatever had started.
        """
        settings = self.app.state.settings.net
        dhcp_server = self._dhcp_factory(
            attachment.tap_ip,
            attachment.guest_ip,
            self.netmask(),
            settings.lease_s,
            device=attachment.tap,
        )
        gate = ResolverGate(
            policy,
            self.app.state.consent,
            self.app.state.model.egress_consent,
            self,
        )
        forwarder = self._dns_factory(
            self.dns_upstream(),
            settings.dns_timeout_s,
            bind=(attachment.tap_ip, dns.DNS_PORT),
            client_ip=attachment.guest_ip,
            gate=gate,
        )
        services = NetServices(
            dhcp=dhcp_server,
            dns=forwarder,
            tasks=[],
            gate=gate,
            consumer=consumer,
            policy=policy,
            queue_num=queue_num,
        )
        self._services[attachment.workspace_id] = services
        try:
            await dhcp_server.start()
            await forwarder.start()
        except OSError as exc:
            # A refused bind (the ports are privileged) is an
            # operator-shaped failure, not a raw 500.
            self._stop_started(services)
            raise MicrovmError(
                f"egress services for {attachment.workspace_id} "
                f"failed to start: {exc}"
            ) from exc
        except BaseException:
            self._stop_started(services)
            raise
        services.tasks = [
            asyncio.create_task(dhcp_server.serve()),
            asyncio.create_task(forwarder.serve()),
        ]
        await self.start_llm(services, attachment)

    async def start_llm(
        self, services: NetServices, attachment: NetAttachment
    ) -> None:
        """Bind this tap's LLM proxy listener when one is warranted
        (#259).

        Best-effort by design: DHCP and DNS are the workspace's
        network, but the proxy is an auxiliary surface — a refused
        bind (another daemon on the port, an address the host
        cannot bind) logs loudly and the boot proceeds, leaving
        that workspace without the LLM surface until its next
        start. A model entry that cannot resolve is not seen here
        at all — entries parse at the first request, where a failed
        configure answers a named 503. The listener's absence
        leaves the input chain's admission pointing at a closed
        port — connection refused, harmless."""
        llm = self.app.state.llm
        if llm is None:
            return
        factory = self._llm_factory or (lambda att: llm.listener_for(att))
        try:
            listener = factory(attachment)
            if listener is None:
                return
            await llm.start_listener(attachment.workspace_id, listener)
        except Exception as exc:
            print(
                f"msksd: LLM proxy for {attachment.workspace_id} "
                f"did not start ({exc}); the workspace boots "
                "without it",
                flush=True,
            )

    def dns_upstream(self) -> tuple[str, int]:
        """Where the forwarder relays: the setting, else resolv.conf."""
        settings = self.app.state.settings.net
        if settings.dns_upstream:
            return (settings.dns_upstream, dns.DNS_PORT)
        upstream = dns.upstream_from_resolv()
        if upstream is None:
            raise MicrovmError(
                "no upstream resolver: set MSKSD_EGRESS_DNS_UPSTREAM"
            )
        return upstream

    def _stop_started(self, services: NetServices) -> None:
        """Stop both services symmetrically after a failed start."""
        services.dhcp.stop()
        services.dns.stop()

    async def _unwind(self, workspace_id: str) -> None:
        """Roll back a half-built attachment (best effort)."""
        with contextlib.suppress(Exception):
            await self.stop_llm(workspace_id)
        services = self._services.pop(workspace_id, None)
        if services is not None:
            await stop_services(services)
        settings = self.app.state.settings
        await self.app.state.interceptor.on_detach(workspace_id)
        await nft.delete_vm_table(settings, workspace_id)
        await taps.remove_tap(alloc.tap_name(workspace_id), settings)


async def stop_services(services: NetServices) -> None:
    """Stop one workspace's service tasks and sockets.

    The consumer unbinds first (fail-closed), then the cancelled
    tasks are gathered so their cleanup (including pending reader
    removal) lands before the caller moves on. The LLM listener is
    not here: detach's own stop_llm owns it, before this runs.
    """
    services.stop_consumer()
    for task in services.tasks:
        task.cancel()
    if services.tasks:
        await asyncio.gather(*services.tasks, return_exceptions=True)
    services.dhcp.stop()
    services.dns.stop()
