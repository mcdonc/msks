"""The daemon's local listener: a unix socket with peer-credential
authentication (#555).

Same-host clients reach the daemon through an AF_UNIX socket
instead of the TCP + TLS + token surface: the kernel names the
connecting process (``SO_PEERCRED`` carries its pid, uid, gid
across the connect), so the listener answers a local client with
no token to mint, distribute, or leak. Access is two gates that
agree: the socket file's permission bits gate the ``connect``,
and the peer-credential policy below gates the request — the
daemon's own uid, or a member of the socket file's group.

The plumbing rides uvicorn's own protocol seams: the listener's
HTTP and websocket protocol classes are wrapped so each
connection's peer uid lands in the ASGI scope's ``client`` pair
(``(\"unix\", <uid>)``) — the one fact the auth dependencies
(:mod:`msks.server.api.deps`) read. A TCP connection cannot forge
the marker: only the wrapped protocols set it, and they set it
only when ``getsockopt(SO_PEERCRED)`` answered.
"""

import grp
import os
import pwd
import socket
import struct
import sys
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

import uvicorn

from ..spec.sockets import default_socket_path

#: The marker's kind half: ``scope[\"client\"][0]`` on a request
#: that arrived through the local listener. The pid half of the
#: credential pair is not carried — nothing authorizes by it and
#: a recycled pid would mislead a log reader.
SCOPE_KIND = "unix"

#: ``struct ucred`` — pid, uid, gid as three ints. The constant
#: is Linux's; the guard below keeps non-Linux hosts compiling
#: and the feature inert rather than crashing on import.
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
_UCRED = struct.Struct("3i")


def peer_credentials(transport) -> int | None:
    """The peer's uid for a unix-socket transport, else None.

    ``None`` answers every other transport — TCP, TLS, or a unix
    socket whose credentials the kernel refused — and the caller
    treats it as \"no local proof\": the request falls to the
    bearer-token path.
    """
    sock = transport.get_extra_info("socket")
    if sock is None or sock.family != socket.AF_UNIX:
        return None
    try:
        pid, uid, _gid = _UCRED.unpack(
            sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _UCRED.size)
        )
    except OSError:
        return None
    del pid  # documented above: nothing authorizes by it
    return uid


def wrap_protocols(config: uvicorn.Config) -> None:
    """Give a uvicorn config peer-credential-carrying protocols.

    Both the HTTP protocol and the websocket protocol build the
    ASGI scope's ``client`` pair from the transport's remote
    address, which is empty (``None``) for a unix socket; the
    wrapped classes fill it with the credential pair instead. The
    wrap preserves whichever implementation uvicorn picked
    (httptools or h11, websockets or wsproto) — it subclasses,
    it does not replace.
    """
    if not config.loaded:
        config.load()
    config.http_protocol_class = _peer_creds_http(config.http_protocol_class)
    if config.ws_protocol_class is not None:
        config.ws_protocol_class = _peer_creds_ws(config.ws_protocol_class)


def _peer_creds_http(base: type) -> type:
    class PeerCredsHttpProtocol(base):  # type: ignore[misc, valid-type]
        def connection_made(self, transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            uid = peer_credentials(transport)
            # The wrapped classes serve the unix listener alone; a
            # transport without credentials never reaches one.
            if uid is not None:  # pragma: no cover
                self.client = (SCOPE_KIND, uid)

    return PeerCredsHttpProtocol


def _peer_creds_ws(base: type) -> type:
    class PeerCredsWebSocketProtocol(base):  # type: ignore[misc, valid-type]
        def connection_made(self, transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            uid = peer_credentials(transport)
            # As above: unix transports answer, and only they serve.
            if uid is not None:  # pragma: no cover
                self.client = (SCOPE_KIND, uid)

    return PeerCredsWebSocketProtocol


def bind(path: Path) -> socket.socket:
    """Bind the listening unix socket at *path* (mode 0660).

    The parent directory is created when missing. A stale socket
    file at the path — a daemon that died without unlinking — is
    replaced; a socket a live daemon still answers is refused (a
    second daemon must not steal the first's listener, #555
    review); anything else that occupies the name is refused (a
    regular file there is an operator mistake to surface, not a
    silent clobber). The mode gives the daemon user and the
    socket's group connect rights and denies the rest; an
    operator widening access to a group chgrps the file, and the
    credential policy below agrees with whatever group it holds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    refuse_taken_path(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, 0o660)
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def refuse_taken_path(path: Path) -> None:
    """The one-listener rule on the filesystem: a non-socket file
    at the path is an operator mistake to surface; a socket file
    someone still answers is a live listener to leave alone
    (a second daemon must not steal the first's clients, #555
    review); a socket nobody answers is stale — a daemon that
    died without unlinking — and is replaced."""
    if path.exists() and not path.is_socket():
        raise OSError(f"{path} exists and is not a socket file")
    if path.is_socket():
        if answers(path):
            raise OSError(
                f"{path}: a daemon is already listening there; a "
                "second listener would steal its clients"
            )
        path.unlink()


def answers(path: Path) -> bool:
    """True when something accepts connections at *path*."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def unlink(path: Path) -> None:
    """Remove the socket file at *path* (shutdown's last step)."""
    with suppress(FileNotFoundError):
        path.unlink()


@lru_cache(maxsize=64)
def _in_group(user: str, gid: int) -> bool:
    """True when *user* belongs to group *gid* (name-level check)."""
    with suppress(KeyError):
        return user in grp.getgrgid(gid).gr_mem
    return False


def peer_allowed(uid: int, socket_gid: int | None) -> bool:
    """The local-listener policy: may this peer uid skip the token?

    The daemon's own uid always passes (the same-user deployment
    — ``msksd`` and ``msks`` as one user). Otherwise the peer
    must belong to the socket file's group, as its primary group
    or by membership: the operator who chgrps the socket to a
    shared group has said who else may connect, and this reads
    that answer from the group database rather than trusting the
    connect to imply consent. A peer that passes neither gate
    still reaches the bearer-token path — the socket is one way
    in, not a wall.
    """
    if uid == os.geteuid():
        return True
    if socket_gid is None:
        return False
    with suppress(KeyError):
        entry = pwd.getpwuid(uid)
        if entry.pw_gid == socket_gid:
            return True
        return _in_group(entry.pw_name, socket_gid)
    return False


def is_unix_scope(scope: dict) -> int | None:
    """The peer uid a scope carries, or None.

    The one read the auth dependencies make: a scope whose client
    pair carries the local-listener marker answers its uid. Only
    the wrapped protocols above build that pair, and only over a
    socket the kernel vouched for.
    """
    client = scope.get("client")
    if (
        isinstance(client, tuple)
        and len(client) == 2
        and client[0] == SCOPE_KIND
        and isinstance(client[1], int)
    ):
        return client[1]
    return None


def enabled(path_setting: str | None) -> Path | None:
    """Resolve the ``socket`` setting into the path to bind.

    ``None`` (the setting absent) binds the default path; an
    empty string disables the listener (an operator who wants
    the TCP surface alone names that); any other value is the
    path itself.
    """
    if path_setting is None:
        return default_socket_path()
    if path_setting == "":
        return None
    return Path(path_setting).expanduser()


class QuietServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to its caller.

    Two servers share one process when the local listener runs
    (TCP and unix): the TCP server captures the signals and owns
    the shutdown; this class runs the socket server's serve path
    without installing handlers of its own — a second ``capture_signals``
    would replace the first server's handlers and strand it
    running after a SIGTERM. The call into uvicorn's private
    ``_serve`` is the pinned seam: it is everything ``serve`` does
    without the signal capture, and the end-to-end test holds it —
    a uvicorn upgrade that renames it fails that test here, not a
    daemon at runtime.
    """

    async def serve(self, sockets: list | None = None) -> None:  # type: ignore[override]
        await self._serve(sockets=sockets)  # noqa: SLF001 — the point of the class


def platform_supported() -> bool:
    """True on the platforms the credential plumbing runs on.

    ``SO_PEERCRED`` is Linux's; a daemon started elsewhere keeps
    the TCP surface and refuses the socket setting with a named
    error instead of a runtime surprise.
    """
    # The short-circuit False arm needs a non-Linux host.
    return sys.platform.startswith("linux") and hasattr(  # pragma: no cover
        socket, "SO_PEERCRED"
    )
