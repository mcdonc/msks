"""Shared client-side plumbing: env, TLS, and the REST calls.

Both the CLI subcommands (:mod:`msks.client.cli`) and the interactive
shell (:mod:`msks.client.shell`) speak the daemon's REST surface
through this module, so the #21 client conventions live here once:
``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for a bearer token,
``MSKSC_CAFILE`` to pin the certificate.
"""

import os
import ssl
import sys

import httpx

DEFAULT_URL = "https://127.0.0.1:8660"

# Generous read budget: ``POST .../start`` answers only after the
# VMM boot completes (~3s p50, slower on a loaded host), and the
# default 5s would cut a healthy launch off mid-flight.
TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)


def env_url() -> str:
    return os.environ.get("MSKSC_URL", DEFAULT_URL).rstrip("/")


def env_token() -> str:
    token = os.environ.get("MSKSC_TOKEN", "")
    if not token:
        raise SystemExit(
            "msks: set MSKSC_TOKEN to a daemon token "
            "(MSKSC_URL for a non-default daemon)"
        )
    return token


def ssl_context() -> ssl.SSLContext:
    """Verify against MSKSC_CAFILE when set; otherwise TOFU-blind v1.

    The daemon's certificate is self-signed; pinning it with
    MSKSC_CAFILE gives verification, and without it the client
    proceeds unverified with a warning to stderr.
    """
    cafile = os.environ.get("MSKSC_CAFILE", "")
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


def api_client(
    url: str,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
    ssl: ssl.SSLContext | None = None,
) -> httpx.AsyncClient:
    """The authenticated client for one command.

    ``transport`` is the seam the tests plug an in-process API (or a
    mock) into; the real client dials ``url`` with the #21 TLS story
    (``ssl`` lets a caller reuse an already-built context, so the
    unverified-mode warning prints once per invocation).
    """
    if transport is not None:
        return httpx.AsyncClient(
            base_url=url,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
    return httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
        verify=ssl if ssl is not None else ssl_context(),
    )


async def request(
    client: httpx.AsyncClient, method: str, path: str, json_body: dict | None = None
):
    """One request on ``client``; failures exit with one readable line."""
    try:
        response = await client.request(method, path, json=json_body)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise SystemExit(
            f"msks: timed out talking to {client.base_url} "
            f"(the daemon may still finish the request): {exc}"
        ) from exc
    except httpx.TransportError as exc:
        raise SystemExit(f"msks: cannot reach {client.base_url}: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise SystemExit(
            f"msks: {exc.response.status_code}: {error_detail(exc.response)}"
        ) from exc
    return response.json()


async def api_call(
    method: str,
    url: str,
    token: str,
    path: str,
    json_body: dict | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    ssl: ssl.SSLContext | None = None,
):
    """One authenticated REST call on a fresh client."""
    async with api_client(url, token, transport, ssl) as client:
        return await request(client, method, path, json_body)


def error_detail(response: httpx.Response) -> str:
    """The API's ``detail`` field; validation lists become one line."""
    try:
        detail = response.json()["detail"]
    except ValueError, KeyError, TypeError:
        return response.text.strip() or "no detail"
    if isinstance(detail, list):
        return validation_detail(detail)
    return str(detail)


def validation_detail(items: list) -> str:
    """FastAPI's validation error list joined into one line."""
    return "; ".join(validation_message(item) for item in items) or "invalid request"


def validation_message(item) -> str:
    """One FastAPI validation error as ``loc: msg`` text."""
    loc = ".".join(str(part) for part in item.get("loc", []))
    msg = item.get("msg", "invalid")
    return f"{loc}: {msg}" if loc else msg


async def ensure_running(
    workspace_id: str,
    url: str,
    token: str,
    ssl: ssl.SSLContext | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Boot ``workspace_id`` when the daemon reports it not running.

    The notices print to stderr before the caller enters raw tty
    mode, where a plain newline would leave the cursor mid-column.
    """
    row = await api_call(
        "GET",
        url,
        token,
        f"/api/v1/workspaces/{workspace_id}",
        ssl=ssl,
        transport=transport,
    )
    if row["status"] == "running":
        return
    print(f"msks: {workspace_id} is {row['status']}; starting it", file=sys.stderr)
    await api_call(
        "POST",
        url,
        token,
        f"/api/v1/workspaces/{workspace_id}/start",
        ssl=ssl,
        transport=transport,
    )
    print(f"msks: {workspace_id} running", file=sys.stderr)
