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
    model = app.state.model
    microvm = app.state.microvm
    published = 0
    for row in await model.list_workspaces():
        info = await microvm.info(row["id"])
        status = SEAM_TO_MODEL_STATUS[info.status]
        if status != row["status"] and await model.set_status(row["id"], status):
            await hub.publish(
                "workspace.status",
                {"id": row["id"], "status": status},
            )
            published += 1
    return published


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
            LOG.exception("workspace status scan failed; retrying next interval")
        await asyncio.sleep(interval)
