"""The status watcher: polls the seam and publishes transitions (#8).

Every ``event_poll_s`` the watcher compares each workspace row's last
recorded status against the live driver view; a difference is written
back and published on the event hub. Polling (not callbacks) keeps the
watcher honest across driver restarts and both backends.
"""

import asyncio
import logging

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


async def watch_loop(app, hub: EventHub) -> None:
    """The background task: scan, sleep, repeat — surviving seam errors.

    One raising ``info()`` (a restarted VMM, a stale socket) must not
    end the loop: statuses would freeze silently until daemon restart.
    """
    interval = app.state.settings.server.event_poll_s
    while True:
        try:
            await scan_once(app, hub)
        except Exception:
            LOG.exception(
                "workspace status scan failed; retrying next interval"
            )
        await asyncio.sleep(interval)
