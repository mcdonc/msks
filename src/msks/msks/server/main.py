"""The msksd entry point: run the API server over TLS (#8)."""

import argparse
import signal
import sys
from pathlib import Path

import uvicorn

from .. import __version__
from ..app import build_app
from ..config import load_settings
from ..settings import Settings
from .api import build_api
from .reload import arm_reload_watcher
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
    ssl_kwargs = (
        {"ssl_certfile": cert, "ssl_keyfile": key} if cert and key else {}
    )
    return uvicorn.Config(
        build_api(app),
        host=server.host,
        port=server.port,
        log_level="info",
        access_log=server.access_log,
        **ssl_kwargs,
    )


def arm_tls(app, no_tls: bool) -> None:
    """Resolve listener TLS material into the live settings.

    ``--no-tls`` wins over configured cert paths: an operator asking
    for plain HTTP in development must not get TLS because the
    environment still carries stale variables. Split out of
    :func:`serve` so ``main`` can fail fast on unusable material —
    a half-configured operator pair raises here with the clean
    one-line error instead of a traceback from inside uvicorn.
    """
    server = app.state.settings.server
    if no_tls:
        server.tls_cert = None
        server.tls_key = None
        return
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


def serve(app, no_tls: bool) -> None:
    """Resolve listener TLS material, then run the server forever."""
    arm_tls(app, no_tls)
    run_forever(app)


def run_forever(app) -> None:
    """Block on uvicorn — structurally untestable, one line."""
    uvicorn.Server(server_config(app)).run()  # pragma: no cover


def reload_settings(app, config: str | None) -> None:
    """SIGHUP action: re-read the config file into the live settings.

    Subsystems read settings off ``app.state.settings`` at call time,
    so the swap propagates without per-module ``reconfigure()``
    calls. The listener (host, port, TLS material) and the database
    path are bound at startup and keep their startup values until a
    restart. A config that fails to load or validate — including a
    default-path file deleted since startup, which is refused rather
    than regenerated (a reload is not a first run) — leaves the
    previous settings in force. A second SIGHUP arriving mid-reload
    re-enters and finishes too; each swap is one attribute
    assignment, so the last writer wins with no torn state.
    """
    try:
        settings = load_settings(config, generate=False)
    except Exception as exc:  # any operator config mistake, foreseen or not
        print(
            f"msksd: SIGHUP reload refused, keeping current settings: {exc}",
            file=sys.stderr,
        )
        return
    keep_startup_bound(app.state.settings, settings)
    app.state.settings = settings
    # A provider or root swap invalidates every cached value: the
    # next read re-fetches from the new settings' store instead of
    # serving the old provider's bytes until a restart.
    app.state.secrets.cache_clear()


def keep_startup_bound(old: Settings, new: Settings) -> None:
    """Latch startup-bound settings across a SIGHUP reload.

    The listener runs on the TLS pair it booted with (``serve``
    resolves unset paths to the generated CA pair and writes them
    back). The database engine is open on its path. The local driver
    resolves every workspace's artifact directory live from
    ``vmm.state_dir`` — a reload that moved it would report each
    running workspace absent, orphan its VMM, and point deletes at
    the new tree. Each of these keeps its startup value until a
    restart; a reload naming a new one changes nothing.
    """
    if new.server.tls_cert is None:
        new.server.tls_cert = old.server.tls_cert
    if new.server.tls_key is None:
        new.server.tls_key = old.server.tls_key
    new.vmm.state_dir = old.vmm.state_dir
    new.server.db_path = old.server.db_path
    # The secret store's location is startup-bound the same way: a
    # root or provider swap would point reads at a store whose
    # manifest and values were never migrated (connection details —
    # region, profile, identity path — reload live, the way
    # dns_upstream does).
    new.secret_store.provider = old.secret_store.provider
    new.secret_store.root = old.secret_store.root


def install_sighup_reload(app, config: str | None) -> None:
    """Wire SIGHUP to :func:`reload_settings` (main thread only)."""
    signal.signal(
        signal.SIGHUP, lambda signum, frame: reload_settings(app, config)
    )


def main(argv: list[str] | None = None) -> int:
    """Console-script entry: parse args, run the server."""
    parser = argparse.ArgumentParser(prog="msksd")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="serve plain HTTP (development only)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help=(
            "development: watch the msks package source tree and "
            "restart the process when it changes (used by the "
            "appliance dev-tree flow, #144)"
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "YAML config file to read (env vars still override it); "
            "'none' reads env vars and defaults only; default: "
            "$MSKSD_CONFIG_DIR/msksd.yaml, generated on first run"
        ),
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        app = build_app(settings)
        install_sighup_reload(app, args.config)
        arm_tls(app, args.no_tls)
    except Exception as exc:  # config + TLS errors read as one line
        print(f"msksd: {exc}", file=sys.stderr)
        return 2
    if args.reload:
        arm_reload_watcher()
    serve(app, args.no_tls)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
