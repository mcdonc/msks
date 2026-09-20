"""State-disk capacity reporting: the numbers an operator can trust (#184).

Three questions, three answers, each labeled so no number pretends to
be another:

- **The budget** — ``statvfs`` on the state dir (total/used/free
  bytes). The state disk is where every overlay, volume, and image
  lives; when it fills, workspace VM writes fail and guests see I/O
  errors (the #180 failure mode). This number predicts that storm.
- **Per-workspace cost** — the disk blocks the workspace's overlay
  and home-volume files occupy (sparse-aware block counts, never
  apparent sizes). Cost is what a workspace *charges* the budget: a
  10G-ceiling overlay with 3G of guest writes costs ~3G plus qcow2
  bookkeeping, so cost can slightly exceed the guest's own ``df``.
- **Image cost** — the blocks under one catalog entry's cache
  directory.

The fullness a guest *user* feels is their own ``df`` inside the
workspace; the daemon never guesses at it from outside a running VM
(the ext4 journal lags the host's view of it).

Pressure turns the budget into a named condition: ``ok``;
``warn`` past ``MSKSD_STORAGE_WARN_PCT`` used; ``critical`` at or
below ``MSKSD_STORAGE_FLOOR_MIB`` free, the point where workspace
creates are refused with a 507 — a named refusal instead of a
wedged ``dpkg``.
"""

import os
from pathlib import Path

MIB = 1024 * 1024

#: The disk-block byte size ``stat``'s ``st_blocks`` counts in.
ST_BLOCK_B = 512


def state_usage(state_dir: Path) -> dict | None:
    """The state dir's ``{total, used, free}`` bytes, or None.

    ``free`` is ``f_bavail`` — the bytes an unprivileged msksd can
    actually allocate — while ``used`` counts everything the
    filesystem holds. An unprobeable path (missing dir, permission)
    answers None and the caller reports ``unknown`` pressure rather
    than inventing a number.
    """
    try:
        probed = os.statvfs(state_dir)
    except OSError:
        return None
    total = probed.f_blocks * probed.f_frsize
    return {
        "total": total,
        "used": (probed.f_blocks - probed.f_bfree) * probed.f_frsize,
        "free": probed.f_bavail * probed.f_frsize,
    }


def pressure_for(usage: dict | None, warn_pct: int, floor_mib: int) -> str:
    """ "critical" / "warn" / "ok" — or "unknown" unprobeable.

    The floor outranks the percentage: a small disk can be past the
    warn line with plenty of runway left, and the refusal point is
    about absolute bytes, not fractions.
    """
    if usage is None or usage["total"] <= 0:
        return "unknown"
    if usage["free"] <= floor_mib * MIB:
        return "critical"
    if usage["used"] * 100 >= usage["total"] * warn_pct:
        return "warn"
    return "ok"


def create_refusal(vmm, action: str = "creating workspaces") -> str | None:
    """The named refusal when the state disk sits at critical.

    A create's blank artifacts and first boot write hundreds of MiB,
    and an import writes whole images — letting either through below
    the floor is how a state disk ends up full with a wedged guest
    on it. ``action`` names the refused write in the message. None
    means proceed. The k8s backend keeps its artifacts on
    per-workspace claims the cluster places, so this daemon-side
    floor never speaks for it.
    """
    if vmm.driver != "local":
        return None
    usage = state_usage(vmm.state_dir)
    if (
        pressure_for(usage, vmm.storage_warn_pct, vmm.storage_floor_mib)
        != "critical"
    ):
        return None
    free_mib = max(usage["free"] // MIB, 0)
    return (
        f"the state disk has {free_mib} MiB free, at or below the "
        f"MSKSD_STORAGE_FLOOR_MIB floor ({vmm.storage_floor_mib} MiB); "
        "reclaim space (msks storage names the consumers; msks rm and "
        "msks image rm remove them) or raise the floor before "
        f"{action}"
    )


def file_cost(path: Path) -> int:
    """The disk blocks one file occupies — sparse holes cost nothing.

    A missing file costs 0: a workspace whose artifacts were removed
    out-of-band reports that absence as an empty cost, matching the
    blank files a start would rebuild.
    """
    try:
        return path.lstat().st_blocks * ST_BLOCK_B
    except OSError:
        return 0


def tree_cost(root: Path) -> int:
    """The disk blocks under a directory (the image caches nest one
    entry per hash, each holding its kernel, initrd, rootfs, and
    manifest); a plain file costs its own blocks."""
    if not root.is_dir():
        return file_cost(root)
    return sum(file_cost(entry) for entry in root.rglob("*"))


def workspace_costs(state_dir: Path, workspace_id: str) -> dict:
    """One workspace's ``{root_bytes, home_bytes}`` cost.

    The root cost is the whole vm directory (the overlay plus the
    cidata seed beside it — the seed is a workspace artifact too);
    the home cost is the volume file.
    """
    return {
        "root_bytes": tree_cost(state_dir / "vms" / workspace_id),
        "home_bytes": file_cost(
            state_dir / "volumes" / f"{workspace_id}.ext4"
        ),
    }


def image_cost(state_dir: Path, image) -> int:
    """One catalog entry's cost: its cache directory **and** the
    retained archive beside it — the whole thing `msks image rm`
    removes, so the number the reclaim decision sees is the number
    the reclaim actually frees."""
    images = state_dir / "images"
    return tree_cost(images / image.hash) + file_cost(
        images / f"archive-{image.hash}.tar"
    )


def storage_report(
    state_dir: Path, warn_pct: int, floor_mib: int, rows: list, images
) -> dict:
    """The whole ``/api/v1/storage`` document.

    ``rows`` are the model's workspace rows (id, root_mib, home_mib)
    and ``images`` the catalog records; the costs are probed off the
    filesystem under them. All probes are plain stat calls, so the
    caller can run this off the event loop in one hop.
    """
    usage = state_usage(state_dir)
    return {
        "state": {
            **(usage or {"total": 0, "used": 0, "free": 0}),
            "pressure": pressure_for(usage, warn_pct, floor_mib),
            "floor_mib": floor_mib,
            "warn_pct": warn_pct,
        },
        "workspaces": [
            {
                "id": row["id"],
                "root_mib": row["root_mib"],
                "home_mib": row["home_mib"],
                **workspace_costs(state_dir, row["id"]),
            }
            for row in rows
        ],
        "images": [
            {
                "hash": image.hash,
                "name": image.name,
                "version": image.version,
                "bytes": image_cost(Path(state_dir), image),
            }
            for image in images
        ],
    }
