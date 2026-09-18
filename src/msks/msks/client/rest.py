"""Shared client-side plumbing: env, TLS, and the REST calls.

Both the CLI subcommands (:mod:`msks.client.cli`) and the interactive
console (:mod:`msks.client.console`) speak the daemon's REST surface
through this module, so the #21 client conventions live here once:
``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for a bearer token,
``MSKSC_CAFILE`` to pin the certificate.
"""

import asyncio
import os
import ssl
import sys
import time

import httpx

DEFAULT_URL = "https://127.0.0.1:8660"

# Generous read budget: ``POST .../start`` answers only after the
# VMM boot completes (~3s p50, slower on a loaded host), and the
# default 5s would cut a healthy launch off mid-flight.
TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

# The byte window streamed bodies move in (#80): matches the
# daemon's import window, and a megabyte is small enough that flow
# control stays responsive on slow links.
STREAM_WINDOW_B = 1024 * 1024

# Waiting out another client's in-flight boot: the same budget the
# start call itself gets.
BOOT_WAIT_S = 120.0
BOOT_POLL_S = 1.0


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
    ssl_ctx: ssl.SSLContext | None = None,
) -> httpx.AsyncClient:
    """The authenticated client for one command.

    ``transport`` is the seam the tests plug an in-process API (or a
    mock) into (an ``ssl_ctx`` is unused alongside one); the real
    client dials ``url`` with the #21 TLS story (``ssl_ctx`` lets a
    caller reuse an already-built context, so the unverified-mode
    warning prints once per invocation).
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
        verify=ssl_ctx if ssl_ctx is not None else ssl_context(),
    )


def timeout_message(client: httpx.AsyncClient, exc: Exception) -> str:
    """The timeout line: the daemon may still finish the request."""
    return (
        f"msks: timed out talking to {client.base_url} "
        f"(the daemon may still finish the request): {exc}"
    )


def reach_message(client: httpx.AsyncClient, exc: Exception) -> str:
    """The dial-failure line."""
    return f"msks: cannot reach {client.base_url}: {exc}"


def status_message(exc: httpx.HTTPStatusError) -> str:
    """The API-status line: the daemon's detail, verbatim."""
    return f"msks: {exc.response.status_code}: {error_detail(exc.response)}"


async def guarded(client: httpx.AsyncClient, exchange):
    """Await one exchange with :func:`request`'s error contract.

    The JSON calls and the #80 byte streams share this map: a
    timeout names the daemon and the possibility it still finishes,
    a dead dial names the daemon, and an HTTP status carries the
    daemon's detail verbatim.
    """
    try:
        return await exchange()
    except httpx.TimeoutException as exc:
        raise SystemExit(timeout_message(client, exc)) from exc
    except httpx.TransportError as exc:
        raise SystemExit(reach_message(client, exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise SystemExit(status_message(exc)) from exc


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    json_body: dict | None = None,
):
    """One request on ``client``; failures exit with one readable line."""

    async def exchange():
        response = await client.request(method, path, json=json_body)
        response.raise_for_status()
        return response

    return (await guarded(client, exchange)).json()


async def api_call(
    method: str,
    url: str,
    token: str,
    path: str,
    json_body: dict | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    ssl_ctx: ssl.SSLContext | None = None,
):
    """One authenticated REST call on a fresh client."""
    async with api_client(url, token, transport, ssl_ctx) as client:
        return await request(client, method, path, json_body)


async def download(client: httpx.AsyncClient, path: str, sink) -> int:
    """Stream one GET body to ``sink.write``; returns the byte count.

    The body never buffers whole — a home volume runs to gigabytes
    (#80).
    """
    total = 0

    async def exchange():
        nonlocal total
        async with client.stream("GET", path) as response:
            if response.is_error:
                # The body holds the error detail; read it before the
                # raise so the one-line message can quote it.
                await response.aread()
                response.raise_for_status()
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                sink.write(chunk)
        return total

    return await guarded(client, exchange)


async def upload(client: httpx.AsyncClient, path: str, content) -> dict:
    """Stream one request body (an async byte iterator) with the
    octet-stream type; the parsed JSON reply."""

    async def exchange():
        response = await client.request(
            "PUT",
            path,
            content=content,
            headers={"content-type": "application/octet-stream"},
        )
        response.raise_for_status()
        return response

    return (await guarded(client, exchange)).json()


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
    return (
        "; ".join(validation_message(item) for item in items)
        or "invalid request"
    )


def validation_message(item) -> str:
    """One FastAPI validation error as ``loc: msg`` text."""
    loc = ".".join(str(part) for part in item.get("loc", []))
    msg = item.get("msg", "invalid")
    return f"{loc}: {msg}" if loc else msg


async def ensure_running(
    workspace_id: str,
    url: str,
    token: str,
    ssl_ctx: ssl.SSLContext | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Boot ``workspace_id`` when the daemon reports it not running.

    A concurrent boot is waited out; a paused workspace is refused
    (the daemon has no resume); a start that loses a race to another
    boot attaches to the winner instead of failing. The notices
    print to stderr before the caller enters raw tty mode, where a
    plain newline would leave the cursor mid-column.
    """
    row = await workspace_row(workspace_id, url, token, ssl_ctx, transport)
    if row["status"] == "starting":
        print(
            f"msks: {workspace_id} is starting; waiting for the boot",
            file=sys.stderr,
        )
        row = await wait_boot(workspace_id, url, token, ssl_ctx, transport)
    if row["status"] == "running":
        return
    if row["status"] == "paused":
        raise SystemExit(paused_advice(workspace_id))
    print(
        f"msks: {workspace_id} is {row['status']}; starting it",
        file=sys.stderr,
    )
    await boot_workspace(workspace_id, url, token, ssl_ctx, transport)
    print(f"msks: {workspace_id} running", file=sys.stderr)


async def workspace_row(
    workspace_id: str,
    url: str,
    token: str,
    ssl_ctx: ssl.SSLContext | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """The workspace row; a missing id exits with the API's 404 line."""
    return await api_call(
        "GET",
        url,
        token,
        f"/api/v1/workspaces/{workspace_id}",
        ssl_ctx=ssl_ctx,
        transport=transport,
    )


async def wait_boot(
    workspace_id: str,
    url: str,
    token: str,
    ssl_ctx: ssl.SSLContext | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Poll another client's in-flight boot until it settles."""
    deadline = time.monotonic() + BOOT_WAIT_S
    while time.monotonic() < deadline:
        row = await workspace_row(workspace_id, url, token, ssl_ctx, transport)
        if row["status"] != "starting":
            return row
        await asyncio.sleep(BOOT_POLL_S)
    raise SystemExit(
        f"msks: {workspace_id} still starting after {BOOT_WAIT_S}s"
    )


async def boot_workspace(
    workspace_id: str,
    url: str,
    token: str,
    ssl_ctx: ssl.SSLContext | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """POST start; a start that lost a race attaches to the winner.

    A 503 from a double launch can mean another client booted it
    between our GET and our POST — re-check and swallow the error
    only when the workspace is in fact running now.
    """
    try:
        await api_call(
            "POST",
            url,
            token,
            f"/api/v1/workspaces/{workspace_id}/start",
            ssl_ctx=ssl_ctx,
            transport=transport,
        )
    except SystemExit as exc:
        row = await workspace_row(workspace_id, url, token, ssl_ctx, transport)
        if row["status"] != "running":
            raise SystemExit(
                f"{exc}\nmsks: {workspace_id} is {row['status']}"
            ) from exc


def paused_advice(workspace_id: str) -> str:
    """The honest refusal for a paused workspace."""
    return (
        f"msks: {workspace_id} is paused and the daemon has no resume; "
        f"stop it with: msks stop {workspace_id}, "
        "then msks start again"
    )


async def fetch_ssh_key(
    url: str,
    token: str,
    workspace_id: str,
    transport=None,
    ssl_ctx=None,
) -> dict:
    """GET the workspace's identity: type, public half, private half
    (null for a client-minted workspace, #121 — the daemon never
    held it).

    ``msks key`` prints it; ``msks ssh`` (#112) serves the private
    half from a transient in-process agent through this same call.
    An already-built ``ssl_ctx`` avoids a second unverified-mode
    warning in one command.
    """
    return await api_call(
        "GET",
        url,
        token,
        f"/api/v1/workspaces/{workspace_id}/ssh-key",
        transport=transport,
        ssl_ctx=ssl_ctx,
    )
