"""The policy switch half of the net manager: the live
swap, its two-phase commit, and the interception tables.
"""

from __future__ import annotations

from .. import alloc, nft
from .helpers import EgressPolicy


class SwitchMixin:
    """Composed into NetManager; every method speaks the
    manager's own attributes."""

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
            proxy_ports=await self.remote_ports(workspace_id),
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
            proxy_ports=await self.remote_ports(workspace_id),
            elements=elements,
        )
