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
    if usage["free"] < floor_mib * MIB:
        return "critical"
    if usage["used"] * 100 >= usage["total"] * warn_pct:
        return "warn"
    return "ok"


def floor_refusal(vmm, action: str, incoming_b: int = 0) -> str | None:
    """The named refusal when a write cannot fit: the floor plus
    the write's own incoming bytes.

    ``incoming_b`` sizes the write being refused: an image import
    carries the archive twice over (the retained copy plus its
    unpacked cache), a home-volume import carries the upload's
    length when the client sent one, and a create carries nothing
    (its blank artifacts are sparse — the floor alone is the guard).
    None means proceed. The escape hatches are honest ones: reclaim
    space, or **lower** the floor — raising it only refuses more.
    """
    usage = state_usage(vmm.state_dir)
    if usage is None:
        return None
    need_b = vmm.storage_floor_mib * MIB + incoming_b
    if usage["free"] >= need_b:
        return None
    free_mib = max(usage["free"] // MIB, 0)
    need_mib = need_b // MIB
    why = (
        f"the MSKSD_STORAGE_FLOOR_MIB floor ({vmm.storage_floor_mib} MiB)"
        if incoming_b == 0
        else (
            f"the MSKSD_STORAGE_FLOOR_MIB floor ({vmm.storage_floor_mib} "
            f"MiB) plus {incoming_b // MIB} MiB of incoming bytes"
        )
    )
    return (
        f"the state disk has {free_mib} MiB free, below the {need_mib} "
        f"MiB this write needs ({why}); reclaim space (msks storage "
        "names the consumers; msks rm and msks image rm remove them) "
        f"or lower the floor if you accept less headroom before {action}"
    )


def create_refusal(
    vmm, action: str = "creating workspaces", incoming_b: int = 0
) -> str | None:
    """The floor refusal for writes whose artifacts are local-backend
    facts (#184).

    A workspace's overlay and home volume live on per-workspace
    claims under the k8s backend — the cluster places and sizes
    them — so their writes never consult this daemon's state disk.
    The image catalog is the exception: it lives on this daemon's
    state disk on every backend, and its import route checks
    :func:`floor_refusal` directly.
    """
    if vmm.driver != "local":
        return None
    return floor_refusal(vmm, action, incoming_b)


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
