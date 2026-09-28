"""The persistent-volume routes (#14, #80, #184): resize, the
home-volume export and import streams, and the move-lock
machinery that orders them against boots."""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.requests import ClientDisconnect

from ...microvm.errors import MicrovmError
from ...persist import (
    base_info,
    grow_overlay,
    home_volume_path,
    open_sized,
    overlay_path,
    read_volume,
    volume_check,
    volume_direction,
    volume_move,
)
from ...persist import (
    import_home_volume as import_home_volume_from_stream,
)
from ...storage import create_refusal
from .deps import require_token
from .rows import (
    home_volume_guard,
    host_mismatch,
    rechecked_row,
    workspace_or_404,
)
from .schemas import WorkspaceResize


async def home_volume_lock(app, workspace_id: str) -> asyncio.Lock:
    """Serialize a workspace's volume moves against its boots (#80).

    A boot attaches the volume file by path; an install that
    renames a new volume over that path mid-boot silently loses
    every guest write after the rename. One lock per workspace,
    held by start across launch and by both volume routes across
    their whole exchange, orders the pair: the boot waits out an
    in-flight move and boots the installed volume, and a move that
    arrives after a boot sees the running row and answers 409.
    """
    lock = app.state.home_locks.setdefault(workspace_id, asyncio.Lock())
    return lock


async def acquire_move_lock(app, workspace_id: str) -> asyncio.Lock:
    """Acquire the workspace's move-lock, or answer the named 409.

    A stalled reader holds an export's lock as long as its
    connection lives; a waiter that blocked on it would hang with
    it. Waiters give up after ``move_wait_timeout_s`` and name the
    move in flight (#80 review).
    """
    lock = await home_volume_lock(app, workspace_id)
    try:
        async with asyncio.timeout(app.state.settings.vmm.move_wait_timeout_s):
            await lock.acquire()
    except TimeoutError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {workspace_id} has a volume move in flight; "
                f"retry when it finishes"
            ),
        ) from None
    return lock


@contextlib.asynccontextmanager
async def move_lock(app, workspace_id: str):
    """acquire_move_lock as a context: start, delete, and the
    import route hold it this way."""
    lock = await acquire_move_lock(app, workspace_id)
    try:
        yield lock
    finally:
        lock.release()


class HoldingStreamingResponse(StreamingResponse):
    """A streaming response that owns its fd and lock to its last
    send (#80 review).

    The release is bound to the response's own ``__call__`` — the
    send loop the server awaits — not the body iterator's fate: a
    client disconnect or a send failure unwinds this frame
    deterministically, where an abandoned generator's ``finally``
    would wait on garbage collection.
    """

    def __init__(self, body, *, teardown, **kwargs) -> None:
        super().__init__(body, **kwargs)
        self.teardown = teardown

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.teardown()


def volume_teardown(fd: int, lock: asyncio.Lock):
    """The response's teardown: close the fd, then hand the lock
    back (close first, so the waiter behind the lock never observes
    a live fd; raw-fd close stays EBADF-safe against a late
    threadpool read)."""

    def teardown() -> None:
        with contextlib.suppress(OSError):
            os.close(fd)
        lock.release()

    return teardown


async def locked_export(app, hub, workspace_id: str, home: Path) -> Response:
    """The export under the workspace's move-lock (#80).

    The lock spans the re-check and the open and rides the response
    through the stream: a boot that arrives mid-download waits it
    out instead of attaching a volume whose bytes are leaving. The
    size comes from the open fd, so the served length always
    matches the body it yields.
    """
    lock = await acquire_move_lock(app, workspace_id)
    try:
        await rechecked_row(app, workspace_id)
        try:
            fd, size = await asyncio.to_thread(open_sized, home)
        except OSError:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"home volume file for workspace {workspace_id} "
                    f"is missing or unreadable under the state dir; "
                    "a start would rebuild it blank"
                ),
            ) from None
    except BaseException:
        lock.release()
        raise
    return HoldingStreamingResponse(
        export_body(hub, workspace_id, fd),
        teardown=volume_teardown(fd, lock),
        media_type="application/octet-stream",
        headers={
            "content-length": str(size),
            "content-disposition": (
                f'attachment; filename="{workspace_id}.ext4"'
            ),
        },
    )


async def export_body(hub, workspace_id: str, fd: int) -> AsyncIterator[bytes]:
    """The streamed half: the volume's windows. The completion event
    fires only on a clean end of file — a client that disconnects
    mid-download cancelled no export."""
    moved = 0
    async for window in read_volume(fd):
        moved += len(window)
        yield window
    await hub.publish("home.exported", {"id": workspace_id, "bytes": moved})


async def installed_volume(
    state_dir: Path, workspace_id: str, request: Request
) -> int:
    """The upload's installed byte count, or its named HTTP failure.

    A body that is not ext4 and a body the client cut off are
    client errors; a disk-side failure is the daemon's — and all
    three leave the workspace's existing volume in place.
    """
    try:
        return await import_home_volume_from_stream(
            state_dir, workspace_id, request.stream()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (MicrovmError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ClientDisconnect as exc:
        raise HTTPException(
            status_code=400,
            detail="the upload ended before its body completed; "
            "the workspace kept its existing volume",
        ) from exc


async def locked_import(
    app, hub, workspace_id: str, request: Request
) -> Response:
    """The upload under the workspace's move-lock (#80).

    The row and seam re-read under the lock plus the lock itself
    close the boot race: a start either finished (the re-read
    refuses) or is waiting (the boot opens the installed volume).
    """
    await rechecked_row(app, workspace_id)
    state_dir = app.state.settings.vmm.state_dir
    total = await installed_volume(state_dir, workspace_id, request)
    await hub.publish("home.imported", {"id": workspace_id, "bytes": total})
    return Response(
        status_code=200,
        content=json.dumps({"id": workspace_id, "bytes": total}),
        media_type="application/json",
    )


def router(app, hub) -> APIRouter:
    """The volume routes: resize and the two byte streams."""
    api = APIRouter()

    @api.post(
        "/api/v1/workspaces/{workspace_id}/resize",
        dependencies=[Depends(require_token)],
    )
    async def resize_workspace(
        workspace_id: str, body: WorkspaceResize
    ) -> dict:
        """Move a stopped workspace's sizes (#184) and topology
        (#277): the home volume grows or shrinks, the overlay grows,
        and cpus and memory become row facts the next boot reads.

        The same guards a home-volume move carries: free lifecycle
        statuses, the placement check, the move-lock against a
        concurrent boot, and the live seam re-check. The floor never
        speaks here — a resize writes MiBs of filesystem metadata,
        and a shrink gives bytes back.
        """
        row = await workspace_or_404(app, workspace_id)
        # The ref (name or id) resolved: everything keyed below uses
        # the row's immutable id (#246).
        workspace_id = row["id"]
        if mismatch := host_mismatch(app, row):
            raise HTTPException(status_code=409, detail=mismatch)
        if (
            body.root_mib is None
            and body.home_mib is None
            and body.cpus is None
            and body.mem_mib is None
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "nothing to resize: name root_mib, home_mib, cpus, "
                    "mem_mib, or any mix"
                ),
            )
        async with move_lock(app, workspace_id):
            row = await rechecked_row(app, workspace_id)
            vmm = app.state.settings.vmm
            state_dir = vmm.state_dir
            home = home_volume_path(state_dir, workspace_id)
            overlay = overlay_path(state_dir, workspace_id)
            moved: list[str] = []
            if body.root_mib is not None:
                # The files are the truth, not the row: create clamps
                # the overlay to the base image's size, and a home
                # import can swap the volume in at any size. Root
                # grows only — its partition table and filesystem
                # belong to the guest. (#187.)
                if overlay.is_file():
                    virtual_b, image_format = await base_info(
                        overlay, vmm.qemu_img, "the root overlay"
                    )
                    if image_format != "qcow2" or virtual_b == 0:
                        # qemu-img probes format, and a corrupt or
                        # truncated overlay answers "raw, 0 bytes" —
                        # a grow would silently truncate garbage. Name
                        # the corrupt file instead of moving it.
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"the root overlay for {workspace_id} is "
                                "not a readable qcow2 image (qemu-img "
                                f"reports {image_format}, {virtual_b} "
                                "bytes) — restore it with msks rm and a "
                                "fresh create, or a factory reset"
                            ),
                        )
                    ceiling_mib = virtual_b // (1024 * 1024)
                    if body.root_mib < ceiling_mib:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "the root overlay only grows; this one is "
                                f"{ceiling_mib} MiB and the request asked "
                                f"for {body.root_mib} MiB — msks rm and a "
                                "fresh create, or factory reset, reclaim a "
                                "root instead"
                            ),
                        )
                    if body.root_mib > ceiling_mib:
                        await grow_overlay(overlay, body.root_mib, vmm)
                        moved.append(f"root grew to {body.root_mib} MiB")
                    # Equal to the file: only the row catches up.
                    await app.state.model.set_sizes(
                        workspace_id, body.root_mib, None
                    )
                elif body.root_mib != row["root_mib"]:
                    # The heal contract: no file, a new size — the row
                    # records it and the next start builds the blank
                    # overlay at it.
                    moved.append(f"root grew to {body.root_mib} MiB")
                    await app.state.model.set_sizes(
                        workspace_id, body.root_mib, None
                    )
            if body.home_mib is not None:
                if home.is_file():
                    if home.stat().st_size != body.home_mib * 1024 * 1024:
                        # e2fsck failures stay the daemon's 503; only
                        # the executed shrink's refusal is the
                        # client's to fix (data must move out of the
                        # tail).
                        await volume_check(home, vmm)
                        executed_shrink = (
                            volume_direction(home, body.home_mib) == "shrank"
                        )
                        try:
                            direction = await volume_move(
                                home, body.home_mib, vmm
                            )
                        except MicrovmError as exc:
                            if not executed_shrink:
                                raise
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"the shrink refused: {exc}; free data "
                                    "in the workspace's /home (or shrink "
                                    "less) and retry"
                                ),
                            ) from None
                        except OSError as exc:
                            # The volume vanished between the check
                            # and the move (an out-of-band rm): a
                            # named 503, not a bare 500.
                            raise HTTPException(
                                status_code=503,
                                detail=(
                                    f"the home volume for {workspace_id} "
                                    f"became unreachable mid-resize: {exc}"
                                ),
                            ) from None
                        moved.append(
                            f"home {direction} to {body.home_mib} MiB"
                        )
                    # The row follows the file, whichever moved.
                    await app.state.model.set_sizes(
                        workspace_id, None, body.home_mib
                    )
                elif body.home_mib != row["home_mib"]:
                    # The heal contract, the overlay's twin.
                    moved.append(f"home to {body.home_mib} MiB (fresh)")
                    await app.state.model.set_sizes(
                        workspace_id, None, body.home_mib
                    )
            # The topology (#277): cpus and memory are row facts the
            # next boot reads (the VmSpec builds from the row), so a
            # row write is the whole move — no host file to move, no
            # guest tool to run. The disk sides' one-sided shape
            # holds here too: only a change records, so an identical
            # resize is a no-op.
            topology_moved = False
            if body.cpus is not None and body.cpus != row["cpus"]:
                moved.append(f"cpus set to {body.cpus}")
                topology_moved = True
            if body.mem_mib is not None and body.mem_mib != row["mem_mib"]:
                moved.append(f"mem set to {body.mem_mib} MiB")
                topology_moved = True
            if topology_moved:
                await app.state.model.set_topology(
                    workspace_id, body.cpus, body.mem_mib
                )
            updated = await app.state.model.get_workspace(workspace_id)
            if updated is None:
                # The row vanished under the move-lock (a concurrent
                # delete won it): answer 404, not a None crash.
                raise HTTPException(
                    status_code=404, detail="no such workspace"
                )
            # Nothing moved and nothing needs recording: idempotent,
            # with no event to announce.
            if not moved:
                return {**updated, "changes": []}
            await hub.publish(
                "workspace.resized",
                {
                    "id": workspace_id,
                    "root_mib": updated["root_mib"],
                    "home_mib": updated["home_mib"],
                    "cpus": updated["cpus"],
                    "mem_mib": updated["mem_mib"],
                    "changes": moved,
                },
            )
            return {**updated, "changes": moved}

    # The home-volume byte streams (#80): export for backup and
    # migration, import to restore or seed. Both refuse a workspace
    # the guard names, and both hold the workspace's move-lock for
    # their whole exchange — see home_volume_lock.
    @api.get(
        "/api/v1/workspaces/{workspace_id}/home",
        dependencies=[Depends(require_token)],
    )
    async def export_home_volume(workspace_id: str) -> Response:
        """Stream the workspace's /home volume out (#80): the volume
        file's bytes, verbatim."""
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        guard = home_volume_guard(app, row)
        if guard is not None:
            raise HTTPException(*guard)
        state_dir = app.state.settings.vmm.state_dir
        home = home_volume_path(state_dir, workspace_id)
        return await locked_export(app, hub, workspace_id, home)

    @api.put(
        "/api/v1/workspaces/{workspace_id}/home",
        dependencies=[Depends(require_token)],
    )
    async def import_home_volume(
        workspace_id: str, request: Request
    ) -> Response:
        """Replace the workspace's /home volume with the request body
        (#80): the uploaded ext4 image lands atomically — a failed or
        refused upload leaves the old volume in place."""
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        guard = home_volume_guard(app, row)
        if guard is not None:
            raise HTTPException(*guard)
        # The floor (#184): an import streams a whole volume at the
        # state disk. The client's Content-Length sizes it when sent
        # (a chunked upload carries none and gets the floor alone);
        # the check runs before the body starts, so a refused import
        # installs nothing.
        incoming_b = 0
        length = request.headers.get("content-length", "")
        if length.isdigit():
            incoming_b = int(length)
        refusal = create_refusal(
            app.state.settings.vmm, "importing a home volume", incoming_b
        )
        if refusal is not None:
            raise HTTPException(status_code=507, detail=refusal)
        async with move_lock(app, workspace_id):
            return await locked_import(app, hub, workspace_id, request)

    return api
