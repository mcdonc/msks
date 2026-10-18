"""The local-listener socket vocabulary (#555).

The default places the daemon's unix listener and the client's
socket dial look for it — one rule both sides share so an
unconfigured pair finds each other on the same host. Stdlib only,
like every ``spec`` leaf: the daemon's server layer and the
client's connection layer both import from here.
"""

import os
import stat
from pathlib import Path

#: The socket's name under its runtime directory.
SOCKET_NAME = "msksd.sock"

#: The subdirectory of the runtime root the socket lives in.
SOCKET_DIR = "msks"


def runtime_root() -> Path | None:
    """The runtime root a same-user deployment uses:
    ``$XDG_RUNTIME_DIR`` (a systemd user session's per-user
    runtime dir, ``/run/user/<uid>``) — a path the daemon user
    owns without root help. A process without one (a container, a
    system unit) has no default socket: the daemon serves TCP
    alone until ``MSKSD_SOCKET`` names a path, the same spelling
    an operator uses to relocate it.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    return Path(xdg) if xdg else None


def default_socket_path() -> Path | None:
    """Where the daemon binds its local listener by default.

    ``MSKSD_SOCKET`` overrides this on the daemon side; the
    client's config file and ``MSKSC_SOCKET`` name it on the
    client side. The default pairs with
    :func:`socket_candidates`: the daemon binds exactly one of
    the candidates — or none, when no runtime root exists and
    the TCP surface serves alone.
    """
    root = runtime_root()
    return root / SOCKET_DIR / SOCKET_NAME if root else None


def socket_candidates() -> list[Path]:
    """The paths an unconfigured client checks: its own runtime
    root's socket, the same path the daemon bound. A client that
    finds nothing there names the path explicitly (``socket:``,
    ``MSKSC_SOCKET``, ``--daemon``) — a socket somewhere else is
    a deployment choice the client does not guess at.
    """
    default = default_socket_path()
    return [default] if default else []


def is_socket_file(path: Path) -> bool:
    """True when *path* names a unix socket file."""
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def find_default_socket() -> Path | None:
    """The first default candidate that names a socket, or None.

    Existence is the whole test: a live daemon leaves the socket
    file behind while it serves, so a client that finds one dials
    it. A path that exists but is not a socket (a leftover
    directory, a typo) is skipped, not dialed.
    """
    for candidate in socket_candidates():
        if is_socket_file(candidate):
            return candidate
    return None
