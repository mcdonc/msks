"""The /api/v1 surface: versioned, token-authenticated, one listener (#8).

Routes are thin: they parse, call the model layer or the microvm seam,
and shape responses. No business logic lives here, and no database
query is built outside ``msks.model``.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import __version__ as fastapi_version
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from .. import __version__, imagestore
from ..imagestore import ImageError
from ..microvm.errors import MicrovmError
from ..microvm.spec import VmSpec
from .auth import require_token
from .events import EventHub, relay
from .watcher import watch_loop


class TokenCreate(BaseModel):
    name: str = "api"


WORKSPACE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class ImageImport(BaseModel):
    """An import request: a host-side path to a container-image tar.

    The daemon's filesystem must reach it (a store path via the
    appliance's share, or a state-disk path) — the API deliberately
    does not accept uploads yet.
    """

    source: str


class WorkspaceCreate(BaseModel):
    # The id becomes a path component under state_dir/vms/ and a pod
    # name on k8s — the charset keeps both safe (no traversal, no
    # invalid names) and stays DNS-label-compatible.
    id: str = Field(min_length=1, max_length=64, pattern=WORKSPACE_ID_PATTERN)
    # Either a catalog reference (image: "name:version", "name", or
    # hash — the default image when omitted) or explicit boot
    # artifacts (kernel/rootfs paths; the pre-catalog shape the
    # tests and dev flows still use).
    image: str | None = None
    kernel: str | None = None
    initrd: str | None = None
    rootfs: str | None = None
    cmdline: str | None = None
    cpus: int = Field(default=2, ge=1, le=64)
    mem_mib: int = Field(default=1024, ge=64, le=1 << 15)
    # Persistent-artifact sizes (#14), fixed at create; unset takes
    # the MSKSD_ROOT_MIB / MSKSD_HOME_MIB defaults.
    root_mib: int | None = Field(default=None, ge=256, le=65536)
    home_mib: int | None = Field(default=None, ge=64, le=65536)


def bootstrap_default_image(app) -> None:
    """Import MSKSD_DEFAULT_IMAGE once, as the catalog default.

    Failure is loud but non-fatal: a bad pointer must not take the
    daemon down with it (the operator can still import by API).
    """
    source = app.state.settings.vmm.default_image
    if not source:
        return
    state_dir = app.state.settings.vmm.state_dir
    imagestore.sweep_crash_leftovers(state_dir)
    try:
        warm = imagestore.warm_import(Path(source), state_dir)
        if warm is not None:
            return
        record = imagestore.import_archive(Path(source), state_dir)
    except (ImageError, OSError) as exc:
        # Genuinely non-fatal: a bad pointer or a full state disk must
        # not take the daemon down with it (import remains available
        # by API once the operator clears it).
        print(f"msksd: default image import failed: {exc}")
        return
    # Empty-before-import is the first-boot case; the sole-entry
    # fallback would resolve anyway, but the pointer makes the
    # designation explicit and survives later imports.
    if len(imagestore.list_images(state_dir)) == 1:
        imagestore.set_default(record.hash, state_dir)
        print(f"msksd: default image {record.ref} ({record.hash[:12]}) imported")


def image_record(app, body: WorkspaceCreate):
    """The requested catalog record, or the default when omitted."""
    state_dir = app.state.settings.vmm.state_dir
    if body.image is not None:
        try:
            record = imagestore.resolve(body.image, state_dir)
        except ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if record is None:
            raise HTTPException(status_code=404, detail=f"no such image: {body.image}")
        return record
    return imagestore.default_image(state_dir)


def resolve_boot(app, body: WorkspaceCreate) -> dict:
    """Fill kernel/initrd/rootfs/cmdline from the image catalog.

    Explicit fields win over the image; the image wins over the
    default; nothing resolves at all is a client error. The result
    also carries the #14 facts: the catalog hash the overlay will
    bind to (None for explicit boot artifacts) and the artifact
    sizes.
    """
    record = image_record(app, body)
    kernel, rootfs = boot_pair(body, record)
    if (body.kernel is None) != (body.rootfs is None):
        raise HTTPException(status_code=400, detail="kernel and rootfs come together")
    return {
        "id": body.id,
        "kernel": kernel,
        "initrd": default_initrd(body, record),
        "rootfs": rootfs,
        "cmdline": default_cmdline(body, record),
        "cpus": body.cpus,
        "mem_mib": body.mem_mib,
        "image_hash": bound_image_hash(body, record),
        **artifact_sizes(app, body),
    }


def bound_image_hash(body: WorkspaceCreate, record) -> str | None:
    """The catalog hash the workspace's overlay binds to, if any.

    Binding follows the root disk: the image's rootfs only carries a
    workspace that did not override it with an explicit path."""
    if body.rootfs is None and record is not None:
        return record.hash
    return None


def artifact_sizes(app, body: WorkspaceCreate) -> dict:
    """root/home sizes: the request's, else the settings defaults."""
    vmm = app.state.settings.vmm
    return {
        "root_mib": body.root_mib if body.root_mib is not None else vmm.root_mib,
        "home_mib": body.home_mib if body.home_mib is not None else vmm.home_mib,
    }


def boot_pair(body: WorkspaceCreate, record) -> tuple[str, str]:
    """kernel/rootfs: explicit fields win, the record fills the rest."""
    if body.kernel is not None and body.rootfs is not None:
        return body.kernel, body.rootfs
    if record is None:
        raise HTTPException(
            status_code=400,
            detail="kernel/rootfs (or image, or a default image) required",
        )
    return fill(body.kernel, record.kernel), fill(body.rootfs, record.rootfs)


def fill(explicit: str | None, from_record: Path) -> str:
    """One field: the explicit value, else the record's path."""
    return explicit if explicit is not None else str(from_record)


def default_initrd(body: WorkspaceCreate, record) -> str | None:
    """The image's initrd when booting wholly from the catalog."""
    if body.initrd is None and record is not None and body.kernel is None:
        return str(record.initrd)
    return body.initrd


def default_cmdline(body: WorkspaceCreate, record) -> str:
    """The image's cmdline, or the legacy default with no record."""
    if body.cmdline is not None:
        return body.cmdline
    if record is not None:
        return record.cmdline
    return "console=hvc0 root=/dev/vda rw"


def spec_for(row: dict) -> VmSpec:
    """Rebuild the seam's VmSpec from a workspace row."""
    initrd = None if row["initrd"] is None else Path(row["initrd"])
    return VmSpec(
        workspace_id=row["id"],
        kernel=Path(row["kernel"]),
        rootfs=Path(row["rootfs"]),
        cmdline=row["cmdline"],
        cpus=row["cpus"],
        mem_mib=row["mem_mib"],
        initrd=initrd,
        root_mib=row["root_mib"],
        home_mib=row["home_mib"],
    )


def owner_host(app) -> str | None:
    """The host recorded as owning a new workspace's artifacts.

    Placement is a local-backend fact: the artifacts are files on one
    host. On k8s the artifacts live in a per-workspace claim the
    cluster places, so no host is recorded and the placement check
    stays out of the way ("None adopts this daemon", below).
    """
    if app.state.settings.vmm.driver == "local":
        return app.state.settings.vmm.host_name
    return None


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


async def _workspace_or_404(app, workspace_id: str) -> dict:
    row = await app.state.model.get_workspace(workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    return row


def build_api(app) -> FastAPI:
    """The FastAPI application bound to one msks App."""
    hub = EventHub()

    @contextlib.asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        watcher: asyncio.Task | None = None
        try:
            app.state.model.migrate()
            await app.state.model.bootstrap_token()
            bootstrap_default_image(app)
            watcher = asyncio.create_task(watch_loop(app, hub))
            api.state.watcher = watcher
            yield
        finally:
            # Close the model's engine so pooled sqlite connections
            # close deterministically — on shutdown and on a failed
            # startup step (migrate/bootstrap can have created the
            # engine before raising). The watcher teardown is its own
            # try so an unexpected watcher error cannot skip the close.
            try:
                if watcher is not None:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher
            finally:
                await app.state.model.close()

    api = FastAPI(title="msksd", version=__version__, lifespan=lifespan)
    api.state.msks_app = app
    api.state.hub = hub

    @api.get("/api/v1/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__, "fastapi": fastapi_version}

    @api.post("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def create_token(body: TokenCreate) -> Response:
        token_id, plaintext = await app.state.model.create_token(body.name)
        payload = {"id": token_id, "name": body.name, "token": plaintext}
        return Response(
            status_code=201, content=json.dumps(payload), media_type="application/json"
        )

    @api.get("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def list_tokens() -> list[dict]:
        return await app.state.model.list_tokens()

    @api.delete("/api/v1/tokens/{token_id}", dependencies=[Depends(require_token)])
    async def revoke_token(token_id: int) -> dict:
        if not await app.state.model.revoke_token(token_id):
            raise HTTPException(status_code=404, detail="no such token")
        return {"revoked": token_id}

    @api.post("/api/v1/workspaces", dependencies=[Depends(require_token)])
    async def create_workspace(body: WorkspaceCreate) -> Response:
        if await app.state.model.get_workspace(body.id) is not None:
            raise HTTPException(status_code=409, detail="workspace exists")
        boot = resolve_boot(app, body)
        # The persistent artifacts (#14) come before the row: a refused
        # create (a leftover artifact from a previous workspace of this
        # id) answers 503 with nothing written and nothing removed, and
        # a row in the table always has its artifacts underneath it.
        try:
            await app.state.microvm.prepare(spec_for(boot))
        except MicrovmError:
            # A racer may have won this id between the 404 check and
            # the strict prepare — its artifacts are the "leftover",
            # and the honest answer is the 409, not a removal plea.
            if await app.state.model.get_workspace(body.id) is not None:
                raise HTTPException(
                    status_code=409, detail="workspace exists"
                ) from None
            raise
        try:
            row = await app.state.model.create_workspace(
                spec_for(boot),
                image_hash=boot["image_hash"],
                host=owner_host(app),
            )
        except IntegrityError:
            # The insert lost the race. The winner's row owns whatever
            # blank artifacts sit at this id's paths now (ours and its
            # are indistinguishable), so nothing is cleaned up — the
            # row-exists-⇒-artifacts-exist invariant must not break.
            raise HTTPException(status_code=409, detail="workspace exists") from None
        return Response(
            status_code=201, content=json.dumps(row), media_type="application/json"
        )

    @api.get("/api/v1/images", dependencies=[Depends(require_token)])
    async def list_images() -> list[dict]:
        state_dir = app.state.settings.vmm.state_dir
        default = imagestore.default_image(state_dir)
        default_hash = default.hash if default is not None else None
        return [
            {
                "hash": image.hash,
                "name": image.name,
                "version": image.version,
                "cmdline": image.cmdline,
                "vsock_shell_port": image.vsock_shell_port,
                "kernel_version": image.kernel_version,
                "kernel_format": image.kernel_format,
                "default": image.hash == default_hash,
            }
            for image in imagestore.list_images(state_dir)
        ]

    @api.post("/api/v1/images", dependencies=[Depends(require_token)])
    async def import_image(body: ImageImport) -> Response:
        state_dir = app.state.settings.vmm.state_dir
        try:
            record = await asyncio.to_thread(
                imagestore.import_archive, Path(body.source), state_dir
            )
        except (ImageError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # The first imported image becomes the default: a fresh
        # appliance answers a bare workspace create immediately (the
        # sole-entry fallback would resolve it, but the pointer keeps
        # the designation explicit and stable across later imports).
        if len(imagestore.list_images(state_dir)) == 1:
            imagestore.set_default(record.hash, state_dir)
        return Response(
            status_code=201,
            content=json.dumps(
                {
                    "hash": record.hash,
                    "name": record.name,
                    "version": record.version,
                    "ref": record.ref,
                }
            ),
            media_type="application/json",
        )

    @api.delete("/api/v1/images/{digest}", dependencies=[Depends(require_token)])
    async def delete_image(digest: str) -> dict:
        state_dir = app.state.settings.vmm.state_dir
        record = next(
            (
                image
                for image in imagestore.list_images(state_dir)
                if image.hash == digest
            ),
            None,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="no such image")
        # An image a workspace still references cannot be removed:
        # its boot paths dangle, the workspace becomes unrestorable,
        # and its overlay would lose its backing file (#14).
        cache_prefix = str(record.kernel.parent) + "/"
        for row in await app.state.model.list_workspaces():
            if row.get("image_hash") == digest or str(row.get("kernel", "")).startswith(
                cache_prefix
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"workspace {row['id']} boots this image",
                )
        imagestore.remove(digest, state_dir)
        return {"removed": digest}

    @api.get("/api/v1/workspaces", dependencies=[Depends(require_token)])
    async def list_workspaces() -> list[dict]:
        return await app.state.model.list_workspaces()

    @api.get("/api/v1/workspaces/{workspace_id}", dependencies=[Depends(require_token)])
    async def get_workspace(workspace_id: str) -> dict:
        return await _workspace_or_404(app, workspace_id)

    @api.post(
        "/api/v1/workspaces/{workspace_id}/start", dependencies=[Depends(require_token)]
    )
    async def start_workspace(workspace_id: str) -> dict:
        row = await _workspace_or_404(app, workspace_id)
        mismatch = host_mismatch(app, row)
        if mismatch is not None:
            # Placement is a fact about the artifacts, not a
            # preference: booting elsewhere would present an empty
            # /home and a pristine root as if they were the data.
            raise HTTPException(status_code=409, detail=mismatch)
        await app.state.microvm.launch(spec_for(row))
        await app.state.model.set_status(workspace_id, "running")
        return {"id": workspace_id, "status": "running"}

    @api.exception_handler(MicrovmError)
    async def microvm_error(_request, exc: MicrovmError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @api.post(
        "/api/v1/workspaces/{workspace_id}/stop", dependencies=[Depends(require_token)]
    )
    async def stop_workspace(workspace_id: str) -> dict:
        row = await _workspace_or_404(app, workspace_id)
        if mismatch := host_mismatch(app, row):
            # A stop from a non-owning host cannot reach the VMM; a
            # local no-op would mark a running VM stopped.
            raise HTTPException(status_code=409, detail=mismatch)
        await app.state.microvm.shutdown(workspace_id)
        await app.state.model.set_status(workspace_id, "stopped")
        return {"id": workspace_id, "status": "stopped"}

    @api.post(
        "/api/v1/workspaces/{workspace_id}/reset", dependencies=[Depends(require_token)]
    )
    async def reset_workspace(workspace_id: str) -> dict:
        """Factory reset: a pristine root, the same /home (#14)."""
        row = await _workspace_or_404(app, workspace_id)
        if mismatch := host_mismatch(app, row):
            # The overlay lives on its owning host; resetting it from
            # here would no-op on this host's (absent) file and lie
            # about a pristine root.
            raise HTTPException(status_code=409, detail=mismatch)
        # The overlay is the running root device — stop the VM first,
        # with kill as the fallback for a wedged one (same contract
        # as delete).
        try:
            await app.state.microvm.shutdown(workspace_id)
        except MicrovmError:
            await app.state.microvm.kill(workspace_id)
        await app.state.microvm.reset(workspace_id)
        await app.state.model.set_status(workspace_id, "created")
        return {"id": workspace_id, "status": "created"}

    @api.delete(
        "/api/v1/workspaces/{workspace_id}", dependencies=[Depends(require_token)]
    )
    async def delete_workspace(workspace_id: str) -> dict:
        row = await _workspace_or_404(app, workspace_id)
        if mismatch := host_mismatch(app, row):
            # Deleting the row from a non-owning host would orphan a
            # possibly-running VM: every route 404s without the row.
            raise HTTPException(status_code=409, detail=mismatch)
        # A wedged VM must still be deletable: a failed graceful
        # shutdown falls back to kill before cleanup.
        try:
            await app.state.microvm.shutdown(workspace_id)
        except MicrovmError:
            await app.state.microvm.kill(workspace_id)
        await app.state.microvm.cleanup(workspace_id)
        await app.state.model.delete_workspace(workspace_id)
        return {"deleted": workspace_id}

    @api.websocket("/api/v1/workspaces/{workspace_id}/console")
    async def console(socket: WebSocket, workspace_id: str) -> None:
        # Byte-stream bridge into a running workspace (#21): the
        # client gets an interactive shell over the same TLS + token
        # as the REST surface. Closing the websocket closes exactly
        # one guest shell session; the workspace keeps running.
        # Accept first, then close with a code: the client sees a
        # specific close reason (4401/4404/4501) instead of a generic
        # HTTP 403 rejection.
        await socket.accept()
        token = socket.query_params.get("token", "")
        if not await app.state.model.token_valid(token):
            await socket.close(code=4401)
            return
        if await app.state.model.get_workspace(workspace_id) is None:
            await socket.close(code=4404)
            return
        try:
            reader, writer = await app.state.microvm.console(workspace_id)
        except MicrovmError as exc:
            # The client is token-authenticated by now: the cause is
            # not a secret, and the close reason is the only channel
            # an operator has for dead-VM vs refused vs deadline
            # (websocket close reasons cap at 123 bytes).
            await socket.close(code=4501, reason=str(exc)[:120])
            return
        try:
            await bridge_console(socket, reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @api.websocket("/api/v1/events")
    async def events(socket: WebSocket) -> None:
        # Websockets cannot carry Authorization headers from browsers;
        # the token rides the query string instead (documented).
        token = socket.query_params.get("token", "")
        if not await app.state.model.token_valid(token):
            await socket.close(code=4401)
            return
        await socket.accept()
        queue = hub.subscribe()
        try:
            await pump_until_disconnect(socket, queue)
        finally:
            hub.unsubscribe(queue)

    return api


async def bridge_console(socket: WebSocket, reader, writer) -> None:
    """Pump raw bytes between the websocket and the vsock stream.

    Two tasks, no queue: backpressure is websocket/TCP flow control
    (the byte stream must not lose or buffer unboundedly, #21).
    Whichever side finishes first (client detach or guest EOF)
    cancels the other.
    """
    to_guest = asyncio.create_task(_ws_to_stream(socket, writer))
    to_client = asyncio.create_task(_stream_to_ws(reader, socket))
    done, pending = await asyncio.wait(
        {to_guest, to_client}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for task in done:
        with contextlib.suppress(Exception):
            task.result()


async def _ws_to_stream(socket: WebSocket, writer) -> None:
    """Client bytes to the guest; returns on disconnect."""
    while True:
        msg = await socket.receive()
        if msg["type"] != "websocket.receive":
            return
        data = msg.get("bytes")
        if data is None:
            data = msg.get("text", "").encode()
        if data:
            writer.write(data)
            await writer.drain()


async def _stream_to_ws(reader, socket: WebSocket) -> None:
    """Guest bytes to the client; returns on guest EOF."""
    while True:
        data = await reader.read(4096)
        if not data:
            return
        await socket.send_bytes(data)


async def pump_until_disconnect(socket: WebSocket, queue) -> None:
    """Deliver events until the client disconnects (or the relay ends)."""
    receiver = asyncio.create_task(wait_for_disconnect(socket))
    sender = asyncio.create_task(relay(queue, socket.send))
    done, pending = await asyncio.wait(
        {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def wait_for_disconnect(socket: WebSocket) -> None:
    """None on disconnect; the relay task owns delivery meanwhile."""
    try:
        await socket.receive()
    except WebSocketDisconnect:
        return None
