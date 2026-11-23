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

#: The system deployment's runtime root (#1080): the system
#: unit's ``RuntimeDirectory`` under ``/run``, where a daemon
#: serving a whole host binds. A process's own
#: ``$XDG_RUNTIME_DIR`` stays the default a same-user pair
#: uses; this root is the second candidate an unconfigured
#: client falls to and the path the system unit names through
#: ``MSKSD_SOCKET``.
SYSTEM_RUNTIME_ROOT = Path("/run")


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
    client side. The default is the caller's own runtime root
    (the same-user deployment's path). A daemon with no runtime
    root (a container, a system unit) serves TCP alone until
    ``MSKSD_SOCKET`` names a path — the system unit points it at
    :func:`system_socket_path`.
    """
    root = runtime_root()
    return root / SOCKET_DIR / SOCKET_NAME if root else None


def system_socket_path() -> Path:
    """The system deployment's socket (#1080):
    ``/run/msks/msksd.sock``, the path a daemon serving a whole
    host binds under its system unit's runtime directory."""
    return SYSTEM_RUNTIME_ROOT / SOCKET_DIR / SOCKET_NAME


def socket_candidates() -> list[Path]:
    """The paths an unconfigured client checks: its own runtime
    root's socket first (the same-user daemon's path), then the
    system deployment's ``/run/msks/msksd.sock`` (#1080) — a
    client whose runtime root holds no socket falls to the
    socket a system-unit daemon serves, so a member of the
    socket's group connects with zero configuration. The two
    coincide when the runtime root is ``/run`` itself, and one
    candidate answers. A client that finds nothing there names
    the path explicitly (``socket:``, ``MSKSC_SOCKET``,
    ``--daemon``) — a socket somewhere else is a deployment
    choice the client does not guess at.
    """
    candidates: list[Path] = []
    default = default_socket_path()
    if default is not None:
        candidates.append(default)
    system = system_socket_path()
    if system not in candidates:
        candidates.append(system)
    return candidates


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
