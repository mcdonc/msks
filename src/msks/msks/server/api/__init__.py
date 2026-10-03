"""The /api/v1 surface: versioned, token-authenticated, one
listener (#8).

The package composes per-resource routers (#417): every module
under ``msks.server.api`` owns one surface — tokens, secrets,
workspaces, volumes, images, the console and forward bridges, the
event stream, egress consent — and :func:`build_api` mounts them
on one FastAPI application in registration order. Routes stay
thin: they parse, call the model layer or the microvm seam, and
shape responses. No business logic lives here, and no database
query is built outside ``msks.model``.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi import __version__ as fastapi_version
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ...microvm.errors import MicrovmError
from ...spec.version import __version__
from ..watcher import watch_loop
from . import (
    console,
    egress,
    events,
    forward,
    images,
    secrets,
    tokens,
    volumes,
    workspaces,
)
from .images import bootstrap_default_image

LOG = logging.getLogger(__name__)


#: The daemon's boot cmdline — a deployment names the
#: image it booted on it (``msksd.image=<store path>``), and
#: :func:`cmdline_image` reports that in ``/health`` so a client
#: can name drift between a running daemon and the tree's
#: current build (#160).
CMDLINE = Path("/proc/cmdline")


def cmdline_image() -> str | None:
    """The image this daemon booted from, when it says.

    A bare ``msksd`` carries no ``msksd.image`` pair
    and reports ``None``; clients treat that as "unknown" and skip
    the drift comparison.
    """
    with contextlib.suppress(OSError):
        for pair in CMDLINE.read_text().split():
            if pair.startswith("msksd.image="):
                return pair.split("=", 1)[1]
    return None


async def microvm_error(_request, exc: MicrovmError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def redacted_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """A validation refusal without the offending input (#423).

    FastAPI's default handler echoes pydantic's ``input`` — the
    raw field value — back in the response body, and a refused
    mint body can carry a whole secret in it. The answer keeps
    the type, the location, and the message, drops the input (and
    the request's url): the caller already holds what it sent,
    and nothing between should learn it.
    """
    errors = [
        {key: error[key] for key in ("type", "loc", "msg") if key in error}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


def build_api(app) -> FastAPI:
    """The FastAPI application bound to one msks App.

    The hub lives on ``app.state`` (#69) — the consent engine
    publishes verdict frames to it before the api exists. The
    resource routers mount in registration order (#417); the
    catalog lock serializes image imports and renames daemon-wide
    (#258, #340).
    """
    hub = app.state.hub
    app.state.create_locks: dict[str, asyncio.Lock] = {}
    # Catalog mutations serialize daemon-wide (#258, #340): the
    # floor check under concurrent URL imports would each clamp to
    # the same headroom and stream real bytes in parallel — the
    # lock keeps the ceiling's promise that the disk never dips
    # below the floor mid-download. Path imports ride the same
    # lock: their floor check has the same race, just a narrower
    # window. Renames join it (#340 review): an import swaps the
    # whole per-hash cache aside, so an unlocked rename landing in
    # that window would write its override into the cache the
    # import then deletes — a committed rename silently lost.
    catalog_lock = asyncio.Lock()
    app.state.home_locks: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        watcher: asyncio.Task | None = None
        try:
            app.state.model.migrate()
            await app.state.model.bootstrap_token()
            # The #335 ref rename: rewrite legacy placeholder rows,
            # their stored values, and the manifest before anything
            # serves or mints — the store lock is unneeded while the
            # watcher and API are not yet up.
            moved = await app.state.secrets.migrate_legacy_refs()
            if moved:
                LOG.info(
                    "secret store: migrated %d placeholder(s) onto "
                    "MSKSWS_ backend refs",
                    moved,
                )
            bootstrap_default_image(app)
            # The probe placeholder (#424): seeded before any
            # workspace can attach, so every boot arms the probe's
            # interception path with zero operator minting.
            await secrets.seed_probe_placeholder(app)
            await app.state.net.start()
            await app.state.consent.start()
            watcher = asyncio.create_task(watch_loop(app, hub))
            api.state.watcher = watcher
            yield
        finally:
            # Close the model's engine so pooled sqlite connections
            # close deterministically — on shutdown and on a failed
            # startup step (migrate/bootstrap can have created the
            # engine before raising). The watcher teardown is its own
            # try so an unexpected watcher error cannot skip the close.
            try:
                if watcher is not None:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher
            finally:
                await app.state.consent.stop()
                await app.state.net.stop()
                # After the net detach: every armed listener's
                # teardown ran through it; what remains is the
                # master itself (#199).
                await app.state.interceptor.stop()
                await app.state.model.close()

    api = FastAPI(title="msksd", version=__version__, lifespan=lifespan)
    api.state.msks_app = app
    api.state.hub = hub

    @api.get("/api/v1/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "fastapi": fastapi_version,
            "image": cmdline_image(),
        }

    api.add_exception_handler(MicrovmError, microvm_error)
    api.add_exception_handler(
        RequestValidationError, redacted_validation_error
    )
    for sub_router in (
        tokens.router(app),
        secrets.router(app, hub),
        workspaces.router(app),
        images.router(app, catalog_lock),
        volumes.router(app, hub),
        console.router(app),
        forward.router(app, hub),
        events.router(app, hub),
        egress.router(app),
    ):
        api.include_router(sub_router)
    return api
