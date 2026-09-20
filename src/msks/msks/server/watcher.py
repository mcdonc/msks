"""The status watcher: polls the seam and publishes transitions (#8).

Every ``event_poll_s`` the watcher compares each workspace row's last
recorded status against the live driver view; a difference is written
back and published on the event hub. Polling (not callbacks) keeps the
watcher honest across driver restarts and both backends.
"""

import asyncio
import logging

from .. import storage
from ..microvm.spec import VmStatus
from .events import EventHub

LOG = logging.getLogger(__name__)

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
    pressure would freeze silently until daemon restart.
    """
    interval = app.state.settings.server.event_poll_s
    while True:
        try:
            await scan_once(app, hub)
            await scan_storage(app, hub)
        except Exception:
            LOG.exception(
                "workspace status scan failed; retrying next interval"
            )
        await asyncio.sleep(interval)
