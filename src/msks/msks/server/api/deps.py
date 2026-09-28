"""The api surface's authentication seams.

One bearer-token dependency for every REST route and one
handshake authenticator for every websocket route, so the
credential never rides a URL (#8, #216).
"""

from fastapi import HTTPException, Request, WebSocket


async def require_token(request: Request) -> None:
    """FastAPI dependency: a valid, unrevoked bearer token or 401
    (#8)."""
    authorization = request.headers.get("authorization", "")
    model = request.app.state.msks_app.state.model
    scheme, _, plaintext = authorization.partition(" ")
    if scheme.lower() != "bearer" or not plaintext:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not await model.token_valid(plaintext):
        raise HTTPException(status_code=401, detail="invalid or revoked token")


def bearer_token(socket: WebSocket) -> str | None:
    """The Authorization header's Bearer token, or None (#216).

    Every websocket surface takes its token from the same header
    the REST surface reads (:func:`msks.server.api.deps.require_token`
    splits it the same way), so the credential never lands in a URL
    recorder — access log, proxy log, shell history. A future
    browser client cannot set the header on its native
    ``WebSocket`` API; when one appears it takes a short-lived
    ticket over the authenticated REST surface, not a long-lived
    token in a query string.
    """
    authorization = socket.headers.get("authorization", "")
    scheme, _, plaintext = authorization.partition(" ")
    if scheme.lower() != "bearer" or not plaintext:
        return None
    return plaintext


async def authed_accept(app, socket: WebSocket) -> bool:
    """Authenticate the websocket handshake and accept it.

    A valid Bearer token accepts the socket; anything else accepts
    bare and closes 4401: the close-code contract the clients name
    for a token the daemon does not hold.
    """
    token = bearer_token(socket)
    if token is None or not await app.state.model.token_valid(token):
        await socket.accept()
        await socket.close(code=4401)
        return False
    await socket.accept()
    return True
