"""The bearer-token routes (#8): create, list, revoke."""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ...model.workspaces import sees_all_rows
from .deps import authed_caller
from .schemas import TokenCreate


def operator_only(caller: int) -> None:
    """The token routes' gate (#1082): tokens are the operator's
    surface — a bearer token read over the TCP surface maps onto
    the daemon's own uid and bypasses the owner filter, so a group
    member minting one would hold every owner's workspaces one
    dial later. Root and the daemon's own uid (the single-user
    deployment, the dev daemon) mint, list, and revoke as before;
    every other peer's access grant is its group membership, not a
    token."""
    if not sees_all_rows(caller):
        raise HTTPException(
            status_code=403,
            detail=(
                "tokens are the operator's surface; the local "
                "listener's group membership is the access grant"
            ),
        )


def router(app) -> APIRouter:
    """The bearer-token routes."""
    api = APIRouter()

    @api.post("/api/v1/tokens")
    async def create_token(
        body: TokenCreate, caller: int = Depends(authed_caller)
    ) -> Response:
        operator_only(caller)
        token_id, plaintext = await app.state.model.create_token(body.name)
        payload = {"id": token_id, "name": body.name, "token": plaintext}
        return Response(
            status_code=201,
            content=json.dumps(payload),
            media_type="application/json",
        )

    @api.get("/api/v1/tokens")
    async def list_tokens(
        caller: int = Depends(authed_caller),
    ) -> list[dict]:
        operator_only(caller)
        return await app.state.model.list_tokens()

    @api.delete("/api/v1/tokens/{token_id}")
    async def revoke_token(
        token_id: int, caller: int = Depends(authed_caller)
    ) -> dict:
        operator_only(caller)
        if not await app.state.model.revoke_token(token_id):
            raise HTTPException(status_code=404, detail="no such token")
        return {"revoked": token_id}

    return api
