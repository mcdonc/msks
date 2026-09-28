"""The bearer-token routes (#8): create, list, revoke."""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from .deps import require_token
from .schemas import TokenCreate


def router(app) -> APIRouter:
    """The bearer-token routes."""
    api = APIRouter()

    @api.post("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def create_token(body: TokenCreate) -> Response:
        token_id, plaintext = await app.state.model.create_token(body.name)
        payload = {"id": token_id, "name": body.name, "token": plaintext}
        return Response(
            status_code=201,
            content=json.dumps(payload),
            media_type="application/json",
        )

    @api.get("/api/v1/tokens", dependencies=[Depends(require_token)])
    async def list_tokens() -> list[dict]:
        return await app.state.model.list_tokens()

    @api.delete(
        "/api/v1/tokens/{token_id}", dependencies=[Depends(require_token)]
    )
    async def revoke_token(token_id: int) -> dict:
        if not await app.state.model.revoke_token(token_id):
            raise HTTPException(status_code=404, detail="no such token")
        return {"revoked": token_id}

    return api
