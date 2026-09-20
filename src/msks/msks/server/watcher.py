"""The status watcher: polls the seam and publishes transitions (#8).

Every ``event_poll_s`` the watcher compares each workspace row's last
recorded status against the live driver view; a difference is written
back and published on the event hub. Polling (not callbacks) keeps the
watcher honest across driver restarts and both backends.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime

from .. import storage
from ..microvm.spec import VmStatus
from .events import EventHub

LOG = logging.getLogger(__name__)

#: The consent retention sweep (#69): hour-scale housekeeping, so
#: an hour between sweeps is plenty. The deadline is wall-clock,
#: tracked next to the poll loop instead of inside it.
PRUNE_INTERVAL_S = 3600.0

SEAM_TO_MODEL_STATUS = {
    VmStatus.ABSENT: "absent",
    VmStatus.STARTING: "starting",
    VmStatus.RUNNING: "running",
    VmStatus.PAUSED: "paused",
    VmStatus.STOPPED: "stopped",
    VmStatus.UNKNOWN: "unknown",
}


async def scan_once(app, hub: EventHub) -> int:
    """One reconcile-and-publish pass; returns transitions published."""
    published = 0
    for row in await app.state.model.list_workspaces():
        if await scan_workspace(app, hub, row):
            published += 1
    return published


async def scan_workspace(app, hub: EventHub, row: dict) -> bool:
    """Reconcile one workspace; a failing one starves no other.

    A freshly created row (never started) reports ``created`` while
    the seam says ``absent`` — that is the steady state until the
    first start, not a transition: neither written nor published.
    """
    try:
        info = await app.state.microvm.info(row["id"])
    except Exception:
        LOG.exception("status probe failed for %s; continuing", row["id"])
        return False
    status = SEAM_TO_MODEL_STATUS[info.status]
    if status == row["status"]:
        return False
    if status == "absent" and row["status"] == "created":
        return False
    return await publish_transition(app, hub, row["id"], status)


async def publish_transition(
    app, hub: EventHub, workspace_id: str, status: str
) -> bool:
    """Write the new status and announce it; False when the row vanished."""
    if not await app.state.model.set_status(workspace_id, status):
        return False
    await hub.publish(
        "workspace.status", {"id": workspace_id, "status": status}
    )
    return True


async def publish_pressure(
    hub: EventHub, previous: str | None, pressure: str, usage: dict | None
) -> bool:
    """Announce one pressure transition; False when this is the
    silent first sight of an unprobeable disk.

    A daemon whose state dir does not exist yet starts at ``unknown``
    without an event — there is no condition to announce — while a
    later fall from a known pressure to ``unknown`` (the state dir
    vanished underneath the daemon) announces like any condition.
    """
    if previous is None and pressure == "unknown":
        return False
    await hub.publish(
        "storage.pressure",
        {
            "pressure": pressure,
            **(usage or {"total": 0, "used": 0, "free": 0}),
        },
    )
    return True


def pressure_warning(pressure: str, usage: dict | None, vmm) -> str | None:
    """The named log line for a disk past its thresholds, or None."""
    if pressure not in ("warn", "critical"):
        return None
    free_mib = max((usage or {}).get("free", 0) // storage.MIB, 0)
    return (
        f"state disk pressure is {pressure}: {free_mib} MiB free "
        f"(warn past {vmm.storage_warn_pct}% used, floor "
        f"{vmm.storage_floor_mib} MiB); msks storage names the consumers"
    )


async def scan_storage(app, hub: EventHub) -> bool:
    """Probe the state disk once; publish on a pressure change (#184).

    Edge-triggered, like every watcher publish: a steady ``warn`` logs
    and announces once, not every poll. Recovery to ``ok`` announces
    too (an operator watching the event stream sees the all-clear).
    The probe serves the local backend only: on k8s the artifacts
    live on per-workspace claims the cluster places, and this
    daemon's filesystem says nothing about them.
    """
    vmm = app.state.settings.vmm
    if vmm.driver != "local":
        return False
    usage = await asyncio.to_thread(storage.state_usage, vmm.state_dir)
    pressure = storage.pressure_for(
        usage, vmm.storage_warn_pct, vmm.storage_floor_mib
    )
    previous = getattr(app.state, "storage_pressure", None)
    if pressure == previous:
        return False
    app.state.storage_pressure = pressure
    announced = await publish_pressure(hub, previous, pressure, usage)
    if notice := pressure_warning(pressure, usage, vmm):
        LOG.warning("%s", notice)
    return announced


async def watch_loop(app, hub: EventHub) -> None:
    """The background task: scan, sleep, repeat — surviving seam
    errors.

    One raising probe (a restarted VMM, a stale socket, an
    unstatvfs-able state dir) must not end the loop: statuses and
    pressure would freeze silently until daemon restart. The
    consent retention sweep rides the same loop on its own
    wall-clock deadline (#69).
    """
    interval = app.state.settings.server.event_poll_s
    next_prune = time.monotonic() + PRUNE_INTERVAL_S
    while True:
        try:
            await scan_once(app, hub)
            await scan_storage(app, hub)
            if time.monotonic() >= next_prune:
                next_prune = time.monotonic() + PRUNE_INTERVAL_S
                await sweep_consent(app)
            await sweep_expired_placeholders(app, hub)
        except Exception:
            LOG.exception("watcher scan failed; retrying next interval")
        await asyncio.sleep(interval)


async def sweep_consent(app) -> None:
    """Prune the consent table past retention/cap (#69); a failure
    defers to the next sweep (housekeeping, not correctness)."""
    deleted = await app.state.model.egress_consent.prune()
    if deleted:
        LOG.info("consent: pruned %d row(s) past retention/cap", deleted)


async def sweep_expired_placeholders(app, hub: EventHub) -> int:
    """Retire placeholders past their deadline (#198): audit, remove,
    re-sync the store manifest, announce.

    Expiry rides the same per-request predicate as revocation, so a
    row past its deadline stops swapping immediately — this sweep is
    the cleanup half: it records the expiry event (destinations and
    timestamp), drops the row, deletes the backend value, and strips
    its declaration from the manifest. A failing store delete never
    keeps the row: the value left behind is inert without it.
    """
    model = app.state.model
    now = datetime.now(UTC)
    rows = [
        row
        for row in await model.list_placeholders()
        if deadline_passed(row["expires_at"], now)
    ]
    for row in rows:
        await retire_expired(app, hub, row)
    if rows:
        refs = await model.placeholder_refs()
        await asyncio.to_thread(app.state.secrets.sync_manifest, refs)
    return len(rows)


def deadline_passed(expires_at: str | None, now: datetime) -> bool:
    """Whether a placeholder's deadline is past. The stored deadline
    is naive UTC on the sqlite round-trip (the dialect strips
    tzinfo at bind); replace() unconditionally normalizes."""
    if expires_at is None:
        return False
    deadline = datetime.fromisoformat(expires_at).replace(tzinfo=UTC)
    return deadline <= now


async def retire_expired(app, hub: EventHub, row: dict) -> None:
    """One expired placeholder: audit, drop, clean the store,
    announce."""
    model = app.state.model
    await model.record_audit("expiry", row)
    await model.delete_placeholder(row["id"])
    try:
        await app.state.secrets.delete(row["backend_ref"])
    except Exception:  # noqa: BLE001 - inert leftover, logged below
        LOG.warning(
            "expired placeholder %s/%s: store value left behind at %s",
            row["workspace_id"],
            row["name"],
            row["backend_ref"],
        )
    await hub.publish(
        "secret.expiry",
        {"workspace_id": row["workspace_id"], "name": row["name"]},
    )
