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

from .. import __version__
from ..microvm.errors import MicrovmError
from ..microvm.spec import VmSpec
from .auth import require_token
from .events import EventHub, relay
from .watcher import watch_loop


class TokenCreate(BaseModel):
    name: str = "api"


WORKSPACE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class WorkspaceCreate(BaseModel):
    # The id becomes a path component under state_dir/vms/ and a pod
    # name on k8s — the charset keeps both safe (no traversal, no
    # invalid names) and stays DNS-label-compatible.
    id: str = Field(min_length=1, max_length=64, pattern=WORKSPACE_ID_PATTERN)
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
        app.state.model.migrate()
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
        try:
            row = await app.state.model.create_workspace(spec_for(body.model_dump()))
        except IntegrityError:
            # The check-then-insert race lost; same answer for the client.
            raise HTTPException(status_code=409, detail="workspace exists") from None
        return Response(
            status_code=201, content=json.dumps(row), media_type="application/json"
        )

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

    @api.exception_handler(MicrovmError)
    async def microvm_error(_request, exc: MicrovmError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

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
