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


def runtime_root() -> Path:
    """The runtime root a same-user deployment uses.

    ``$XDG_RUNTIME_DIR`` (a systemd user session's per-user
    runtime dir, ``/run/user/<uid>``) when set — a per-user path
    the daemon user owns without root help. System deployments
    (a daemon running as a service user, root included) run with
    the variable unset and land on ``/run``.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg:
        return Path(xdg)
    return Path("/run")


def default_socket_path() -> Path:
    """Where the daemon binds its local listener by default.

    ``MSKSD_SOCKET`` overrides this on the daemon side; the
    client's config file and ``MSKSC_SOCKET`` name it on the
    client side. The default pairs with
    :func:`socket_candidates`: the daemon binds exactly one of
    the candidates.
    """
    return runtime_root() / SOCKET_DIR / SOCKET_NAME


def socket_candidates() -> list[Path]:
    """The paths an unconfigured client checks, in order.

    The client's own runtime root first (the same-user case: the
    daemon bound there), then ``/run`` (a system daemon bound
    there; an operator's session reaches it through the socket
    file's group bits). The first candidate that names a socket
    wins; the list is deduplicated for hosts whose
    ``XDG_RUNTIME_DIR`` points at ``/run`` itself.
    """
    candidates = [
        default_socket_path(),
        Path("/run") / SOCKET_DIR / SOCKET_NAME,
    ]
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if str(candidate) not in seen:
            seen.add(str(candidate))
            unique.append(candidate)
    return unique


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
