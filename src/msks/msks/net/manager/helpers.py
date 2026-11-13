"""The net manager's module-level vocabulary.

The dial-with-retry the forward stream shares, the workspace
guard, the attachment and services shapes, the forwarding
sysctl check, and the per-flow reject read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ...spec.egress import EgressPolicy as EgressPolicy
from ...spec.egress import VerdictPin as VerdictPin
from ...spec.egress import is_ipv4 as is_ipv4
from ...spec.failures import MicrovmError
from ..dhcp import DhcpServer
from ..dns import DnsForwarder, ResolverGate
from ..nfq import FlowConsumer

#: The sysctl egress reads at startup; 0 refuses every egress
#: workspace with the cause named (#101).
SYSCTL_KEY = "net.ipv4.ip_forward"

#: The forwarding sysctl's own path, the check's default target.
FORWARDING = Path("/proc/sys/net/ipv4/ip_forward")

#: The forwarding check's poll interval while a first boot's
#: sysctl settles.
FORWARD_POLL_S = 0.05

#: The dial's own refusal when no cause answered under the deadline.
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
    verdict on a shared address in an interactive posture — the
    element would otherwise answer the co-resident's connections
    too. Static and literal denies pin the blanket element (the
    static chain has no queue to fall back to; a literal verdict
    was given on the address itself)."""
    return bool(
        named
        and sport
        and services.policy.interactive
        and services.dns.shared(ip)
    )


async def stop_services(services: NetServices) -> None:
    """Stop one workspace's service tasks and sockets.

    The consumer unbinds first (fail-closed), then the cancelled
    tasks are gathered so their cleanup (including pending reader
    removal) lands before the caller moves on. The LLM and probe
    listeners are not here: detach's own stop paths own them,
    before this runs.
    """
    services.stop_consumer()
    for task in services.tasks:
        task.cancel()
    if services.tasks:
        await asyncio.gather(*services.tasks, return_exceptions=True)
    services.dhcp.stop()
    services.dns.stop()
