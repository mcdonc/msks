"""The forward stream and proxy lifecycle half of the
net manager.
"""

from __future__ import annotations

import contextlib

from ...spec.failures import MicrovmError
from .helpers import NetAttachment, dial_with_retry


class ForwardMixin:
    """Composed into NetManager; every method speaks the
    manager's own attributes."""

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

    async def remote_ports(self, workspace_id: str) -> list[int]:
        """The workspace's remote proxy listen ports that carry a
        LIVE listener (#807 review): the admission set every table
        install carries for the tap. A row whose bind failed
        admits nothing — the accept line would open the port to
        whatever squats it (the probe listener, the interceptor,
        the daemon's own API on a wildcard bind), the one hole the
        ingress chain exists to prevent."""
        return await self.app.state.proxies.bound_remote_ports(workspace_id)

    async def stop_proxies(self, workspace_id: str) -> None:
        """End the workspace's live proxy listeners (#807):
        unconditional and idempotent, the same contract stop_llm
        and stop_probe carry."""
        await self.app.state.proxies.stop_workspace(workspace_id)

    async def start_proxies(self, attachment: NetAttachment) -> None:
        """Bring up the workspace's persisted proxy listeners
        (#807) — the same best-effort posture the LLM and probe
        listeners carry: a subsystem-level failure (the row read)
        logs loudly and the boot proceeds; a single proxy's bind
        failure is the manager's own per-row concern."""
        try:
            await self.app.state.proxies.start_workspace(
                attachment.workspace_id, attachment
            )
        except Exception as exc:
            print(
                f"msksd: proxies for {attachment.workspace_id} "
                f"did not start ({exc}); the workspace boots "
                "without them",
                flush=True,
            )

    async def stop_llm(self, workspace_id: str) -> None:
        """Stop the workspace's LLM listener when one serves (#259).
        Unconditional and idempotent: the services' own stop is the
        backstop, this one keeps the proxy's mapping honest."""
        if self.app.state.llm is not None:
            await self.app.state.llm.stop_listener(workspace_id)

    async def stop_probe(self, workspace_id: str) -> None:
        """Stop the workspace's probe listener when one serves
        (#424): unconditional and idempotent, the same contract
        stop_llm carries."""
        if self.app.state.probe is not None:
            await self.app.state.probe.stop_listener(workspace_id)

    async def refresh_proxies(self, workspace_id: str) -> None:
        """Re-apply the workspace's table with its current remote
        proxy ports (#807): a proxy added or removed on a running
        workspace swaps its admission in the one-transaction shape
        every other live change uses. The interceptor's armed half
        stays exactly as it stands, and the consent elements ride
        across the swap like every other one does.

        Under the workspace's net guard, the same serialization
        the interception swap holds: an install landing between an
        attach's table and its services would clobber one of them.
        """
        async with self.workspace_guard(workspace_id):
            await self.swap_interception(
                workspace_id, self.interceptor_port(workspace_id)
            )

    async def admit_bound_proxies(self, workspace_id: str) -> None:
        """Swap the table when a boot's proxy binds came up (#807
        review): nothing to swap when none did. Best-effort like
        every proxy step in a boot — an nft refusal here cannot
        make the workspace unbootable. The refusal is fail-closed
        (no admission means the guest cannot reach the port) and
        named per row, so the listings keep showing it until a
        later swap lands."""
        if not await self.remote_ports(workspace_id):
            return
        try:
            await self.refresh_proxies(workspace_id)
        except Exception as exc:
            print(
                f"msksd: proxy admissions for {workspace_id} "
                f"did not apply ({exc}); the workspace boots without "
                "them",
                flush=True,
            )
            self.app.state.proxies.note_admission_failure(
                workspace_id, f"admission refresh failed: {exc}"
            )
