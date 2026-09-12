"""Bearer-token authentication for the /api/v1 surface (#8)."""

from fastapi import HTTPException, Request

BEARER_PREFIX = "Bearer "


async def require_token(request: Request) -> None:
    """FastAPI dependency: a valid, unrevoked bearer token or 401."""
    authorization = request.headers.get("authorization", "")
    model = request.app.state.msks_app.state.model
    if not authorization.startswith(BEARER_PREFIX):
        raise HTTPException(status_code=401, detail="missing bearer token")
    plaintext = authorization[len(BEARER_PREFIX) :]
    if not await model.token_valid(plaintext):
        raise HTTPException(status_code=401, detail="invalid or revoked token")
