"""The probe service (#424): an emulated external HTTPS service.

The daemon serves one deliberately ordinary HTTPS endpoint that a
workspace operator uses to verify secret interception end to end.
It behaves exactly like an external basic-auth-protected service:
real TLS on port 443 from its own CA, a fixed username and
password, and a minimal page. Nothing about it is special to the
interception machinery — the request reaches it the way any
allowlisted request does, through the redirect, the splice tier,
the leaf mint, and the sentinel→secret swap, and the service
itself only ever sees a well-formed basic-auth credential.

The verification recipe (docs/networking.md): mint a placeholder
whose allowlist carries the probe host and whose **secret is the
base64 of the whole credential** — ``base64("msks:msks")`` — then
from the workspace send the sentinel as the raw Basic blob::

    curl -H "Authorization: Basic <sentinel>" https://secretprobe.msks/

The swap rewrites the blob into the credential, the service
validates it like any external service would, and ``ok`` is the
answer only when every link worked. The credentials are fixed by
design — a probe credential shared by every deployment, not a
secret (the endpoint's whole power is the ``ok`` page it already
gave). Code scanning reads a credential-shaped literal either
way, so each line naming the value carries the inline suppression
for the hardcoded-credentials query, and this module says why.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
from cryptography import x509
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from ..llm import TapListener
from ..spec.probe import (
    PROBE_HOST,
    PROBE_PASSWORD,
    PROBE_PORT,
    PROBE_USERNAME,
)
from . import ca

logger = logging.getLogger(__name__)

#: The probe CA's state directory: ``<state_dir>/probe``.
PROBE_DIR = "probe"

#: The service leaf's files inside the probe directory.
LEAF_CERT_FILE = "service.crt"
LEAF_KEY_FILE = "service.key"

#: The upstream trust bundle mitmproxy loads (#424): setting the
#: option REPLACES the default lookup, so the file carries the
#: probe CA appended to the platform roots — verification holds
#: for the daemon's own service and every real public-CA service
#: alike.
BUNDLE_FILE = "upstream-bundle.pem"

#: The service leaf's validity window and the freshness line: a
#: leaf with less than this left is reminted at the next listener.
LEAF_DAYS = 30
LEAF_REMINT_S = 7 * 24 * 3600


def probe_dir(settings) -> Path:
    """The probe material's directory under the daemon state."""
    return settings.vmm.state_dir / PROBE_DIR


def leaf_fresh(cert_path: Path) -> bool:
    """Whether the on-disk service leaf still has runway."""
    if not cert_path.exists():
        return False
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.not_valid_after_utc - datetime.now(UTC) > timedelta(
        seconds=LEAF_REMINT_S
    )


def service_material(settings) -> ca.Authority:
    """The probe CA and a fresh service leaf, as file paths.

    Loads or mints the service's own CA (the same ``load_or_mint``
    the daemon-wide interceptor CA uses, in this service's own
    directory — a separate identity from the interception path's),
    then mints the service leaf when it is missing or close to
    expiry. The leaf is shared by
    every tap's listener: one service, one certificate, the way a
    real external deployment serves one name.
    """
    directory = probe_dir(settings)
    authority = ca.load_or_mint(directory)
    cert_path = directory / LEAF_CERT_FILE
    key_path = directory / LEAF_KEY_FILE
    if leaf_fresh(cert_path):
        return authority
    key, cert = ca.mint_leaf(authority, PROBE_HOST, days=LEAF_DAYS)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(ca.key_pem(key))
    cert_path.write_bytes(ca.cert_pem(cert))
    return authority


def parse_basic_auth(header: str) -> tuple[str, str] | None:
    """The Authorization header's basic pair, or None.

    The header names the Basic scheme and carries base64 of
    ``username:password``; the FIRST colon bounds the username, so
    a password may hold colons of its own. Anything else — another
    scheme, a missing pair, undecodable base64, a pair with no
    colon — is no credential at all and answers 401, never an
    error page.
    """
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode(
            "utf-8", "replace"
        )
    except ValueError:
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


def credentials_match(username: str, password: str) -> bool:
    """Constant-time equality on both halves.

    The encoded forms compare, because HTTP header bytes are
    latin-1-decoded and a digest comparison refuses non-ASCII
    strings — a crafted header must answer 401, never a 500. Both
    halves always compare (``&``, never ``and``'s short circuit):
    the halves' timing stays flat whichever mismatched. The
    values are the fixed probe credential (#424), which is why
    the comparison lines carry the hardcoded-credentials
    suppression.
    """
    # lgtm[py/hardcoded-credentials]
    user_ok = hmac.compare_digest(
        username.encode("utf-8", "replace"), PROBE_USERNAME.encode()
    )
    # lgtm[py/hardcoded-credentials]
    pass_ok = hmac.compare_digest(
        password.encode("utf-8", "replace"), PROBE_PASSWORD.encode()
    )
    return user_ok & pass_ok


def unauthorized() -> Response:
    """The named 401: a basic-auth challenge, nothing else."""
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="msks"'},
        content="invalid credentials",
        media_type="text/plain",
    )


def upstream_bundle(settings) -> str:
    """The trust bundle's path, written atomically (#424): the
    probe CA appended to certifi's platform bundle. mitmproxy's
    ``ssl_verify_upstream_trusted_ca`` replaces its default
    lookup (certifi, when the option is unset) with exactly the
    named file — a bundle holding the probe CA alone would break
    verification for every real public-CA service, so the file
    carries both.
    """
    authority = service_material(settings)
    path = probe_dir(settings) / BUNDLE_FILE
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_bytes(
        ca.cert_pem(authority.cert) + Path(certifi.where()).read_bytes()
    )
    scratch.replace(path)
    return str(path)


def build_probe_app() -> FastAPI:
    """The probe surface: one GET route, the auth check, the page."""

    async def ok(request: Request) -> Response:
        """The service's whole behavior (#424): a valid basic-auth
        credential answers ``ok``, everything else answers the
        challenge — exactly what an external basic-auth service
        does."""
        presented = parse_basic_auth(request.headers.get("authorization", ""))
        if presented is None or not credentials_match(*presented):
            return unauthorized()
        return PlainTextResponse("ok")

    app = FastAPI()
    app.router.add_api_route("/", ok, methods=["GET"])
    return app


class ProbeService:
    """The probe subsystem on ``app.state.probe`` (#424).

    Owns the shared service app, the probe CA and leaf (minted on
    the first listener), and the per-tap listener registry. The
    net manager asks for a listener at attach and stops it at
    detach; the listener is the same per-tap uvicorn wrapper the
    LLM proxy serves on (:class:`TapListener` in :mod:`msks.llm`),
    here serving TLS on the port the redirect preserves.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.probe_app = build_probe_app()
        self._listeners: dict[str, TapListener] = {}
        self._lock: asyncio.Lock | None = None
        self._material: tuple[str, str] | None = None
        self._bundle: str | None = None

    async def material(self) -> tuple[str, str]:
        """``(cert_path, key_path)`` for the TLS listeners, minted
        once and shared by every tap."""
        if self._material is not None:
            return self._material
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._material is None:
                settings = self.app.state.settings
                await asyncio.to_thread(service_material, settings)
                directory = probe_dir(settings)
                self._material = (
                    str(directory / LEAF_CERT_FILE),
                    str(directory / LEAF_KEY_FILE),
                )
        return self._material

    async def upstream_trust_bundle(self) -> str:
        """The upstream trust bundle's path, minted once through
        the same serialized path as the listener material (#424
        review: two unsynchronized entry points could interleave
        the cert/key writes into a mismatched pair)."""
        if self._bundle is not None:
            return self._bundle
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._bundle is None:
                settings = self.app.state.settings
                self._bundle = await asyncio.to_thread(
                    upstream_bundle, settings
                )
        return self._bundle

    def listener_for(self, attachment, certfile: str, keyfile: str):
        """The attachment's listener: its tap address on 443, the
        service leaf's TLS — one service shape on every tap, the
        device pin included (#483): the probe serves its own tap's
        redirected traffic, and the pin holds the same cross-tap
        degraded path it holds for the proxy. The loopback half is
        the pin's and the proxy's story alone: the base table's
        guard names the proxy port, so a host process reaching the
        probe over lo still connects — the probe's own fixed public
        credential is its designed gate, unchanged here."""
        return TapListener(
            self.probe_app,
            tap=attachment.tap,
            tap_ip=attachment.tap_ip,
            port=PROBE_PORT,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )

    async def start_listener(
        self, workspace_id: str, listener: TapListener
    ) -> None:
        """Bind one attachment's listener and record it; a bind
        that fails un-registers first (a stale registry entry
        would name a listener that never served)."""
        self._listeners[workspace_id] = listener
        try:
            await listener.start()
        except BaseException:
            self.stop_mapping(workspace_id, listener)
            raise

    def stop_mapping(self, workspace_id: str, listener: TapListener) -> None:
        """Forget one listener's registry entry when it is the one
        recorded (a replaced listener keeps its successor's
        entry)."""
        if self._listeners.get(workspace_id) is listener:
            del self._listeners[workspace_id]

    async def stop_listener(self, workspace_id: str) -> None:
        """Stop and forget one attachment's listener (idempotent)."""
        listener = self._listeners.pop(workspace_id, None)
        if listener is None:
            return
        await listener.stop()
