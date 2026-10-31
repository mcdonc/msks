"""Workspace-row helpers shared by the resource routers: the
seam's VmSpec rebuilt from a row, the placement and lifecycle
guards (with the row re-read under the move-lock), the
create-serialization lock, and the 404 the ref-resolving routes
answer."""

import asyncio
from pathlib import Path

from fastapi import HTTPException

from ...hostshares import check_host_share
from ...spec.mounts import MountIssue, parse_mounts, validate_mounts
from ...spec.vm import VmSpec, VmStatus


def spec_for(row: dict, mounts: tuple | None = None) -> VmSpec:
    """Rebuild the seam's VmSpec from a workspace row.

    *mounts* overrides the row's parsed list (#620, part #622):
    the start path derives the attachable subset (#621) and hands
    the seam exactly that — a skipped mount (a gate refusal)
    never reaches a device."""
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
        timezone=row.get("timezone"),
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
    (#620, part #621; #672).

    The derivation reads live state every time it runs: each
    mount re-runs the #667 gates against the daemon's current
    host mode, allowed roots, and the daemon VM's own mount
    state, and a refusal (a root the VM no longer mounts, a root
    the policy narrowed away, a symlink that moved) becomes a
    ``skipped`` mount with the gate's message. The start
    proceeds without the mount; the skip is state, derived again
    on every read."""
    mounts = parse_mounts(row.get("mounts"))
    issues = host_mount_issues(app, mounts)
    skipped = {issue.mount for issue in issues}
    return (
        tuple(mount for mount in mounts if mount not in skipped),
        issues,
    )


def host_mount_issues(app, mounts: tuple) -> tuple:
    """The mounts a reload or a VM change took away (#672): each
    re-runs the #667 gates against the live policy and mount
    state, and a refusal becomes a ``skipped`` mount with the
    gate's message. The start proceeds without the mount."""
    issues = []
    for mount in mounts:
        try:
            check_host_share(
                app.state.settings.mounts.host_mode,
                app.state.settings.mounts.host_roots,
                mount.host,
            )
        except ValueError as exc:
            issues.append(
                MountIssue(kind="skipped", mount=mount, detail=str(exc))
            )
    return tuple(issues)


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


def checked_mounts(app, raw_mounts: list) -> tuple:
    """The mounts a set request lands on (#620, part #624): the
    parsed, row-validated list, refused with a named ValueError
    when the shape or the rules fail — and when the daemon's
    gates bound it: every host path runs the #667 gates, and the
    translated path must sit under a root the daemon's VM mounts
    and an allowed root, resolved against symlink escapes — each
    refusal names its gate and remedy at set time, so nothing
    lands on the row to fail at boot. The gates read the config
    as loaded (a SIGHUP reload re-opens the surface without a
    restart); a root the operator removes later reads as a skip
    at start, not a set-time fact — the set speaks against the
    gates it can see."""
    try:
        # The wire's absent fields arrive as None (the schema's
        # optional mode); the parser takes absent-or-valued keys.
        mounts = parse_mounts(
            [
                {
                    key: value
                    for key, value in entry.items()
                    if value is not None
                }
                for entry in raw_mounts
            ],
        )
        validate_mounts(mounts)
    except ValueError as exc:
        raise exc from None
    check_host_mounts(app, mounts)
    return mounts


def check_host_mounts(app, mounts: tuple) -> None:
    """Every mount through the #667 gates (#672): the set-time
    refusal taxonomy (not mounted in the VM / outside the allowed
    roots / symlink escape / missing) is the whole point of
    set-time validation — a share that cannot serve never lands
    on the row."""
    for mount in mounts:
        check_host_share(
            app.state.settings.mounts.host_mode,
            app.state.settings.mounts.host_roots,
            mount.host,
        )


def issue_payloads(issues: tuple[MountIssue, ...]) -> list[dict]:
    """The derived per-mount state the surfaces read (#620, part
    #624): one frame per skipped mount — ``kind``, the mount, and
    the named reason. An empty list is the healthy row."""
    return [
        {
            "kind": issue.kind,
            "host": issue.mount.host,
            "guest": issue.mount.guest,
            "mode": issue.mount.mode,
            "detail": issue.detail,
        }
        for issue in issues
    ]


async def row_mount_issues(app, row: dict) -> tuple:
    """One row's derived mount issues — the split's skipped half
    (#620, part #621) as the surfaces read it."""
    _attachable, issues = await mount_state(app, row)
    return issues


async def list_mount_issues(app, rows: list[dict]) -> dict[str, tuple]:
    """Every row's derived mount issues, keyed by id (#620, part
    #624): the gates read per-row state only, so the listing
    matches the row read."""
    return {
        row["id"]: host_mount_issues(app, parse_mounts(row.get("mounts")))
        for row in rows
    }
