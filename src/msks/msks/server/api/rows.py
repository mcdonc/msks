"""Workspace-row helpers shared by the resource routers: the
seam's VmSpec rebuilt from a row, the placement and lifecycle
guards (with the row re-read under the move-lock), the
create-serialization lock, and the 404 the ref-resolving routes
answer."""

import asyncio
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from ...spec.vm import VmSpec, VmStatus


async def healed_spec(app, row: dict) -> VmSpec:
    """The launch spec with the row's LLM credential restored (#259
    review): ``workspace_dict`` omits it (operator views never show
    it), so ``spec_for`` alone would rebuild a crash-healed seed
    without the token the row still authenticates — the boot path
    is the one consumer that needs the secret half."""
    spec = spec_for(row)
    if spec.llm_token is not None:
        return spec
    token = await app.state.model.get_llm_token(row["id"])
    if token is None or token["llm_token"] is None:
        return spec
    return replace(spec, llm_token=token["llm_token"])


def spec_for(row: dict) -> VmSpec:
    """Rebuild the seam's VmSpec from a workspace row."""
    initrd = None if row["initrd"] is None else Path(row["initrd"])
    return VmSpec(
        workspace_id=row["id"],
        # The creation name rides the spec for one consumer — the
        # seed's local-hostname (#370). The minted id keeps owning
        # every path and row key.
        name=row.get("name"),
        kernel=Path(row["kernel"]),
        rootfs=Path(row["rootfs"]),
        cmdline=row["cmdline"],
        cpus=row["cpus"],
        mem_mib=row["mem_mib"],
        initrd=initrd,
        root_mib=row["root_mib"],
        home_mib=row["home_mib"],
        egress=bool(row.get("egress", False)),
        egress_mode=row.get("egress_mode") or "allow",
        secret_coverage=row.get("secret_coverage") or "all",
        egress_allowlist=tuple(row.get("egress_allowlist") or ()),
        user_data=row.get("user_data"),
        ssh_pubkey=row.get("ssh_pubkey"),
        login_user=row.get("login_user"),
        llm_token=row.get("llm_token"),
    )


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

    The minted identity makes every create racer-specific — two
    concurrent creates of one name would each mint their own key,
    and the artifact installs are last-rename-wins, so the loser's
    seed could outlive its 409 under the winner's row: a workspace
    whose key never logs in. One lock per workspace name, held
    from the exists-check through the row insert, keeps the pair
    (row, seed) from one mint; the loser sees the winner's row and
    answers 409.
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
