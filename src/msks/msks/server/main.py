"""The msksd entry point: run the API server over TLS (#8)."""

import argparse
import sys
from pathlib import Path

import uvicorn

from .. import __version__
from ..app import build_app
from .api import build_api
from .tls import load_or_generate


def ssl_paths(app) -> tuple[str | None, str | None]:
    """Resolve listener TLS material; None/None means plain HTTP."""
    server = app.state.settings.server
    if server.tls_cert is None and server.tls_key is None:
        return None, None
    return server.tls_cert, server.tls_key


def server_config(app) -> uvicorn.Config:
    """The uvicorn config for one msks App (testable, not run here)."""
    server = app.state.settings.server
    cert, key = ssl_paths(app)
    ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key} if cert and key else {}
    return uvicorn.Config(
        build_api(app),
        host=server.host,
        port=server.port,
        log_level="info",
        **ssl_kwargs,
    )


def serve(app, no_tls: bool) -> None:
    """Resolve listener TLS material, then run the server forever.

    ``--no-tls`` wins over configured cert paths: an operator asking
    for plain HTTP in development must not get TLS because the
    environment still carries stale variables.
    """
    server = app.state.settings.server
    if no_tls:
        server.tls_cert = None
        server.tls_key = None
    else:
        state_dir = Path(server.db_path).parent
        cert, key, ca_fp = load_or_generate(
            state_dir, server.host, server.tls_cert, server.tls_key
        )
        server.tls_cert, server.tls_key = cert, key
        if ca_fp:
            print(
                f"msksd: CA fingerprint (pin on first connect): {ca_fp}",
                file=sys.stderr,
            )
    run_forever(app)


def run_forever(app) -> None:
    """Block on uvicorn — structurally untestable, one line."""
    uvicorn.Server(server_config(app)).run()  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    """Console-script entry: parse args, run the server."""
    parser = argparse.ArgumentParser(prog="msksd")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="serve plain HTTP (development only)",
    )
    args = parser.parse_args(argv)
    serve(build_app(), args.no_tls)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
