"""The net manager core: attach/detach lifecycle and services.

:class:`NetManager` composes the forward, switch, and consent
mixins beside its own lifecycle and slice allocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from ipaddress import IPv4Network

from ...spec.failures import MicrovmError
from .. import alloc, dns, nft, taps
from ..dhcp import DhcpServer
from ..dns import DnsForwarder, ResolverGate
from ..nfq import FlowConsumer
from .consent import ConsentMixin
from .forward import ForwardMixin
from .helpers import (
    NOT_READY_CAUSES,
    EgressPolicy,
    NetAttachment,
    NetServices,
    WorkspaceGuard,
    stop_services,
    verify_forwarding,
)
from .switch import SwitchMixin

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


class NetManager(ConsentMixin, SwitchMixin, ForwardMixin):
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
        probe_factory=None,
    ) -> None:
        self.app = app
        self._dhcp_factory = dhcp_factory
        self._dns_factory = dns_factory
        self._consumer_factory = consumer_factory
        # The per-tap LLM listener seam (#259): the default asks the
        # llm subsystem; the tests inject one that records the ask.
        self._llm_factory = llm_factory
        # The per-tap probe listener seam (#424), same shape: the
        # default asks the probe service.
        self._probe_factory = probe_factory
        # The guest-dial seam for the forward websocket (#109): the
        # default dials real TCP; the tests inject one that answers
        # from a listener they control (fake_ch has no NIC).
        self.dialer = dialer or asyncio.open_connection
        self._attachments: dict[str, NetAttachment] = {}
        self._services: dict[str, NetServices] = {}
        # Attachments a failed detach left behind (#828): the slice
        # released, but the table and tap deletes raised — keyed for
        # the next detach's retry, superseded by a fresh attach.
        self._residues: dict[
            str, tuple[NetAttachment, NetServices | None]
        ] = {}
        self._guards: dict[str, WorkspaceGuard] = {}
        self._forwards: dict[str, list] = {}
        self._used_slices: set[int] = set()
        # The base table's loopback-guard shape the running ruleset
        # carries (#483) — the shape, not the settings, says whether
        # a re-apply is due.
        self._base_guard_shape: tuple[str, int] | None = None
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
            self._base_guard_shape = nft.base_guard_shape(settings)
        except (MicrovmError, OSError) as exc:
            # Loud, not fatal: workspaces without egress are unaffected;
            # every egress boot below refuses with this cause.
            print(f"msksd: egress unavailable: {exc}", flush=True)
            self._state = "unavailable"
            return
        self._state = "ready"

    async def stop(self) -> None:
        """Detach every workspace (daemon shutdown; idempotent).
        The proxy sweep runs after: a forward listener can outlive
        its workspace's attachment, and shutdown ends it too. Each
        workspace's teardown is its own failure (#828 review): one
        raising detach stashes its residue for the retry pass below
        and the loop moves on, so a single broken workspace cannot
        skip the proxy sweep, the interceptor's stop, or the model's
        close."""
        for workspace_id in list(self._attachments):
            with contextlib.suppress(Exception):
                await self.detach(workspace_id)
        for workspace_id in list(self._residues):
            with contextlib.suppress(Exception):
                await self.detach(workspace_id)
        await self.app.state.proxies.stop_all()
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

    def attachment(self, workspace_id: str) -> NetAttachment | None:
        """The workspace's live attachment, None when not attached —
        the read the proxy subsystem (#807) makes to decide a
        remote listener's bind address."""
        return self._attachments.get(workspace_id)

    async def detach(self, workspace_id: str) -> None:
        """Tear one workspace's egress down (idempotent).

        Releasing the slice keeps the workspace on its own /30 across
        stop/start cycles — the address derives from it, and nothing
        else remembers the pairing. The claim holds until the
        teardown completes (#828): a raise in the teardown stashes
        the popped attachment as residue and the claim stays, so no
        other workspace can take the /30 while its tap lingers. The
        workspace's next stop retries the residue's teardown, and a
        fresh start retries or supersedes it (the launch path
        detaches before it attaches; the manager's own attach stops
        the residue's services, transfers its claim without
        releasing it, and rebuilds the plumbing).
        """
        async with self.workspace_guard(workspace_id):
            attachment = self._attachments.pop(workspace_id, None)
            services = self._services.pop(workspace_id, None)
            residue = self._residues.pop(workspace_id, None)
            self.close_forwards(workspace_id)
            with contextlib.suppress(Exception):
                # Best-effort like the rest of the teardown: a listener
                # whose serve task already died badly must not abort the
                # table and tap cleanup below it.
                await self.stop_llm(workspace_id)
            with contextlib.suppress(Exception):
                await self.stop_probe(workspace_id)
            with contextlib.suppress(Exception):
                await self.stop_proxies(workspace_id)
            if attachment is None and residue is None:
                return
            if attachment is None:
                attachment, services = residue
            try:
                await self.teardown_plumbing(
                    workspace_id, attachment, services
                )
            except BaseException:
                self._residues[workspace_id] = (attachment, services)
                raise
            self._used_slices.discard(attachment.slice)

    async def teardown_plumbing(
        self,
        workspace_id: str,
        attachment: NetAttachment,
        services: NetServices | None,
    ) -> None:
        """The teardown steps a detach runs below its pops (#828) —
        and the same steps a residue retry runs. The consent stop
        hook runs first (holds fail-close while the table still
        exists to receive their drops), the consumer unbinds before
        the table delete (an unbound queue drops — fail-closed), and
        the plumbing subprocesses run before the service sockets
        close, so a closed-socket fd number is never reused by a
        fresh subprocess pipe underneath a stale selector entry.
        Every step is idempotent: a retry re-runs the whole body.
        """
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

    def require_ready(self, workspace_id: str) -> None:
        """Refuse an egress boot unless the plumbing is armed."""
        if self._state == "ready":
            return
        cause = NOT_READY_CAUSES.get(self._state, self._state)
        raise MicrovmError(
            f"workspace {workspace_id} requests egress but {cause}"
        )

    async def _build(
        self, workspace_id: str, policy: EgressPolicy
    ) -> NetAttachment:
        """Create tap + chain + services (+ consent queue) for one
        workspace."""
        settings = self.app.state.settings
        await self.ensure_base_guard()
        await self.stop_residue_services(workspace_id)
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
                # (the library did not load) refuses outright with
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
                    proxy_ports=await self.remote_ports(workspace_id),
                )
            except BaseException:
                if consumer is not None:
                    consumer.stop()
                raise
            await self._start_services(attachment, policy, consumer, queue_num)
            self._attachments[workspace_id] = attachment
            # The live attachment supersedes any residue (#828): the
            # build swept its leftover tap, replaced its leftover
            # table, stopped its services, and its claim transferred
            # above — nothing of the residue is left to clean.
            self._residues.pop(workspace_id, None)
            await self.replay_boot_verdicts(workspace_id, policy)
            # The interceptor arms last (#199): its listener and the
            # table's redirect half appear together, only when a
            # placeholder wants them.
            await self.app.state.interceptor.refresh(workspace_id)
            # The proxies' admissions land after their binds (#807
            # review): the table above installed with the ports
            # already listening (an attach starts from none), and
            # this swap adds exactly the binds that came up. A
            # failed bind stays unadmitted.
            await self.admit_bound_proxies(workspace_id)
            return attachment
        except BaseException:
            await self.unwind_failed_build(workspace_id, slice_)
            raise

    async def unwind_failed_build(
        self, workspace_id: str, slice_: int
    ) -> None:
        """A failed build's unwind (#828 review): the claim releases
        unless the workspace's own residue still holds it — that
        record is still there to retry, and its leftover plumbing
        may still address the /30 — and the half-built plumbing
        tears down best-effort (the residue's leftovers carry the
        same names, so one sweep cleans both)."""
        residue = self._residues.get(workspace_id)
        if residue is None or residue[0].slice != slice_:
            self._used_slices.discard(slice_)
        await self._unwind(workspace_id)

    async def stop_residue_services(self, workspace_id: str) -> None:
        """Stop a residue's still-live services before a superseding
        build binds its own (#828 review): the raise that stashed
        the residue preceded the services' stop, and a forwarder
        still bound to this slice's address would refuse the fresh
        services their bind. The record and the claim stay — the
        build's claim below transfers without releasing, and a
        failed build leaves the residue retryable. A cancellation
        here changes nothing the record did not already say: the
        services stop is idempotent, so the next attempt re-runs
        it."""
        residue = self._residues.get(workspace_id)
        if residue is None or residue[1] is None:
            return
        with contextlib.suppress(Exception):
            await stop_services(residue[1])

    async def ensure_base_guard(self) -> None:
        """Re-apply the base table when its loopback guard's shape
        drifted from the live settings (#483).

        The guard is baked from the settings at start; the listeners
        bind from the LIVE settings at attach — a SIGHUP that added
        a model list (or moved the port, or the pool) after startup
        would otherwise leave the next listener bound behind a
        guard-less table. Attach is the one path that binds from the
        live shape, so the drift check lives here: the shape the
        running ruleset carries, not the stamp-time settings, says
        whether a re-apply is due."""
        shape = nft.base_guard_shape(self.app.state.settings)
        if shape != self._base_guard_shape:
            await nft.apply_base(self.app.state.settings)
            self._base_guard_shape = shape

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
        await self.start_probe(attachment)
        await self.start_proxies(attachment)

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

    async def start_probe(self, attachment: NetAttachment) -> None:
        """Bind this tap's probe listener (#424) — the same
        best-effort posture the LLM listener carries (#259): a
        refused bind (another daemon on the port, an address the
        host cannot bind) logs loudly and the boot proceeds,
        leaving that workspace without the probe endpoint until
        its next start. The port is the setting's own per-tap
        number (#532), not the guest's 443: the interceptor's
        dial is repointed to it, so a host wildcard listener on
        443 cannot squat the bind."""
        probe = self.app.state.probe
        if probe is None:
            return
        factory = self._probe_factory or self.default_probe
        try:
            listener = await factory(attachment)
            if listener is None:
                return
            await probe.start_listener(attachment.workspace_id, listener)
        except Exception as exc:
            print(
                f"msksd: probe endpoint for {attachment.workspace_id} "
                f"did not start ({exc}); the workspace boots "
                "without it",
                flush=True,
            )

    async def default_probe(self, attachment):
        """The real listener path: the service's TLS material, then
        its listener for this tap (the seam's tests replace this
        whole half)."""
        certfile, keyfile = await self.app.state.probe.material()
        return self.app.state.probe.listener_for(attachment, certfile, keyfile)

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
        with contextlib.suppress(Exception):
            await self.stop_probe(workspace_id)
        with contextlib.suppress(Exception):
            await self.stop_proxies(workspace_id)
        services = self._services.pop(workspace_id, None)
        if services is not None:
            await stop_services(services)
        settings = self.app.state.settings
        await self.app.state.interceptor.on_detach(workspace_id)
        await nft.delete_vm_table(settings, workspace_id)
        await taps.remove_tap(alloc.tap_name(workspace_id), settings)

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
        """Mark a recorded slice live, refusing a conflicting holder.
        A slice the workspace's own residue still holds transfers
        through a superseding build: releasing and re-claiming it
        would be two steps with a steal window between them, and the
        residue's dead tap still addresses the /30."""
        if slice_ not in self._used_slices:
            self._used_slices.add(slice_)
            return
        residue = self._residues.get(workspace_id)
        if residue is not None and residue[0].slice == slice_:
            return
        raise MicrovmError(
            f"egress slice {slice_} recorded for {workspace_id} is "
            "held by another live workspace; delete one of them"
        )

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

    def attached_workspaces(self) -> list[str]:
        """Every workspace with a live attachment, in attach order
        (#339): the daemon-wide placeholder refresh reads this — a
        row that covers the whole daemon arms and disarms with
        every attached workspace, and its sentinel rides every
        armed tap's entry table."""
        return list(self._attachments)
