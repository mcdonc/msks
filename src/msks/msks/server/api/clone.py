"""The clone route (#773): one workspace copied into a new one.

A clone is a full fork of a stopped workspace: both disks copy
(the root overlay and the /home volume, by the same
reflink-or-windowed copy the storage layer owns), the create-time
config rides the new row verbatim (image binding, sizes, topology,
egress posture, mounts, login user, the user_data seed payload),
and everything per-instance mints fresh (the immutable id, the
egress address slice, the identity's provenance comment). Verdict
rows stay with the source: the clone's posture is the mode and
allowlist it inherited, and each ``allow forever`` is re-earned.
Workspace-scoped placeholder rows never cover the clone — the
secret perimeter stays a per-workspace decision.
"""

import asyncio
import contextlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from ...identity import normalize_public_key
from ...persist import (
    clone_file,
    home_volume_path,
    overlay_path,
    remove_home_volume,
    remove_overlay,
)
from ...spec.failures import MicrovmError
from ...spec.vm import VmStatus
from ...storage import create_refusal, file_cost
from .boot import mint_workspace_id
from .deps import require_token
from .rows import (
    HOME_FREE_STATUSES,
    host_mismatch,
    issue_payloads,
    owner_host,
    row_mount_issues,
    serialize_create,
    spec_for,
    workspace_or_404,
)
from .schemas import WorkspaceClone
from .volumes import move_lock


def clone_guard(app, row: dict) -> None:
    """Refuse a clone this daemon cannot read the source's disks
    for: the placement check and the free-status allow-list, with
    the clone's own wording (the home endpoints' twin)."""
    mismatch = host_mismatch(app, row)
    if mismatch is not None:
        raise HTTPException(status_code=409, detail=mismatch)
    if row["status"] not in HOME_FREE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {row['id']} is {row['status']}; "
                f"stop it before cloning it"
            ),
        )


def reannotated_pubkey(row: dict, name: str) -> str | None:
    """The source's public key line, its provenance comment
    rewritten for the clone. The key itself is the operator's
    (#486) and copies verbatim; only the ``msks-client:`` comment
    names the workspace, and the clone's names itself. A line the
    validator refuses (a row written before the annotation
    settled) copies as it stands."""
    line = row.get("ssh_pubkey")
    if line is None:
        return None
    try:
        algo, key_body = normalize_public_key(line)
    except ValueError:
        return line
    return f"{algo} {key_body} msks-client:{name}"


def router(app) -> APIRouter:
    """The clone route."""
    api = APIRouter()

    @api.post(
        "/api/v1/workspaces/{workspace_id}/clone",
        dependencies=[Depends(require_token)],
    )
    async def clone_workspace(
        workspace_id: str, body: WorkspaceClone
    ) -> Response:
        source = await workspace_or_404(app, workspace_id)
        source_id = source["id"]
        clone_guard(app, source)
        vmm = app.state.settings.vmm
        state_dir = vmm.state_dir
        workspace_id = await mint_workspace_id(app.state.model)
        async with await serialize_create(app, body.name):
            # The name re-check under the lock, the create route's
            # own race story: the loser answers the 409, never a
            # same-name insert.
            if await app.state.model.get_workspace(body.name) is not None:
                raise HTTPException(status_code=409, detail="workspace exists")
            # The source's move-lock for the whole copy: a boot or
            # a volume move of the source would mutate the disks
            # underneath the reader, so the pair orders against
            # every other exchange the source serves.
            async with move_lock(app, source_id):
                source = await app.state.model.get_workspace(source_id)
                if source is None:
                    raise HTTPException(
                        status_code=404, detail="no such workspace"
                    )
                clone_guard(app, source)
                # The seam's truth-guard, the home endpoints' twin
                # (#80): a launch's spawn window can leave a live
                # VM's row at ``stopped`` for one poll interval.
                info = await app.state.microvm.info(source_id)
                if info.status not in (VmStatus.ABSENT, VmStatus.STOPPED):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"workspace {source_id}'s VMM reports "
                            f"{info.status.value}; stop it before "
                            f"cloning it"
                        ),
                    )
                # The floor (#184), read under the lock so a
                # resize of the source (a lock holder) cannot move
                # the cost between the check and the copy: a clone
                # writes both artifacts onto the state disk at the
                # source's allocated cost — a reflink shares the
                # blocks, but the floor cannot promise one (the
                # filesystem may refuse it), so the worst case
                # sizes the check and a disk under the floor
                # refuses before any byte moves.
                refusal = create_refusal(
                    vmm,
                    "cloning a workspace",
                    file_cost(overlay_path(state_dir, source_id))
                    + file_cost(home_volume_path(state_dir, source_id)),
                    grow_hint=app.state.settings.mounts.disk_grow_hint,
                )
                if refusal is not None:
                    raise HTTPException(status_code=507, detail=refusal)
                # Both disks (#773): a full fork copies the root
                # overlay and the home volume. The seed is not a
                # disk to fork — its payload rides the row, and the
                # clone's first start builds its own seed from its
                # own spec. The copy runs on a worker thread: a
                # windowed copy of a volume-sized file holds the
                # loop for the whole copy otherwise, the stall the
                # home stream's to_thread reads exist to avoid. A
                # failure past the first installed artifact removes
                # exactly what this clone installed — pre-existing
                # debris at the target (a crashed predecessor's
                # leftovers) stays for the operator, the create
                # route's contract — and the rollback covers the
                # row build too: a spec or insert that fails after
                # the copy leaves no orphaned pair behind.
                copied: list[Path] = []
                try:
                    for src, dst in (
                        (
                            overlay_path(state_dir, source_id),
                            overlay_path(state_dir, workspace_id),
                        ),
                        (
                            home_volume_path(state_dir, source_id),
                            home_volume_path(state_dir, workspace_id),
                        ),
                    ):
                        worker = asyncio.create_task(
                            asyncio.to_thread(clone_file, src, dst)
                        )
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            # The request died mid-copy. Join the
                            # in-flight worker before unwinding: its
                            # install() rename can land after any
                            # cleanup that runs now, and the move-lock
                            # must not release while the thread still
                            # reads the source's disks. A copy that
                            # lands joins the rollback list; a copy
                            # that fails installed nothing.
                            with contextlib.suppress(BaseException):
                                await worker
                                copied.append(dst)
                            raise
                        copied.append(dst)
                except (MicrovmError, OSError) as exc:
                    for dst in copied:
                        dst.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=503,
                        detail=f"the clone's disk copy failed: {exc}",
                    ) from None
                except BaseException:
                    # A cancellation (a client gone, a shutdown) rolls
                    # back what this clone installed before the locks
                    # release — the same no-orphan rule the row build
                    # carries below.
                    for dst in copied:
                        dst.unlink(missing_ok=True)
                    raise
                # The allowlist stays with the source by default
                # (#773): a clone's posture is the mode it
                # inherited over an empty list, and everything the
                # source allowed is re-earned — the same fresh
                # start the verdict rows take. ``copy_allowlist``
                # opts the clone into the source's list verbatim.
                allowlist = (
                    tuple(source.get("egress_allowlist") or ())
                    if body.copy_allowlist
                    else ()
                )
                try:
                    spec = spec_for(
                        {
                            **source,
                            "id": workspace_id,
                            "name": body.name,
                            "ssh_pubkey": reannotated_pubkey(
                                source, body.name
                            ),
                            "egress_allowlist": allowlist,
                        }
                    )
                    row = await app.state.model.create_workspace(
                        spec,
                        image_hash=source.get("image_hash"),
                        host=owner_host(app),
                        name=body.name,
                    )
                except IntegrityError:
                    # The insert lost the name race: the artifacts
                    # sit under this clone's fresh id and are ours
                    # to remove — unless the collision was the id
                    # itself (two clones minted one id before either
                    # inserted), where the winner's row owns the
                    # pair and the loser cleans nothing, the create
                    # route's own rule.
                    if (
                        await app.state.model.get_workspace(workspace_id)
                        is None
                    ):
                        remove_overlay(state_dir, workspace_id)
                        remove_home_volume(state_dir, workspace_id)
                    raise HTTPException(
                        status_code=409, detail="workspace exists"
                    ) from None
                except (ValueError, MicrovmError, OSError) as exc:
                    # The row build failed after the copy (a mounts
                    # column that no longer parses among them): the
                    # same rollback, the same named 503.
                    for dst in copied:
                        dst.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=503,
                        detail=f"the clone's row build failed: {exc}",
                    ) from None
                except BaseException:
                    # An abort between the copy and the insert still
                    # leaves no orphaned pair (ensure_artifacts'
                    # rollback rule).
                    for dst in copied:
                        dst.unlink(missing_ok=True)
                    raise
        row["mount_issues"] = issue_payloads(await row_mount_issues(app, row))
        return Response(
            status_code=201,
            content=json.dumps(row),
            media_type="application/json",
        )

    return api
