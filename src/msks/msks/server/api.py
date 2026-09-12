"""The /api/v1 surface: versioned, token-authenticated, one listener (#8).

Routes are thin: they parse, call the model layer or the microvm seam,
and shape responses. No business logic lives here, and no database
query is built outside ``msks.model``.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import __version__ as fastapi_version
from pydantic import BaseModel, Field

from .. import __version__
from ..microvm.spec import VmSpec
from .auth import require_token
from .events import EventHub, relay
from .watcher import watch_loop


class TokenCreate(BaseModel):
    name: str = "api"


class WorkspaceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    kernel: str
    initrd: str | None = None
    rootfs: str
    cmdline: str = "console=hvc0 root=/dev/vda rw"
    cpus: int = Field(default=2, ge=1, le=64)
    mem_mib: int = Field(default=1024, ge=64, le=1 << 15)


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
        await app.state.model.create_all()
        await app.state.model.bootstrap_token()
        watcher = asyncio.create_task(watch_loop(app, hub))
        api.state.watcher = watcher
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    api = FastAPI(title="msksd", version=__version__, lifespan=lifespan)
    api.state.msks_app = app
    api.state.hub = hub

    @api.get("/api/v1/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__, "fastapi": fastapi_version}

    @api.post("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def create_token(body: TokenCreate) -> dict:
        token_id, plaintext = await app.state.model.create_token(body.name)
        return {"id": token_id, "name": body.name, "token": plaintext}

    @api.get("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def list_tokens() -> list[dict]:
        return await app.state.model.list_tokens()

    @api.delete("/api/v1/tokens/{token_id}", dependencies=[Depends(require_token)])
    async def revoke_token(token_id: int) -> dict:
        if not await app.state.model.revoke_token(token_id):
            raise HTTPException(status_code=404, detail="no such token")
        return {"revoked": token_id}

    @api.post("/api/v1/workspaces", dependencies=[Depends(require_token)])
    async def create_workspace(body: WorkspaceCreate) -> dict:
        if await app.state.model.get_workspace(body.id) is not None:
            raise HTTPException(status_code=409, detail="workspace exists")
        return await app.state.model.create_workspace(spec_for(body.model_dump()))

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
        await app.state.microvm.launch(spec_for(row))
        await app.state.model.set_status(workspace_id, "running")
        return {"id": workspace_id, "status": "running"}

    @api.post(
        "/api/v1/workspaces/{workspace_id}/stop", dependencies=[Depends(require_token)]
    )
    async def stop_workspace(workspace_id: str) -> dict:
        await _workspace_or_404(app, workspace_id)
        await app.state.microvm.shutdown(workspace_id)
        await app.state.model.set_status(workspace_id, "stopped")
        return {"id": workspace_id, "status": "stopped"}

    @api.delete(
        "/api/v1/workspaces/{workspace_id}", dependencies=[Depends(require_token)]
    )
    async def delete_workspace(workspace_id: str) -> dict:
        await _workspace_or_404(app, workspace_id)
        await app.state.microvm.shutdown(workspace_id)
        await app.state.microvm.cleanup(workspace_id)
        await app.state.model.delete_workspace(workspace_id)
        return {"deleted": workspace_id}

    @api.websocket("/api/v1/events")
    async def events(socket: WebSocket) -> None:
        await socket.accept()
        queue = hub.subscribe()
        try:
            await pump_until_disconnect(socket, queue)
        finally:
            hub.unsubscribe(queue)

    return api


async def pump_until_disconnect(socket: WebSocket, queue) -> None:
    """Deliver events until the client disconnects (or the relay ends)."""
    receiver = asyncio.create_task(wait_for_disconnect(socket))
    sender = asyncio.create_task(relay(queue, socket.send))
    done, pending = await asyncio.wait(
        {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()


async def wait_for_disconnect(socket: WebSocket) -> None:
    """None on disconnect; the relay task owns delivery meanwhile."""
    try:
        await socket.receive()
    except WebSocketDisconnect:
        return None
