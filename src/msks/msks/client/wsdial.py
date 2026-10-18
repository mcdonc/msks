"""The websocket dial: one address vocabulary, two transports.

Every client websocket surface (console, forward, the egress
event stream, the consent decider) dials through here. The
address is the daemon's, with its websocket scheme: ``wss`` or
``ws`` for a TCP daemon, ``ws+unix://<path>`` for the local
listener (#555) — the socket path percent-encoded into the
scheme's authority, the API path behind it, the same convention
the REST side's ``http+unix://`` address uses.

The local shape dials ``websockets.unix_connect``: no TLS (the
kernel vouches for both ends of a unix socket) and, when the
daemon's peer-credential policy answers the connect, no token
header either (#555) — ``token`` is None there and the handshake
carries no Authorization header.
"""

import ssl
from urllib.parse import unquote

import websockets

from . import wsauth

#: The scheme that carries a unix socket path (the address form
#: :func:`msks.client.wsdial.ws_url` builds from the daemon's
#: ``http+unix://`` address).
UNIX_WS = "ws+unix://"

#: A placeholder authority for the unix dial's URI: the websocket
#: handshake needs a URL-shaped uri, and the socket path — not
#: any hostname — is the dial target.
UNIX_AUTHORITY = "ws://unix"


def ws_url(base_url: str, path: str) -> str:
    """The websocket address for *path* under the daemon at
    *base_url*.

    ``https`` becomes ``wss``, ``http`` becomes ``ws``, and the
    local listener's ``http+unix://`` becomes ``ws+unix://`` —
    the path encoding rides through unchanged. A bare host
    (no ``://``) takes ``wss``, the caller's shorthand for a
    hostname that serves TLS.
    """
    scheme, sep, rest = base_url.partition("://")
    if not sep:
        scheme, rest = "wss", base_url
    elif scheme == "https":
        scheme = "wss"
    elif scheme == "http+unix":
        scheme = "ws+unix"
    else:
        scheme = "ws"
    return f"{scheme}://{rest}{path}"


def unix_parts(address: str) -> tuple[str, str]:
    """The socket path and API path inside a ``ws+unix://`` address."""
    rest = address[len(UNIX_WS) :]
    encoded, sep, path = rest.partition("/")
    return unquote(encoded), f"/{path}" if sep else "/"


def dial(
    address: str,
    token: str | None,
    ssl_ctx: ssl.SSLContext | None,
    max_size: int,
):
    """The websocket connection, in hand for a clean close on
    failure (either transport)."""
    if address.startswith(UNIX_WS):
        socket_path, path = unix_parts(address)
        return websockets.unix_connect(
            socket_path,
            uri=f"{UNIX_AUTHORITY}{path}",
            additional_headers=(
                wsauth.auth_headers(token) if token is not None else []
            ),
            max_size=max_size,
        )
    return websockets.connect(
        address,
        additional_headers=wsauth.auth_headers(token),
        ssl=None if address.startswith("ws://") else ssl_ctx,
        max_size=max_size,
    )
