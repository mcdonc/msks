"""The egress-consent half of the net manager: verdicts,
pins, retracts, and the flow drops a reject takes.
"""

from __future__ import annotations

import logging

from ...spec.failures import MicrovmError
from .. import conntrack, nft
from .helpers import EgressPolicy, VerdictPin, is_ipv4, reject_is_per_flow

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


class ConsentMixin:
    """Composed into NetManager; every method speaks the
    manager's own attributes."""

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
        answer). A NAME pin in an INTERACTIVE posture on a shared
        address — more than one live name resolved to it (#304) —
        installs nothing: the element is keyed by address alone
        and would let the verdict cover its co-resident, so the
        next SYN gates at the queue under the naming memory
        instead. Address-literal verdicts (``named=False``) and
        every static-mode pin act regardless — a literal verdict
        was given on the address itself, and a static chain has
        no queue to gate a skipped pin's connections."""
        services = self._services.get(workspace_id)
        if services is None or not services.policy.gated:
            return
        settings = self.app.state.settings
        if self.skips_shared_pin(services, ip, named):
            logger.debug(
                "consent: shared address %s unpinned for %s (#304)",
                ip,
                workspace_id,
            )
            return
        await nft.allow_element(settings, workspace_id, ip, port, ttl_s)
        # The shared mark may have flipped while the add ran (a
        # second name resolved mid-pin): reconcile by withdrawing
        # the now-over-broad element (#304 review, round 2).
        if self.skips_shared_pin(services, ip, named):
            await self.retract_address(workspace_id, ip)

    def skips_shared_pin(self, services, ip: str, named: bool) -> bool:
        """Whether this allow pin must not install: a name-derived
        pin in an interactive posture on an address two live
        names resolved to."""
        return bool(
            named and services.policy.interactive and services.dns.shared(ip)
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
        NAME deny on a shared address in an interactive posture
        refuses only its own connection: the element lands in the
        per-flow set keyed by the connection's source port (#304),
        so the co-resident's connections never see the refusal.
        An address-literal verdict (``named=False``) and every
        static-mode deny pin the blanket element."""
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
        # The shared mark may have flipped while the add ran: a
        # blanket RST on a now-shared address answers the
        # co-resident's connections too — swap it for the
        # connection's own per-flow element (#304 review, round 2).
        if reject_is_per_flow(services, ip, sport, named):
            await nft.clear_elements(settings, workspace_id, ip, port)
            await nft.reject_flow_element(
                settings, workspace_id, ip, sport, port, ttl_s
            )

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
        re-gates on the flow's next retransmit — the session memory
        or the verdict cache answers it, and past both windows it
        re-prompts (fail-closed). Only an interactive table defines
        the set."""
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
            await self.retract_address(workspace_id, ip)

    async def retract_address(self, workspace_id: str, ip: str) -> None:
        """Withdraw one address's consent elements and restore the
        verdicts that legitimately survive co-residency: the clear
        cannot tell a name's pin from an address-literal one, so
        every literal ``forever`` verdict re-pins (a literal
        verdict is address-scoped by construction — #304 review,
        round 2)."""
        settings = self.app.state.settings
        await nft.clear_ip_elements(settings, workspace_id, ip)
        await self.replay_forever(workspace_id)

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
        """Pin a fresh boot's durable verdicts: the consent side
        hands over decided pins — action, destination, TTL — for
        every in-effect ``forever`` verdict given by address; the
        resolver gate reads name-keyed verdicts from their rows
        live. Best-effort: a missed pin re-prompts, which is the
        correct fallback, not a leak."""
        for pin in await self.app.state.consent.forever_pins(workspace_id):
            await self.apply_pin(workspace_id, pin)

    async def apply_pin(self, workspace_id: str, pin: VerdictPin) -> None:
        """Install one decided verdict pin (#401): address-literal
        by construction, so the pin's destination is the address
        itself."""
        if pin.allowed:
            await self.consent_allow(
                workspace_id, pin.ip, pin.port, pin.ttl_s, named=False
            )
            return
        await self.consent_reject(
            workspace_id, pin.ip, pin.port, pin.ttl_s, named=False
        )
