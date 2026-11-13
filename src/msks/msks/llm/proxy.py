"""The per-tap proxy: one OpenAI-shaped API per workspace.

A :class:`TapListener` binds the workspace's tap and serves the
OpenAI pair plus the Anthropic Messages route through
:class:`LlmProxy`; request bodies are capped and allowlisted before
they reach the router.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .anthropic import (
    anthropic_api_error,
    anthropic_to_openai,
    events_from_message,
    openai_to_anthropic,
    render_anthropic_events,
    sse_line,
)
from .router import LlmRouter

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

#: The port the per-tap listeners bind (a setting; this is the
#: default). Unprivileged by choice: the daemon's two low-port
#: services are DHCP and DNS, and the proxy has no reason to join
#: them.
DEFAULT_PORT = 8770

# The chat-completion request fields the proxy forwards (#259
# review): litellm's Router treats call-site ``api_base``/``api_key``
# as dynamic overrides of the deployment's credentials, so a guest
# body naming them would redirect the daemon's upstream and ride
# the provider key out. The allowlist is the whole defense: fields
# an OpenAI chat-completion request may carry, nothing more, in
# both routing modes — unknown fields drop (a newer client field
# the list lacks costs that field, never the credential).
CHAT_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "n",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "response_format",
        "seed",
        "stop",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning_effort",
        "user",
        "metadata",
    }
)


def chat_fields(body: dict) -> dict:
    """The request body's forwardable subset — the allowlist applied."""
    return {k: v for k, v in body.items() if k in CHAT_FIELDS}


def bind_to_device(sock: socket.socket, tap: str) -> None:
    """Pin a socket to one interface (#483): the kernel delivers
    packets to it only from that tap. Linux-only by shape — the
    option constant is absent elsewhere, and the daemon's taps are
    Linux's. No capability is required on kernel 5.7+ (the option's
    CAP_NET_RAW check went away there); the pair the daemon's
    privilege contract names (CAP_NET_ADMIN, CAP_NET_BIND_SERVICE)
    stays exact. A tap name no interface carries answers ENODEV,
    so a mis-built spec fails at the bind, not in the field."""

    sock.setsockopt(
        socket.SOL_SOCKET, socket.SO_BINDTODEVICE, tap.encode() + b"\0"
    )


class TapListener:
    """One tap's proxy listener: a uvicorn server bound to the tap's
    own address, serving the shared proxy app.

    The bind IS the scoping (#483), in two cooperating halves:
    the address exists only on that tap's /30, and the socket is
    bound to the tap device itself (``SO_BINDTODEVICE``) — the
    kernel delivers a packet to it only when the packet arrived
    on that tap. A host process connecting through ``lo``, and a
    packet another guest sends over its own tap toward this
    address, die at the socket even when every firewall table is
    missing — the device pin survives nft failure modes
    (`absent_ok` applies) that the per-VM input chain does not.
    The per-VM chain still admits exactly this port from exactly
    that tap ahead of the socket (defense in depth, and the
    enforcement of every other tap service). The socket binds
    eagerly (a taken port raises where the manager can tolerate
    it, not inside the serve task), and signal handlers stay the
    process's own — an embedded server has no business owning
    SIGTERM."""

    def __init__(
        self,
        app,
        *,
        tap: str,
        tap_ip: str,
        port: int,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
    ) -> None:
        self._app = app
        self.tap = tap
        self.tap_ip = tap_ip
        self.port = port
        self._ssl_certfile = ssl_certfile
        self._ssl_keyfile = ssl_keyfile
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None

    def bind(self) -> None:
        """Bind the listening socket synchronously: a refused bind
        (another daemon on the port) raises here, in the caller's
        exception frame. The device pin (#483) needs no privilege
        on kernel 5.7+ (SO_BINDTODEVICE shed its CAP_NET_RAW
        requirement in v5.7); a pre-5.7 kernel answers EPERM and a
        mistyped tap name ENODEV — both surface as this workspace's
        LLM absence through the manager's best-effort catch, the
        same posture a taken port carries."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.tap_ip, self.port))
        # An ephemeral ask (``port=0``) binds a kernel-chosen
        # number — record it, so the served port is readable off
        # the listener (the probe dial repoint reads it off the
        # running listener's registry entry, #532).
        self.port = sock.getsockname()[1]
        bind_to_device(sock, self.tap)
        sock.listen(128)
        sock.setblocking(False)
        self._sock = sock

    async def start(self) -> None:
        """Bind if needed, then serve; the task is gathered by
        :meth:`stop`."""
        import uvicorn  # allow-deferred-import (startup path only)

        self.bind_if_needed()
        config = uvicorn.Config(
            self._app,
            host=self.tap_ip,
            port=self.port,
            # No log_level and no log_config: uvicorn's loggers are
            # process-global by name, and a Config's load() applies
            # its own level to the shared "uvicorn.error" logger —
            # an embedded "warning" would then silence the outer
            # server's INFO records for the rest of the process
            # life (its "Application shutdown complete." among
            # them: the shutdown itself runs, its proof vanishes).
            # The embedded server stays quiet by shape — lifespan
            # off, access log off, sockets handed in — so it has
            # nothing to configure globally.
            log_config=None,
            log_level=None,
            access_log=False,
            lifespan="off",
            # TLS material when the listener serves HTTPS (the probe
            # service, #424); None serves plain HTTP (the LLM proxy).
            ssl_certfile=self._ssl_certfile,
            ssl_keyfile=self._ssl_keyfile,
            # A bound on the graceful shutdown: an in-flight proxied
            # request gets this long to finish after the stop ask —
            # explicit because uvicorn's default (no bound) is
            # internal behavior, not a contract a daemon teardown
            # should lean on.
            timeout_graceful_shutdown=5,
        )
        server = uvicorn.Server(config)
        # An embedded server has no business owning the process's
        # signals — and in this uvicorn line, serve() arms them
        # unconditionally: capture_signals installs (and on exit
        # restores) process-wide SIGTERM/SIGINT handlers, swapping
        # whatever the outer server armed. A tap listener stopping
        # mid-teardown then leaves a dead server's handler in the
        # slot: a later SIGTERM sets a stopped server's flag while
        # the live one never learns, and the daemon dies by the
        # signal with no graceful shutdown — the shape the daemon
        # e2e started failing with once the probe listener (#424)
        # put a second embedded server on a tap. Neutralized
        # per-instance; the process's signals stay the outer
        # server's own.
        server.capture_signals = contextlib.nullcontext
        self._server = server
        self._task = asyncio.create_task(server.serve(sockets=[self._sock]))

    def bind_if_needed(self) -> None:
        """The bind start() owes when the caller did not pre-bind
        (the eager path that surfaces a taken port in the manager's
        tolerant frame)."""
        if self._sock is None:
            self.bind()

    async def stop(self) -> None:
        """Ask the server to exit and gather its task (idempotent).
        The socket closes last and double-safe: uvicorn's own
        shutdown closes the servers it was handed."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        self._server = None
        self._task = None


class LlmProxy:
    """The proxy subsystem on ``app.state.llm``.

    Owns the shared proxy app (one for every tap — the request's
    local address says which workspace's tap served it), the router,
    and the per-tap listener registry. The net manager asks for a
    listener at attach; the tap-to-workspace map answers the auth
    path."""

    def __init__(self, app) -> None:
        self.app = app
        self.router = LlmRouter()
        self.proxy_app = build_proxy_app(self)
        self._listeners: dict[str, TapListener] = {}
        self._by_tap_ip: dict[str, str] = {}

    def listener_for(self, attachment) -> TapListener | None:
        """The attachment's listener, or None when no model list is
        configured (an unconfigured daemon presents no LLM surface:
        nothing binds, and the input chain admits nothing). Reads
        the settings only — the router configures lazily at the
        first request, so a broken entry never blocks the bind
        decision here."""
        settings = self.app.state.settings
        if not settings.llm.models:
            return None
        listener = TapListener(
            self.proxy_app,
            tap=attachment.tap,
            tap_ip=attachment.tap_ip,
            port=settings.llm.port,
        )
        return listener

    async def start_listener(
        self, workspace_id: str, listener: TapListener
    ) -> None:
        """Bind one attachment's listener and record its mapping;
        a bind that fails un-registers first (a stale mapping would
        authenticate the next holder of the address)."""
        self._listeners[workspace_id] = listener
        self._by_tap_ip[listener.tap_ip] = workspace_id
        try:
            await listener.start()
        except BaseException:
            self.stop_mapping(workspace_id, listener)
            raise

    def stop_mapping(self, workspace_id: str, listener: TapListener) -> None:
        """Forget one listener's registries (shared by stop and the
        failed-start unwind)."""
        if self._listeners.get(workspace_id) is listener:
            del self._listeners[workspace_id]
        if self._by_tap_ip.get(listener.tap_ip) == workspace_id:
            del self._by_tap_ip[listener.tap_ip]

    async def stop_listener(self, workspace_id: str) -> None:
        """Stop and forget one attachment's listener (idempotent)."""
        listener = self._listeners.pop(workspace_id, None)
        if listener is None:
            return
        self.stop_mapping(workspace_id, listener)
        await listener.stop()

    def workspace_for_request(self, request: Request) -> str | None:
        """The workspace whose tap served *request* — by the local
        address the listener is bound to."""
        server = request.scope.get("server")
        if not server:
            return None
        return self._by_tap_ip.get(server[0])

    def bearer_of(self, request: Request) -> str | None:
        """The request's bearer credential, None when the header
        names no bearer scheme."""
        scheme, _, presented = request.headers.get(
            "authorization", ""
        ).partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return None
        return presented

    async def authorize(self, request: Request) -> None:
        """The proxy's one gate (#483): the tap that served the
        request. The workspace is the one whose listener answered,
        and nothing else is consulted — the proxy holds no
        credential to present, because every process inside the
        workspace could read one anyway (the seed's own exports).

        One credential class stays distinct: a presented bearer the
        daemon's own token table accepts is a daemon API token,
        and the proxy refuses it — the API surface's credential
        must never double as a proxy credential, so an API token
        stolen off a client config grants nothing on this port.
        Any other value (the placeholder the seed exports, a
        vendor-shaped key, garbage) passes: the tap already named
        the workspace, and the header carries no authority here.
        The refusal is a liveness oracle — a workspace holding a
        stolen API token can ask "is it still valid?" here without
        touching the monitored API surface — accepted for the class
        separation: a bearer the daemon accepts must never ride a
        second surface, and the tokens are high-entropy enough that
        the oracle only answers holders, not guessers."""
        workspace_id = self.workspace_for_request(request)
        if workspace_id is None:
            raise HTTPException(status_code=401, detail="no workspace")
        presented = self.bearer_of(request)
        if presented is None:
            return
        if await self.app.state.model.token_valid(presented):
            raise HTTPException(
                status_code=401,
                detail=(
                    "a daemon API token is not a proxy credential; "
                    "the proxy serves the workspace whose tap "
                    "reached it, and needs no credential"
                ),
            )


# One completion body's ceiling: generous against a real prompt
# (a pasted book is a few MiB) and bounded against a guest that
# streams an unending document at the daemon's memory.
MAX_BODY_BYTES = 16 * 1024 * 1024

#: The read's oversized sentinel (an object identity, so a body
# that legitimately reads back as anything else is never confused).
OVERSIZED = object()


def body_refusal(request: Request) -> JSONResponse | None:
    """413 when the request announces more than the ceiling on
    Content-Length — the honest case refuses before a byte is
    read; the streamed read enforces the same bound for bodies
    that arrive without the header."""
    for value in request.headers.get("content-length", "").split(","):
        if value.strip().isdigit() and int(value) > MAX_BODY_BYTES:
            return too_large()
    return None


def too_large() -> JSONResponse:
    """The named 413 refusal."""
    return JSONResponse(
        status_code=413,
        content={"error": f"request body exceeds {MAX_BODY_BYTES} bytes"},
    )


async def read_capped_json(request: Request):
    """The request's parsed JSON body, or OVERSIZED past the
    ceiling; a decode failure raises (the caller answers 400)."""
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return OVERSIZED
        chunks.append(chunk)
    return json.loads(b"".join(chunks))


async def parse_body(request: Request) -> tuple:
    """(refusal, body): the named 413/400 answers, or (None, the
    parsed dict) — the whole request-body half of the completion
    route, split out so the stream-bounded path is testable
    without an HTTP client."""
    refusal = body_refusal(request)
    if refusal is not None:
        return refusal, None
    try:
        body = await read_capped_json(request)
    except Exception:
        return bad_json(), None
    if body is OVERSIZED:
        return too_large(), None
    if not isinstance(body, dict):
        return bad_json(), None
    return None, body


def bad_json() -> JSONResponse:
    """The named 400 for a body that is not a JSON object."""
    return JSONResponse(
        status_code=400,
        content={"error": "request body is not JSON"},
    )


async def configured_router(proxy: LlmProxy):
    """The proxy's router after a live ensure — or None with the
    route's answer ready: a configuration that cannot build (a
    dead ``file:``/``cmd:`` reference after a SIGHUP) answers a
    named 503, its detail logged server-side only (the configure
    error names the entry, which may carry an inline key — never
    guest-visible)."""
    router = proxy.router
    try:
        await router.ensure_async(proxy.app.state.settings)
    except Exception:
        logger.exception("LLM router configuration failed")
        return None, JSONResponse(
            status_code=503,
            content={"error": "LLM router configuration failed"},
        )
    if not router.active:
        return None, JSONResponse(
            status_code=503,
            content={"error": "LLM router not configured"},
        )
    return router, None


def build_proxy_app(proxy: LlmProxy) -> FastAPI:
    """The proxy app: the OpenAI pair ``/v1/models`` and
    ``/v1/chat/completions``, and the Anthropic Messages route
    ``/v1/messages`` (#765) — all workspace-token gated."""

    async def list_models(request: Request) -> dict:
        """The model list (OpenAI ``GET /v1/models`` shape); 503 in
        the unconfigured posture and on a failed configure, the
        same gates the completion route carries."""
        await proxy.authorize(request)
        router, failure = await configured_router(proxy)
        if failure is not None:
            return failure
        return {
            "object": "list",
            "data": await router.list_upstream_models(),
        }

    async def chat_completions(request: Request):
        """One completion (OpenAI ``POST /v1/chat/completions`` body):
        JSON or SSE by the request's ``stream`` flag; 503 when no
        model list is configured, 502 when the upstream fails. The
        body's forwardable fields are the allowlist's — credentials
        and endpoints a guest names never reach the router."""
        await proxy.authorize(request)
        refusal, body = await parse_body(request)
        if refusal is not None:
            return refusal
        router, failure = await configured_router(proxy)
        if failure is not None:
            return failure
        try:
            return await dispatch_completion(router, chat_fields(body))
        except Exception:
            logger.exception("LLM completion failed")
            return JSONResponse(
                status_code=502,
                content={"error": "LLM upstream request failed"},
            )

    async def messages(request: Request):
        """One Anthropic Messages request (``POST /v1/messages``
        body, #765): claude-code's route. Passthrough mode forwards
        the body verbatim to the upstream's own Messages endpoint;
        router mode translates to a completion and back. The auth,
        the body caps, and the configure gates are the ones the
        completion route carries; an upstream failure answers the
        Messages error shape a Messages client surfaces."""
        await proxy.authorize(request)
        refusal, body = await parse_body(request)
        if refusal is not None:
            return refusal
        router, failure = await configured_router(proxy)
        if failure is not None:
            return failure
        try:
            if router.passthrough:
                return await dispatch_anthropic_passthrough(
                    router,
                    body,
                    request.headers.get("anthropic-version"),
                    request.headers.get("anthropic-beta"),
                )
            return await dispatch_anthropic_routed(router, body)
        except Exception:
            logger.exception("LLM anthropic messages failed")
            return JSONResponse(
                status_code=502,
                content=anthropic_api_error("LLM upstream request failed"),
            )

    async def count_tokens(request: Request):
        """The Messages count-tokens probe (``POST
        /v1/messages/count_tokens``, #765): claude-code's context
        tracker asks it. Passthrough mode forwards it verbatim to
        the upstream's own endpoint; router mode answers 404 —
        the translated completion shape has no token counter, and
        a Messages client estimates locally when the endpoint
        answers nothing."""
        await proxy.authorize(request)
        refusal, body = await parse_body(request)
        if refusal is not None:
            return refusal
        router, failure = await configured_router(proxy)
        if failure is not None:
            return failure
        if not router.passthrough:
            return JSONResponse(
                status_code=404,
                content=anthropic_api_error(
                    "no token counter for a routed model list"
                ),
            )
        try:
            return JSONResponse(
                content=await router.passthrough_count_tokens(
                    body,
                    request.headers.get("anthropic-version"),
                    request.headers.get("anthropic-beta"),
                )
            )
        except Exception:
            logger.exception("LLM anthropic count_tokens failed")
            return JSONResponse(
                status_code=502,
                content=anthropic_api_error("LLM upstream request failed"),
            )

    app = FastAPI()
    app.router.add_api_route("/v1/models", list_models, methods=["GET"])
    app.router.add_api_route(
        "/v1/chat/completions", chat_completions, methods=["POST"]
    )
    app.router.add_api_route("/v1/messages", messages, methods=["POST"])
    app.router.add_api_route(
        "/v1/messages/count_tokens", count_tokens, methods=["POST"]
    )
    return app


async def dispatch_completion(router: LlmRouter, body: dict):
    """Route one completion body to its response flavor: passthrough
    SSE, litellm async-generator SSE, or plain JSON."""
    if body.get("stream", False) and router.passthrough:
        resp = await router.passthrough_completion_stream(body)
        return passthrough_stream_response(resp)
    response = await router.acompletion(**body)
    if hasattr(response, "__aiter__"):
        return await stream_litellm_response(response)
    return await json_response(response)


async def dispatch_anthropic_passthrough(
    router: LlmRouter, body: dict, version: str | None, beta: str | None
):
    """Route one Messages body in passthrough mode (#765): the
    body forwards verbatim, and a streaming answer's SSE lines
    come home as they arrived — the upstream serves the Messages
    wire shape itself."""
    if body.get("stream", False):
        resp = await router.passthrough_messages_stream(body, version, beta)
        return passthrough_stream_response(resp)
    return JSONResponse(
        content=await router.passthrough_messages(body, version, beta)
    )


async def dispatch_anthropic_routed(router: LlmRouter, body: dict):
    """Route one Messages body in router mode (#765): the body
    translates to completion kwargs, the router answers, and the
    answer translates back. A streaming request renders the
    OpenAI chunk stream as Anthropic events; an upstream that
    answered a streaming request without a stream still gets a
    complete event sequence — the client asked for events and
    events arrive."""
    kwargs = chat_fields(anthropic_to_openai(body))
    if kwargs.get("stream", False):
        # OpenAI-shaped upstreams omit chunk usage unless asked;
        # claude-code reads the output-token count the closing
        # ``message_delta`` carries. A provider without the
        # parameter drops it (the Router's ``drop_params``).
        kwargs["stream_options"] = {"include_usage": True}
    response = await router.acompletion(**kwargs)
    if kwargs.get("stream", False):
        if hasattr(response, "__aiter__"):
            return StreamingResponse(
                render_anthropic_events(response, chunk_data),
                media_type="text/event-stream",
            )
        return await anthropic_events_response(response)
    return JSONResponse(
        content=openai_to_anthropic(await chunk_data(response))
    )


async def anthropic_events_response(response) -> StreamingResponse:
    """A non-streamed answer rendered as the event sequence the
    request's ``stream`` flag promised (the single-shot path's
    answer to an upstream that ignores the flag)."""
    message = openai_to_anthropic(await chunk_data(response))

    async def render():
        for event in events_from_message(message):
            yield sse_line(event)

    return StreamingResponse(render(), media_type="text/event-stream")


def passthrough_stream_response(resp) -> StreamingResponse:
    """A StreamingResponse forwarding the upstream's SSE lines."""

    async def stream_passthrough():
        try:
            async for line in resp.aiter_lines():
                yield f"{line}\n"
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_passthrough(),
        media_type="text/event-stream",
    )


async def chunk_data(chunk):
    """One streamed chunk to its payload: a model_dump'd object, a
    dict, or the string fallback."""
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump()
    if isinstance(chunk, dict):
        return chunk
    return str(chunk)


async def stream_litellm_response(response) -> StreamingResponse:
    """A StreamingResponse rendering litellm's chunks in the OpenAI
    SSE shape, terminated with [DONE]."""

    async def stream_litellm():
        async for chunk in response:
            data = await chunk_data(chunk)
            yield f"data: {json.dumps(data)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_litellm(),
        media_type="text/event-stream",
    )


async def json_response(response) -> JSONResponse:
    """A non-streaming response as JSON: a model_dump'd object, a
    dict, or a mapping fallback."""
    if hasattr(response, "model_dump"):
        return JSONResponse(content=response.model_dump())
    if isinstance(response, dict):
        return JSONResponse(content=response)
    return JSONResponse(content=dict(response))
