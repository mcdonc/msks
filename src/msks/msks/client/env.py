"""The client's connection settings: env readers, defaults, and TLS.

The #21 client conventions live here once — ``MSKSC_URL`` for the
daemon, ``MSKSC_TOKEN`` for a bearer token, ``MSKSC_CAFILE`` to pin
the certificate — as a leaf with no transport imports (#402):
modules that only need *where* to connect (the config file layer,
the TUI) import this module, while the REST transport
(:mod:`msks.client.rest`) keeps the calls. The YAML file layer
(:mod:`msks.client.config`) materializes its winners into the
environment these readers read.
"""

import os
import ssl
import sys

DEFAULT_URL = "https://127.0.0.1:8660"


def env_url() -> str:
    # Empty string is the unset form (the file layer and the shell
    # presets both lean on it): fall to the default, never to a
    # base URL of "".
    return (os.environ.get("MSKSC_URL", "") or DEFAULT_URL).rstrip("/")


def env_token() -> str:
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
    carry the same home-relative paths the state roots do.
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
