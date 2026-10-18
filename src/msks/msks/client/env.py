"""The client's connection settings: env readers, defaults, and TLS.

The #21 client conventions live here once — ``MSKSC_URL`` for a
remote daemon, ``MSKSC_TOKEN`` for a bearer token, ``MSKSC_CAFILE``
to pin the certificate — as a leaf with no transport imports
(#402): modules that only need *where* to connect (the config
file layer, the TUI) import this module, while the REST transport
(:mod:`msks.client.rest`) keeps the calls. The YAML file layer
(:mod:`msks.client.config`) materializes its winners into the
environment these readers read.

#555 adds the local shape: ``MSKSC_SOCKET`` (or the default
socket an unconfigured client finds) connects through the
daemon's unix listener with peer-credential authentication — no
token, no TLS. The two shapes flow through one address string:
a TCP daemon is its ``https://host:port`` URL; the local daemon
is ``http+unix://<percent-encoded path>`` (the curl convention),
and :func:`is_unix` answers which shape a resolved address
holds. There is no implicit TCP default — an unconfigured client
with no socket to find refuses before dialing (#555): silently
reaching whatever listens on a well-known port targets the wrong
daemon.
"""

import os
import ssl
import sys
from urllib.parse import quote, unquote

from ..spec.sockets import find_default_socket, socket_candidates

#: The URL scheme that carries a unix socket path (the curl
#: convention: the path percent-encoded into the host position).
UNIX_SCHEME = "http+unix://"

#: The message an unconfigured client exits with (#555): the
#: socket paths it looked for and the two settings that name a
#: daemon outright.
UNCONFIGURED_MESSAGE = (
    "no daemon configured and no local socket found (looked for "
    "{candidates}); set MSKSC_URL (and MSKSC_TOKEN) for a remote "
    "daemon, MSKSC_SOCKET for a local one, or start msksd"
)


def unix_url(path: str) -> str:
    """The address form of a unix socket path (slashes encoded —
    the path is the URL's authority, the curl convention)."""
    return UNIX_SCHEME + quote(path, safe="")


def unix_path(url: str) -> str:
    """The socket path inside an :data:`UNIX_SCHEME` address."""
    return unquote(url[len(UNIX_SCHEME) :])


def is_unix(url: str) -> bool:
    """True when *url* addresses the daemon's local listener."""
    return url.startswith(UNIX_SCHEME)


def env_url() -> str:
    """The daemon's address: a configured URL or socket, else the
    default socket an unconfigured client finds, else a refusal.

    A configured ``MSKSC_URL`` wins (the environment beats the
    file layer, which materializes into it); ``MSKSC_SOCKET``
    names the local listener directly; an unconfigured client
    looks for the default socket a same-host daemon binds. The
    empty string is the unset form for both variables — the file
    layer and the shell presets lean on it.
    """
    url = (os.environ.get("MSKSC_URL", "") or "").rstrip("/")
    if url:
        return url
    sock = os.environ.get("MSKSC_SOCKET", "").strip()
    if sock:
        return unix_url(sock)
    return unconfigured_or_default()


def unconfigured_or_default() -> str:
    """The unconfigured tail: the default socket a same-host
    daemon left, or the one-line refusal that names both ways to
    configure a daemon (#555)."""
    found = find_default_socket()
    if found is not None:
        return unix_url(str(found))
    raise SystemExit(
        "msks: "
        + UNCONFIGURED_MESSAGE.format(
            candidates=", ".join(str(path) for path in socket_candidates())
        )
    )


def env_token() -> str | None:
    """The bearer token, or None on the local listener.

    The local shape authenticates by peer credential (#555): no
    token exists to read. A TCP daemon still requires one — from
    ``MSKSC_TOKEN`` or the config file's ``token_file``.
    """
    url = env_url()
    if is_unix(url):
        return None
    token = os.environ.get("MSKSC_TOKEN", "")
    if not token:
        raise SystemExit(
            "msks: set MSKSC_TOKEN to a daemon token, or point "
            "token_file at a token file in the client config "
            "(~/.config/msks/msks.yaml; MSKSC_URL for a non-default "
            "daemon)"
        )
    return token


def ssl_context() -> ssl.SSLContext:
    """Verify against MSKSC_CAFILE when set; otherwise TOFU-blind v1.

    The daemon's certificate is self-signed; pinning it with
    MSKSC_CAFILE gives verification, and without it the client
    proceeds unverified with a warning to stderr. A leading ``~``
    expands — the variable and the config file's ``cafile`` key
    carry the same home-relative paths the state roots do. The
    local listener (#555) never builds a context: the kernel
    vouches for both ends of a unix socket.
    """
    cafile = os.path.expanduser(os.environ.get("MSKSC_CAFILE", ""))
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    print(
        "msks: MSKSC_CAFILE not set; the daemon certificate is NOT verified",
        file=sys.stderr,
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
