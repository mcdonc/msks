"""The api surface's authentication seams.

One bearer-token dependency for every REST route and one
handshake authenticator for every websocket route, so the
credential never rides a URL (#8, #216). A request that arrived
through the local unix listener (#555) answers instead by peer
credential: the daemon's own uid or a member of the socket's
group passes without a token, and every other caller — TCP or
socket — takes the bearer path unchanged.
"""

from fastapi import HTTPException, Request, WebSocket

from .. import localsocket


def local_peer_ok(scope: dict, app) -> bool:
    """True when *scope* carries a peer credential the local
    listener's policy accepts (#555)."""
    uid = localsocket.is_unix_scope(scope)
    if uid is None:
        return False
    gid = getattr(app.state, "unix_socket_gid", None)
    return localsocket.peer_allowed(uid, gid)


async def require_token(request: Request) -> None:
    """FastAPI dependency: a local peer credential or a valid,
    unrevoked bearer token, else 401 (#8, #555)."""
    if local_peer_ok(request.scope, request.app.state.msks_app):
        return
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

    A local peer credential (#555) or a valid Bearer token accepts
    the socket; anything else accepts bare and closes 4401: the
    close-code contract the clients name for a token the daemon
    does not hold.
    """
    if not local_peer_ok(socket.scope, app):
        token = bearer_token(socket)
        if token is None or not await app.state.model.token_valid(token):
            await socket.accept()
            await socket.close(code=4401)
            return False
    await socket.accept()
    return True
