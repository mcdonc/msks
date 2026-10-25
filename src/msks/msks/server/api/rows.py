"""Workspace-row helpers shared by the resource routers: the
seam's VmSpec rebuilt from a row, the placement and lifecycle
guards (with the row re-read under the move-lock), the
create-serialization lock, and the 404 the ref-resolving routes
answer."""

import asyncio
import os
from pathlib import Path

from fastapi import HTTPException

from ...spec.mounts import (
    MODE_RW,
    MountIssue,
    mount_issues,
    parse_mounts,
)
from ...spec.vm import VmSpec, VmStatus


def spec_for(row: dict, mounts=None) -> VmSpec:
    """Rebuild the seam's VmSpec from a workspace row.

    *mounts* overrides the row's parsed list (#620, part #622):
    the start path derives the attachable subset (#621) and hands
    the seam exactly that — a skipped mount (share gone, mode
    denied, conflict, drift) never reaches a device."""
    return VmSpec(
        workspace_id=row["id"],
        # The creation name rides the spec for one consumer — the
        # seed's local-hostname (#370). The minted id keeps owning
        # every path and row key.
        name=row.get("name"),
        kernel=Path(row["kernel"]),
        rootfs=Path(row["rootfs"]),
        initrd=row_initrd(row),
        cmdline=row["cmdline"],
        cpus=row["cpus"],
        mem_mib=row["mem_mib"],
        root_mib=row["root_mib"],
        home_mib=row["home_mib"],
        egress=bool(row.get("egress", False)),
        egress_mode=row.get("egress_mode") or "allow",
        secret_coverage=row.get("secret_coverage") or "all",
        egress_allowlist=tuple(row.get("egress_allowlist") or ()),
        user_data=row.get("user_data"),
        ssh_pubkey=row.get("ssh_pubkey"),
        login_user=row.get("login_user"),
        mounts=row_mounts(row, mounts),
    )


def row_initrd(row: dict) -> Path | None:
    """The row's initrd as a path, None kept as None — the spec's
    optional boot half."""
    return None if row["initrd"] is None else Path(row["initrd"])


def row_mounts(row: dict, mounts) -> tuple:
    """The spec's mounts: the caller's derived subset when it
    passed one, the row's parsed list otherwise."""
    if mounts is None:
        return parse_mounts(row.get("mounts"))
    return mounts


def owner_host(app) -> str | None:
    """The host recorded as owning a new workspace's artifacts.

    Placement is a fact of the local backend: the artifacts are
    files on one host, and that host's name is recorded at create
    (#14) so only it may boot the workspace.
    """
    return app.state.settings.vmm.host_name


def host_mismatch(app, row: dict) -> str | None:
    """The named error when the artifacts live on another host.

    Placement is recorded at create (#14): a workspace's overlay and
    home volume live on one host, and only that host may boot it. A
    row without a host predates #14 — the artifacts are wherever
    this daemon finds them, so this host adopts the start.
    """
    recorded = row.get("host")
    local = app.state.settings.vmm.host_name
    if recorded is None or recorded == local:
        return None
    return (
        f"home volume for workspace {row['id']} lives on host {recorded}; "
        f"this host is {local}"
    )


async def serialize_create(app, key: str):
    """Serialize same-name creates end to end (#111, #246).

    The artifact installs are last-rename-wins, so two concurrent
    creates of one name would race their seeds against each other's
    409s: the loser's seed could outlive its refusal under the
    winner's row — a seed the row does not name. One lock per
    workspace name, held from the exists-check through the row
    insert, keeps the pair (row, seed) from one create; the loser
    sees the winner's row and answers 409.
    """
    lock = app.state.create_locks.setdefault(key, asyncio.Lock())
    return lock


async def workspace_or_404(app, workspace_id: str) -> dict:
    """The workspace a route's ref names — its immutable id or its
    unique name (#246) — or the 404."""
    row = await app.state.model.get_workspace(workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    return row


#: The lifecycle statuses a home-volume move serves (#80): an
#: allow-list, so ``unknown`` — a possibly-live VM the watcher
#: could not probe — and any future status refuse until named here.
#: ``starting``/``running``/``paused`` all keep the volume: it is a
#: live block device in each of them.
HOME_FREE_STATUSES = ("created", "stopped", "absent")

#: The lifecycle statuses a workspace holds its rw mounts in
#: (#620): the VM is live in each, so its exclusive shares are
#: spoken for — and ``unknown``, a possibly-live VM the watcher
#: could not probe, holds too: the conservative direction for
#: mounts is refusing a second rw attacher, mirroring the home
#: volume guard's refusal of ``unknown`` frees.
MOUNT_HOLDING_STATUSES = (
    "starting",
    "running",
    "paused",
    "unknown",
)


def starting_workspaces(app) -> set[str]:
    """The ids with a start in flight in this process — the
    process-local half of the ``starting`` hold (#620, part
    #621). Built at API assembly; an app without the attribute
    gets one installed, so the caller's add/discard always lands
    on the stored set."""
    existing = getattr(app.state, "starting_workspaces", None)
    if existing is None:
        existing = set()
        setattr(app.state, "starting_workspaces", existing)
    return existing


async def mount_state(app, row: dict) -> tuple[tuple, tuple]:
    """A row's mounts split into the attachable and the skipped
    (#620, part #621).

    The derivation reads live state every time it runs: the
    configured shares (a SIGHUP reload re-shares without a
    restart) and the other rows' live rw attachments. A mount
    skips when its share left the config (``skipped``), when a
    reload narrowed its share to read-only under an rw mount
    (``denied``), when it wants ``rw`` on a ``multi: false`` share
    another workspace already holds (``conflict``), or when the
    share's path drifted since load (``drift``, #620 part #622:
    the realpath at settings load pinned the baseline, and a
    symlink retargeted inside a shareable tree must not walk the
    attach out of what the operator configured). The row itself
    never conflicts with its own attach. Read-only mounts never
    conflict: any number sit beside one rw holder."""
    mounts = parse_mounts(row.get("mounts"))
    shares = {share.name: share for share in app.state.settings.mounts.shares}
    issues = mount_issues(mounts, shares, await rw_holders(app, row["id"]))
    issues = (*issues, *drift_issues(mounts, shares))
    skipped = {issue.mount for issue in issues}
    return (
        tuple(mount for mount in mounts if mount not in skipped),
        issues,
    )


def drift_issues(mounts, shares) -> tuple:
    """The drift refusals (#620, part #622) for the mounts whose
    shares still exist: path vanished since load, or resolves
    elsewhere than the load-time pin."""
    return tuple(
        issue
        for issue in (
            drift_issue(mount, shares[mount.share])
            for mount in mounts
            if mount.share in shares
        )
        if issue is not None
    )


def drift_issue(mount, share) -> object | None:
    """The drift refusal (#620, part #622): the share's path must
    resolve where the settings load left it. A path that did not
    exist at load (the doctor warns) attaches at its current
    resolution when it appears — no baseline existed to drift
    from. A vanished path is a skip with the named reason, the
    same event kind a config deletion carries."""
    if not os.path.exists(share.path):
        return MountIssue(
            kind="skipped",
            mount=mount,
            detail=(
                f"share {mount.share!r} path does not exist: {share.path}"
            ),
        )
    if share.resolved is None:
        return None
    now = os.path.realpath(share.path)
    if now != share.resolved:
        return MountIssue(
            kind="drift",
            mount=mount,
            detail=(
                f"share {mount.share!r} path drifted since load: "
                f"{share.resolved} became {now}; widen the config's "
                "path or reload it"
            ),
        )
    return None


async def rw_holders(app, workspace_id: str) -> dict[str, str]:
    """The live rw attachments by share: the holder's workspace
    id, this row excluded — a start never conflicts with the
    mounts the same workspace already carries."""
    holders: dict[str, str] = {}
    for other in await app.state.model.list_workspaces():
        if other_row_holds(other, workspace_id):
            record_rw_holders(holders, other)
    return holders


def other_row_holds(row: dict, workspace_id: str) -> bool:
    """Whether another row's VM is live: the holding statuses are
    the live ones, and the row being started (or read) never
    counts against itself."""
    return (
        row["id"] != workspace_id
        and row.get("status") in MOUNT_HOLDING_STATUSES
    )


def record_rw_holders(holders: dict[str, str], row: dict) -> None:
    """Add one live row's rw mounts to the holder map — the first
    holder a share sees is the one a conflict names."""
    for mount in parse_mounts(row.get("mounts")):
        if mount.mode == MODE_RW:
            holders.setdefault(mount.share, row["id"])


def home_volume_guard(app, row: dict) -> tuple[int, str] | None:
    """(status, refusal) when this daemon cannot move the volume.

    Placement (the artifacts live on one host) and a possibly-live
    attachment are the two facts that block a move: the free
    statuses are named, everything else refuses.
    """
    mismatch = host_mismatch(app, row)
    if mismatch is not None:
        return 409, mismatch
    if row["status"] not in HOME_FREE_STATUSES:
        return 409, (
            f"workspace {row['id']} is {row['status']}; "
            f"stop it before moving its home volume"
        )
    return None


async def rechecked_row(app, workspace_id: str) -> dict:
    """The row re-read under the move-lock, with both guards applied.

    The row's status is the cheap guard; the live seam is the
    truth-guard: a watcher scan that probed a launch's spawn window
    can leave a live VM's row at ``stopped`` for one poll interval,
    and the seam's answer refuses where the row lies (#80 review).
    """
    row = await app.state.model.get_workspace(workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    guard = home_volume_guard(app, row)
    if guard is not None:
        raise HTTPException(*guard)
    info = await app.state.microvm.info(workspace_id)
    if info.status not in (VmStatus.ABSENT, VmStatus.STOPPED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {workspace_id}'s VMM reports {info.status.value}; "
                f"stop it before moving its home volume"
            ),
        )
    return row
