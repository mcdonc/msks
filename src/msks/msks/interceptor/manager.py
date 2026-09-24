"""The interceptor manager: one embedded mitmproxy, per-tap
listeners (#199).

One master serves every workspace in the daemon's process. Each
armed workspace contributes one transparent-mode listener bound to
its own tap address — a ``transparent@<tap ip>:<port>`` mode spec —
and the addon dispatches by that listener. Arming is placeholder-
driven: a workspace arms while it has at least one active
placeholder (minted, unrevoked, unexpired) **and** a live egress
attachment, and disarms when either half goes away. The nft side is
a whole-table swap on every arm/disarm, so the redirect and the
listener always appear and disappear together.

The master starts lazily — a daemon that never arms a workspace
never pays mitmproxy's bring-up — and its mode list is the armed
set: adding or removing a spec reconfigures the listeners in
place.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mitmproxy import options
from mitmproxy.addons import default_addons
from mitmproxy.master import Master

from ..microvm.errors import MicrovmError
from ..server.watcher import deadline_passed
from . import ca
from .engine import InterceptorAddon, LogBridge, host_matches

logger = logging.getLogger(__name__)

#: How long a shutting-down master gets to land its done() pass.
SHUTDOWN_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class PlaceholderEntry:
    """One active placeholder, as the addon's hot path sees it."""

    sentinel: str
    name: str
    dests: tuple[str, ...]
    backend_ref: str


@dataclass(frozen=True)
class Armed:
    """One armed workspace's listener identity."""

    workspace_id: str
    tap_ip: str
    port: int

    @property
    def spec(self) -> str:
        """The mitmproxy mode spec: transparent mode on this
        workspace's tap address."""
        return f"transparent@{self.tap_ip}:{self.port}"


def build_master(owner) -> Master:
    """The embedded master: no listener of its own — one mode spec
    per armed workspace joins ``options.mode`` at arm time.

    The addon registers before the default addons (registration
    order is hook dispatch order — the spike's round one), and the
    confdir points into the daemon state so mitmproxy never touches
    ``~/.mitmproxy``.
    """
    confdir = owner.confdir()
    confdir.mkdir(parents=True, exist_ok=True)
    master = Master(options.Options(mode=[], confdir=str(confdir)))
    master.addons.add(InterceptorAddon(owner), LogBridge(), *default_addons())
    # lazy: the splice tier must relay before any upstream dial;
    # keep_host_header: the Host the swap pinned is the Host the
    # origin sees — mitmproxy's own rewrite stays off.
    master.options.update(connection_strategy="lazy", keep_host_header=True)
    return master


def log_dead_master(task: asyncio.Task) -> None:
    """Name why a finished run task ended, when it can be named at
    all (a cancelled task carries no exception to read)."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("the interceptor's master run ended: %s", error)


class Interceptor:
    """Owns the embedded master and every workspace's armed state."""

    def __init__(self, app, *, master_factory=build_master) -> None:
        self.app = app
        self._master_factory = master_factory
        self._master: Master | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._armed: dict[str, Armed] = {}
        self._by_tap: dict[str, str] = {}
        self._entries: dict[str, dict[str, PlaceholderEntry]] = {}
        self._cas: dict[str, ca.WorkspaceCA] = {}

    # --- the addon's surface (sync: the hot path never awaits) ------

    def workspace_for_tap(self, tap_ip: str | None) -> str | None:
        """The workspace whose tap address this is, None when the
        listener is not one of ours."""
        return self._by_tap.get(tap_ip)

    def entries_for(self, workspace_id: str) -> dict[str, PlaceholderEntry]:
        """The workspace's live entries (sentinel-keyed)."""
        return self._entries.get(workspace_id, {})

    def matching_entry(
        self, workspace_id: str, host: str
    ) -> PlaceholderEntry | None:
        """Any entry of this workspace whose allowlist covers *host*
        (the splice tier's question)."""
        for entry in self.entries_for(workspace_id).values():
            if host_matches(host, entry.dests):
                return entry
        return None

    def ca_for(self, workspace_id: str) -> ca.WorkspaceCA | None:
        """The workspace's CA, or None when disarmed mid-handshake."""
        return self._cas.get(workspace_id)

    def mint_leaf(self, workspace_id: str, sni: str):
        """One connection's leaf from the workspace CA (SNI-keyed)."""
        return ca.mint_leaf(self._cas[workspace_id], sni)

    def confdir(self) -> Path:
        """mitmproxy's own scratch inside the daemon state."""
        return self.app.state.settings.vmm.state_dir / "interceptor"

    async def sentinel_live(self, sentinel: str) -> bool:
        """The per-request swap gate: the row exists and its expiry
        is in the future (#198's predicate)."""
        return await self.app.state.model.placeholder_valid(sentinel)

    async def secret_for(self, entry: PlaceholderEntry) -> str:
        """The real secret behind the entry (cached after first use)."""
        return await self.app.state.secrets.read(entry.backend_ref)

    async def publish_swap(
        self, workspace_id: str, entry: PlaceholderEntry, host: str
    ) -> None:
        """A swap reached the wire: one event per swapped request."""
        await self.publish("secret.swap", workspace_id, entry, host)

    async def publish_sighting(
        self, workspace_id: str, entry: PlaceholderEntry, host: str
    ) -> None:
        """An off-allowlist sighting: decrypted, unrewritten,
        announced — the blind spot's complement (#194)."""
        await self.publish("secret.sighting", workspace_id, entry, host)

    async def publish(
        self, event: str, workspace_id: str, entry: PlaceholderEntry, host: str
    ) -> None:
        await self.app.state.hub.publish(
            event,
            {
                "workspace_id": workspace_id,
                "name": entry.name,
                "host": host,
            },
        )

    # --- lifecycle ---------------------------------------------------

    def armed_port(self, workspace_id: str) -> int | None:
        """The armed listener's port for a workspace, None while
        disarmed (#280): a live table swap re-applies the chain
        with the redirect half exactly as it stands."""
        armed = self._armed.get(workspace_id)
        return None if armed is None else armed.port

    async def refresh(self, workspace_id: str) -> None:
        """Re-evaluate one workspace's armed state — after any
        placeholder change (mint, renew, revoke, expiry) or as the
        last step of an egress attach."""
        async with self._lock:
            self.retire_dead_master()
            attachment = self.app.state.net.attachment_for(workspace_id)
            if attachment is None:
                return  # not running: arming happens at attach
            entries = await self.active_entries(workspace_id)
            if not entries:
                if workspace_id in self._armed:
                    await self.disarm(workspace_id)
                return
            if workspace_id in self._armed:
                self._entries[workspace_id] = entries
                return
            await self.arm(workspace_id, attachment, entries)

    def retire_dead_master(self) -> None:
        """Drop a master whose run task ended: it serves nothing,
        and armed bookkeeping over a dead listener would let every
        later refresh "succeed" while redirected flows hit a closed
        port. Clearing the books makes the next arm rebuild the
        master and its listeners whole (#260 review, round 5)."""
        if self._master is None or self._task is None:
            return
        if not self._task.done():
            return
        log_dead_master(self._task)
        self._armed.clear()
        self._by_tap.clear()
        self._entries.clear()
        self._cas.clear()
        self._master = None
        self._task = None

    async def active_entries(
        self, workspace_id: str
    ) -> dict[str, PlaceholderEntry]:
        """The workspace's live entries: minted, unrevoked (the row
        exists), unexpired (the deadline is checked here — the
        watcher's sweep is only the cleanup half)."""
        rows = await self.app.state.model.workspace_placeholders(workspace_id)
        now = datetime.now(UTC)
        return {
            row["sentinel"]: PlaceholderEntry(
                sentinel=row["sentinel"],
                name=row["name"],
                dests=tuple(row["dests"]),
                backend_ref=row["backend_ref"],
            )
            for row in rows
            if not deadline_passed(row["expires_at"], now)
        }

    async def arm(self, workspace_id, attachment, entries) -> None:
        """Add this workspace's listener and swap its table in. A
        failure after registration rolls the registration back, so
        the armed set never claims a listener that did not come up."""
        port = self.app.state.settings.net.interceptor_port
        armed = Armed(workspace_id, attachment.tap_ip, port)
        await self.ensure_master()
        vm_dir = self.app.state.settings.vmm.state_dir / "vms" / workspace_id
        authority = await asyncio.to_thread(
            ca.load_or_mint, vm_dir, workspace_id
        )
        # Register before the listener exists: a connection can
        # arrive the moment the port binds.
        self._armed[workspace_id] = armed
        self._by_tap[armed.tap_ip] = workspace_id
        self._entries[workspace_id] = entries
        self._cas[workspace_id] = authority
        try:
            await self.apply_modes()
            await self.app.state.net.apply_interception(workspace_id, port)
        except BaseException:
            with contextlib.suppress(Exception):
                await self.disarm(workspace_id)
            raise

    async def ensure_master(self) -> None:
        """Start the embedded master once, lazily.

        ``Master.run`` sets two process-wide loop attributes from
        here until shutdown: an eager task factory and mitmproxy's
        own exception handler for otherwise-unhandled loop errors
        (they log through the LogBridge instead of the default
        lastResort stderr). The daemon's other tasks keep running
        under both — the first arm is the moment the loop's
        semantics change, which is why the master starts once and
        lives for the daemon's lifetime, never per workspace."""
        if self._master is not None:
            return
        self._master = self._master_factory(self)
        self._task = asyncio.create_task(self._master.run())

    async def apply_modes(self) -> None:
        """Bind the armed set: one transparent listener per tap. A
        failed bring-up is a named refusal — mitmproxy logs the bind
        error and answers False rather than raising."""
        assert self._master is not None
        specs = sorted(armed.spec for armed in self._armed.values())
        self._master.options.update(mode=specs)
        server = self._master.addons.get("proxyserver")
        if server is None or not await server.setup_servers():
            raise MicrovmError("the interceptor's listener failed to start")

    async def disarm(self, workspace_id: str, swap_table: bool = True) -> None:
        """Drop this workspace's listener and (unless the table dies
        with the attachment anyway) swap its table back."""
        armed = self._armed.pop(workspace_id, None)
        self._entries.pop(workspace_id, None)
        self._cas.pop(workspace_id, None)
        if armed is not None:
            self._by_tap.pop(armed.tap_ip, None)
        if self._master is not None:
            await self.apply_modes()
        if swap_table and armed is not None:
            await self.app.state.net.apply_interception(workspace_id, None)

    async def on_detach(self, workspace_id: str) -> None:
        """The attachment is going away: drop the listener and the
        maps; the per-VM table dies with the attachment, so no table
        swap runs here."""
        async with self._lock:
            await self.disarm(workspace_id, swap_table=False)

    async def stop(self) -> None:
        """Shut the embedded master down (daemon shutdown;
        idempotent)."""
        async with self._lock:
            for workspace_id in list(self._armed):
                await self.disarm(workspace_id, swap_table=False)
            master, task = self._master, self._task
            self._master = self._task = None
        if master is None:
            return
        master.shutdown()
        try:
            await asyncio.wait_for(task, SHUTDOWN_TIMEOUT_S)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except Exception:  # noqa: BLE001 - a dead master must not
            # take daemon shutdown down with it: the run task's own
            # error is logged here, and the close-out below continues.
            logger.exception("the interceptor's master run ended in error")
