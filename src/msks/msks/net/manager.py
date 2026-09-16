"""The egress attachment lifecycle: one tap + services per workspace (#52).

NetManager is the state object on ``app.state.net``. Enabled and
privileged, ``start()`` arms the shared plumbing once (the NAT
base table, after verifying the kernel's ip_forward sysctl — the
appliance ships it as a boot-time setting, and the daemon never
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
from dataclasses import dataclass
from ipaddress import IPv4Network
from pathlib import Path

from ..microvm.errors import MicrovmError
from . import alloc, dns, nft, taps
from .dhcp import DhcpServer
from .dns import DnsForwarder

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
            return await asyncio.wait_for(dialer(host, port), deadline - loop.time())
        except (OSError, TimeoutError) as exc:
            last_cause = str(exc) or last_cause
            await asyncio.sleep(FORWARD_POLL_S)
    raise MicrovmError(
        f"forward to {host}:{port} unavailable: {last_cause or DIAL_DEADLINE_EXPIRED}"
    )


NOT_READY_CAUSES = {
    "init": "the egress subsystem never started",
    "disabled": (
        "egress is not enabled (MSKSD_EGRESS_ENABLED, read at startup "
        "— set it and restart)"
    ),
    "unavailable": (
        "the daemon could not arm egress (it needs CAP_NET_ADMIN and "
        "CAP_NET_BIND_SERVICE — the appliance grants both, and only "
        "those, to its service user — plus " + SYSCTL_KEY + "=1 from "
        "sysctl.d; a dev-shell daemon has none of them)"
    ),
}


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
    """One workspace's live DHCP + DNS tasks."""

    dhcp: DhcpServer
    dns: DnsForwarder
    tasks: list[asyncio.Task]


def verify_forwarding(path: Path = FORWARDING) -> None:
    """Refuse egress unless the kernel already routes packets (#101).

    ``ip_forward`` is part of the machine's identity: the appliance
    ships ``net.ipv4.ip_forward=1`` as a boot-time sysctl.d setting,
    and the daemon — a service user without write access to
    /proc/sys — verifies it and fails closed. A daemon that finds
    ``0`` records itself unavailable, and every egress workspace
    boot refuses with a cause naming the sysctl key.
    """
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise MicrovmError(f"could not read {SYSCTL_KEY} ({path}): {exc}") from exc
    if value != "1":
        raise MicrovmError(
            f"{SYSCTL_KEY} is not enabled (reads {value!r}); enable it "
            "at boot with sysctl.d and restart the daemon"
        )


class NetManager:
    """Owns every workspace's egress plumbing."""

    def __init__(
        self, app, *, dhcp_factory=DhcpServer, dns_factory=DnsForwarder, dialer=None
    ) -> None:
        self.app = app
        self._dhcp_factory = dhcp_factory
        self._dns_factory = dns_factory
        # The guest-dial seam for the forward websocket (#109): the
        # default dials real TCP; the tests inject one that answers
        # from a listener they control (fake_ch has no NIC).
        self.dialer = dialer or asyncio.open_connection
        self._attachments: dict[str, NetAttachment] = {}
        self._services: dict[str, NetServices] = {}
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

    async def attach(self, workspace_id: str, *, want: bool) -> NetAttachment | None:
        """Arm one workspace's egress; None when it asked for none.

        Idempotent per workspace: an existing attachment is returned
        as-is, so a boot racing a stop/kill cycle converges on the
        one live attachment.
        """
        if not want:
            return None
        self.require_ready(workspace_id)
        existing = self._attachments.get(workspace_id)
        if existing is not None:
            return existing
        attachment = await self._build(workspace_id)
        return attachment

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

    async def detach(self, workspace_id: str) -> None:
        """Tear one workspace's egress down (idempotent).

        Releasing the slice keeps the workspace on its own /30 across
        stop/start cycles — the address derives from it, and nothing
        else remembers the pairing. The plumbing subprocesses run
        before the service sockets close, so a closed-socket fd number
        is never reused by a fresh subprocess pipe underneath a stale
        selector entry.
        """
        attachment = self._attachments.pop(workspace_id, None)
        services = self._services.pop(workspace_id, None)
        self.close_forwards(workspace_id)
        if attachment is None:
            return
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
        raise MicrovmError(f"workspace {workspace_id} requests egress but {cause}")

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

    async def _build(self, workspace_id: str) -> NetAttachment:
        """Create tap + chain + services for one workspace."""
        settings = self.app.state.settings
        slice_ = await self.claim_slice(workspace_id)
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
            await taps.create_tap(
                attachment.tap, f"{attachment.tap_ip}/{alloc.SLICE_PREFIX}", settings
            )
            await nft.install_vm(
                settings,
                workspace_id,
                attachment.tap,
                attachment.guest_ip,
                attachment.tap_ip,
            )
            await self._start_services(attachment)
            self._attachments[workspace_id] = attachment
            return attachment
        except BaseException:
            self._used_slices.discard(slice_)
            await self._unwind(workspace_id)
            raise

    async def _start_services(self, attachment: NetAttachment) -> None:
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
        forwarder = self._dns_factory(
            self.dns_upstream(),
            settings.dns_timeout_s,
            bind=(attachment.tap_ip, dns.DNS_PORT),
            client_ip=attachment.guest_ip,
        )
        services = NetServices(dhcp=dhcp_server, dns=forwarder, tasks=[])
        self._services[attachment.workspace_id] = services
        try:
            await dhcp_server.start()
            await forwarder.start()
        except OSError as exc:
            # A refused bind (ports are the appliance's) is an
            # operator-shaped failure, not a raw 500.
            self._stop_started(services)
            raise MicrovmError(
                f"egress services for {attachment.workspace_id} failed to start: {exc}"
            ) from exc
        except BaseException:
            self._stop_started(services)
            raise
        services.tasks = [
            asyncio.create_task(dhcp_server.serve()),
            asyncio.create_task(forwarder.serve()),
        ]

    def dns_upstream(self) -> tuple[str, int]:
        """Where the forwarder relays: the setting, else resolv.conf."""
        settings = self.app.state.settings.net
        if settings.dns_upstream:
            return (settings.dns_upstream, dns.DNS_PORT)
        upstream = dns.upstream_from_resolv()
        if upstream is None:
            raise MicrovmError("no upstream resolver: set MSKSD_EGRESS_DNS_UPSTREAM")
        return upstream

    def _stop_started(self, services: NetServices) -> None:
        """Stop both services symmetrically after a failed start."""
        services.dhcp.stop()
        services.dns.stop()

    async def _unwind(self, workspace_id: str) -> None:
        """Roll back a half-built attachment (best effort)."""
        services = self._services.pop(workspace_id, None)
        if services is not None:
            await stop_services(services)
        settings = self.app.state.settings
        await nft.delete_vm_table(settings, workspace_id)
        await taps.remove_tap(alloc.tap_name(workspace_id), settings)


async def stop_services(services: NetServices) -> None:
    """Stop one workspace's service tasks and sockets.

    The cancelled tasks are gathered so their cleanup (including
    pending reader removal) lands before the caller moves on.
    """
    for task in services.tasks:
        task.cancel()
    if services.tasks:
        await asyncio.gather(*services.tasks, return_exceptions=True)
    services.dhcp.stop()
    services.dns.stop()
