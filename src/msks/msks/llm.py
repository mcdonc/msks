"""The workspace LLM proxy (#259): one router, one credential per tap.

msksd holds the provider credentials and the routing configuration;
every workspace consumes LLMs through an OpenAI-shaped proxy served
on its own tap — the third host-reachable port beside DHCP (67) and
the resolver (53). A workspace holds no provider key and needs no
provider egress: the daemon makes the upstream connection, so a
workspace uses LLMs with its egress consent untouched.

Two routing modes, both ported from klangk's in-process router
(klangk #2070/#2072):

- **multi-provider** — a ``litellm.Router`` over a configured model
  list, entries spelled ``provider/model:api_base:api_key``;
- **passthrough** — one single-upstream entry whose model name is
  ``*``: requests forward verbatim over httpx and ``/models`` is
  discovered dynamically from the upstream.

Secret-bearing values (each entry's key, the default key) resolve
``file:`` and ``cmd:`` indirection at configure time, so provider
credentials stay out of the config file and the process
environment. ``litellm`` is imported lazily — its ~5 s import tax
stays off every path that never configures a model list.

Settings are read live: the router rebuilds itself when the settings
object it sees changes identity (the SIGHUP swap), so a model-list
edit applies without a restart wherever a listener already serves.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hmac
import json
import logging
import secrets
import socket
import subprocess
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

if TYPE_CHECKING:
    from litellm import Router

logger = logging.getLogger(__name__)

#: The port the per-tap listeners bind (a setting; this is the
#: default). Unprivileged by choice: the daemon's two low-port
#: services are DHCP and DNS, and the proxy has no reason to join
#: them.
DEFAULT_PORT = 8770

#: The prefix a minted workspace token carries (#259): the charset
#: behind it is token_urlsafe's (URL-safe base64), safe inside the
#: seed script's single-quoted assignments and the Authorization
#: header.
TOKEN_PREFIX = "msksllm1_"

# Provider defaults: well-known providers whose api_base can be
# omitted from an entry (ported from klangk).
PROVIDER_DEFAULTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "cohere": "https://api.cohere.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together_ai": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks_ai": "https://api.fireworks.ai/inference/v1",
}

# Strong references for fire-and-forget client-close tasks (ported
# from klangk #2928): an unreferenced task suspended in an await is
# GC-eligible mid-execution, which would leave the replaced
# passthrough client's connections unclosed.
_aclose_tasks: set[asyncio.Task] = set()


def finish_aclose_task(task: asyncio.Task) -> None:
    """Done callback for :func:`spawn_aclose`: drop the strong
    reference and surface a failed close as a logged error."""
    _aclose_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("LLM passthrough client close failed", exc_info=exc)


def spawn_aclose(client: httpx.AsyncClient) -> None:
    """Schedule *client*'s ``aclose()`` holding a strong reference.

    No-op outside a running event loop (a worker thread — the
    configure ran under ``to_thread``): the caller drains the
    retired list on the serving loop instead, where this schedules
    for real. Process exit closes the sockets anyway.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(client.aclose())
    _aclose_tasks.add(task)
    task.add_done_callback(finish_aclose_task)


# --- secret indirection ---------------------------------------------------


def resolve_indirection(value: str, name: str) -> str:
    """Resolve ``file:``/``cmd:`` prefixes on one secret-bearing value.

    ``file:`` reads the remainder as a path; ``cmd:`` runs it through
    the shell; anything else returns as-is. A resolution failure is a
    named error (the *name* of the value — an entry or the default
    key — never the value itself), so a broken reference fails the
    configure step loudly instead of sending an empty key upstream.
    """
    if value.startswith("file:"):
        try:
            return read_secret_file(value[len("file:") :])
        except OSError as exc:
            raise ValueError(
                f"{name}: cannot read {value[:5]} reference ({exc.strerror})"
            ) from None
    if value.startswith("cmd:"):
        return resolve_cmd_ref(value[len("cmd:") :], name)
    return value


def read_secret_file(path: str) -> str:
    """The file's contents, stripped; the path stays out of errors
    (it may name a secret)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def resolve_cmd_ref(command: str, name: str) -> str:
    """The command's stripped stdout; a nonzero exit is a named
    error carrying the command's stderr head, never the value."""
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ValueError(f"{name}: cmd: reference exited {proc.returncode}")
    return proc.stdout.strip()


# --- model-entry parsing (ported from klangk) -----------------------------


# The secret-indirection markers: an entry whose key half starts
# with one of these keeps everything from the marker (a path or
# command may itself contain colons).
INDIRECT_MARKERS = ("file:", "cmd:")


def find_marker(rest: str) -> int:
    """The latest indirection marker in *rest*, -1 when none."""
    best = -1
    for marker in INDIRECT_MARKERS:
        idx = rest.rfind(marker)
        if idx > best:
            best = idx
    return best


def split_entry(entry: str) -> tuple[str, str, str]:
    """``(litellm_model, api_base, api_key)``: the **first** colon
    bounds the model, the rest splits three ways — an indirection
    marker (``file:``/``cmd:``) bounds the key so a path or command
    may carry colons; otherwise the colon that bounds the key is
    the last one in the base's **path** portion (a scheme-bearing
    base's authority may carry a port, and an authority with no
    path names no key at all), or the last colon overall when the
    base carries no scheme."""
    first_colon = entry.find(":")
    if first_colon == -1:
        return entry, "", ""
    litellm_model = entry[:first_colon]
    rest = entry[first_colon + 1 :]
    marker = find_marker(rest)
    if marker > 0:
        return litellm_model, rest[: marker - 1], rest[marker:]
    if marker == 0:
        return litellm_model, "", rest
    sep = key_separator(rest)
    if sep == -1:
        return litellm_model, rest, ""
    return litellm_model, rest[:sep], rest[sep + 1 :]


def key_separator(rest: str) -> int:
    """The index of the colon that bounds the key in *rest*, -1
    when the base takes it all: with a scheme, only a colon in the
    path portion (at or after the authority's closing slash)
    separates a key — the authority's own colon is a port; without
    one, the last colon does."""
    last = rest.rfind(":")
    scheme = rest.find("://")
    if scheme == -1:
        return last
    path_start = rest.find("/", scheme + 3)
    if path_start == -1 or last < path_start:
        return -1
    return last


def resolve_api_base(api_base: str, litellm_model: str) -> str:
    """A known provider's default base URL when none was given."""
    if "/" not in litellm_model:
        return api_base
    provider = litellm_model.split("/", 1)[0]
    if api_base or provider not in PROVIDER_DEFAULTS:
        return api_base
    return PROVIDER_DEFAULTS[provider]


def parse_model_entry(entry: str) -> dict:
    """Parse one ``provider/model:api_base:api_key`` string into a
    litellm ``model_list`` entry. Secret-bearing halves resolve
    indirection here, so the built list never carries a ``file:``
    or ``cmd:`` spelling."""
    litellm_model, api_base, api_key = split_entry(entry)
    if api_key:
        api_key = resolve_indirection(api_key, f"model entry {entry!r}")
    api_base = resolve_api_base(api_base, litellm_model)
    model_name = (
        litellm_model.split("/", 1)[1]
        if "/" in litellm_model
        else litellm_model
    )
    params: dict[str, Any] = {"model": litellm_model}
    if api_base:
        params["api_base"] = api_base
    if api_key:
        params["api_key"] = api_key
    return {"model_name": model_name, "litellm_params": params}


# litellm_params keys whose values may carry secrets and so
# resolve file:/cmd: indirection inside dict entries too.
INDIRECT_KEYS = frozenset({"api_key", "api_base"})


def normalize_key(key: str) -> str:
    """Kebab-case to snake_case (the file's dict entries accept
    both spellings, klangk's shape)."""
    return key.replace("-", "_")


def normalize_params(params: dict) -> dict:
    """One ``litellm_params`` block: kebab→snake keys, indirection
    resolved on the secret-bearing values."""
    normalized = {}
    for key, value in params.items():
        name = normalize_key(key)
        if name in INDIRECT_KEYS and isinstance(value, str):
            value = resolve_indirection(value, name) or ""
        normalized[name] = value
    return normalized


def normalize_dict_entry(entry: dict) -> dict:
    """One LiteLLM-native dict entry: kebab- or snake-case keys at
    the top level (``model-name``/``model_name``), ``params`` as the
    ``litellm_params`` shorthand, and the params block normalized
    (ported from klangk — the file's list form carries these). The
    copy is deep: the Router owns its model_list outright, and a
    configure retry must see the entry exactly as the file wrote
    it — depth-2 sub-objects included."""
    return _normalized_entry(copy.deepcopy(entry))


def _normalized_entry(entry: dict) -> dict:
    """The normalization walk over a shallow copy of *entry*."""
    normalized = {}
    for key, value in entry.items():
        name = normalize_key(key)
        if name == "params":
            name = "litellm_params"
        if name == "litellm_params" and isinstance(value, dict):
            normalized[name] = normalize_params(value)
        else:
            normalized[name] = value
    return normalized


def build_model_list(
    entries: tuple[str | dict, ...], default_api_key: str
) -> list[dict]:
    """The litellm model_list from the settings' entries — strings
    parsed, dicts normalized; the default key fills every entry that
    named none (``setdefault`` attaches the block, so an entry that
    arrived without one still takes the key — klangk's original
    wrote it into a detached dict and lost it)."""
    items = []
    for entry in entries:
        if isinstance(entry, dict):
            parsed = normalize_dict_entry(entry)
        else:
            parsed = parse_model_entry(entry)
        params = parsed.setdefault("litellm_params", {})
        if default_api_key and "api_key" not in params:
            params["api_key"] = default_api_key
        items.append(parsed)
    return items


def is_passthrough(model_list: list[dict]) -> bool:
    """True when the config is a single wildcard entry: exactly one
    entry whose ``model_name`` is ``*`` — not a name that merely
    contains one."""
    if len(model_list) != 1:
        return False
    return model_list[0].get("model_name", "") == "*"


# --- the router (ported from klangk, adapted to live settings) -------------


class LlmRouter:
    """The routing half: a litellm Router or a passthrough client.

    Built from the live settings; ``ensure(settings)`` rebuilds when
    the settings object changed identity (the SIGHUP swap — every
    caller passes ``app.state.settings``, so the swap propagates
    without a reconfigure hook). Reads only the ``llm`` group."""

    def __init__(self) -> None:
        self._settings = None
        self._router: Router | None = None
        self._passthrough_base: str | None = None
        self._passthrough_key: str = ""
        self._http_client: httpx.AsyncClient | None = None
        # The test seam for the passthrough client: a factory the
        # suite replaces with a MockTransport-backed client.
        self.client_factory = httpx.AsyncClient
        # Reconfigure serialization: concurrent first-requests after
        # one SIGHUP swap share a single configure.
        self._lock: asyncio.Lock | None = None
        # Clients a configure replaced, awaiting their close on the
        # serving loop: ``configure`` may run in a worker thread
        # (where no loop exists to schedule on), so it retires here
        # and the ensure paths drain — the sync one best-effort, the
        # async one on the loop that owns the client's connections.
        self._retired: list[httpx.AsyncClient] = []

    def ensure(self, settings) -> None:
        """Rebuild when the settings object changed; a no-op while
        it did not (the per-request call on the proxy paths). The
        heavy halves of a rebuild — the lazily-imported litellm tree,
        a ``cmd:`` secret's subprocess — run in :func:`ensure_async`
        off the loop; the sync form serves sync callers only and
        stays off the request path. The settings latch lands only
        after a successful configure — a broken model entry raises,
        and the next request retries instead of serving the stale
        router under a latched identity."""
        if settings is self._settings:
            return
        self.configure(settings)
        self._settings = settings
        self.drain_retired()

    def drain_retired(self) -> None:
        """Hand every retired client to :func:`spawn_aclose` — on
        the caller's loop when one exists (the serving loop for the
        async path), a no-op otherwise (the sync path outside a
        loop; the next drain on a loop closes them)."""
        while self._retired:
            spawn_aclose(self._retired.pop(0))

    async def ensure_async(self, settings) -> None:
        """The request path's ensure: one reconfigure at a time, off
        the event loop — a ~5 s litellm import or a 30 s ``cmd:``
        must not stall DHCP, DNS, and every other workspace's
        traffic."""
        if settings is self._settings:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if settings is self._settings:
                return
            await asyncio.to_thread(self.configure, settings)
            self._settings = settings
            self.drain_retired()

    def configure(self, settings) -> None:
        """Build the routing state from one settings object."""
        llm = settings.llm
        if not llm.models:
            self.clear()
            return
        model_list = build_model_list(llm.models, self.default_key(settings))
        # Retire any previous passthrough client before switching
        # (the close is scheduled by the ensure path that owns a
        # loop — a worker thread here has none).
        if self._http_client is not None:
            self._retired.append(self._http_client)
            self._http_client = None
        if is_passthrough(model_list):
            params = model_list[0]["litellm_params"]
            self._passthrough_base = params.get("api_base", "")
            self._passthrough_key = params.get("api_key", "")
            if not self._passthrough_base:
                raise ValueError(
                    "the passthrough entry names no api_base — the "
                    "upstream URL is the entry's whole meaning"
                )
            self._router = None
            self._http_client = self.client_factory(timeout=300)
            logger.info(
                "llm router: passthrough mode -> %s", self._passthrough_base
            )
        else:
            self._passthrough_base = None
            self._passthrough_key = ""
            self._router = self.build_litellm_router(model_list)

    def default_key(self, settings) -> str:
        """The default key, indirection-resolved; empty when unset."""
        raw = settings.llm.api_key
        return resolve_indirection(raw, "the default api key") if raw else ""

    def build_litellm_router(self, model_list: list[dict]):
        """Construct the litellm Router (the module's lazy import —
        its ~5 s tax stays off paths that never configure models)."""
        from litellm import Router  # allow-deferred-import (module top)

        return Router(
            model_list=model_list,
            routing_strategy="simple-shuffle",
            num_retries=2,
        )

    def clear(self) -> None:
        """The unconfigured state: no router, no base, no client."""
        self._router = None
        self._passthrough_base = None
        self._passthrough_key = ""
        if self._http_client is not None:
            self._retired.append(self._http_client)
            self._http_client = None

    @property
    def active(self) -> bool:
        """Whether a model list is configured."""
        return self._router is not None or self._passthrough_base is not None

    @property
    def passthrough(self) -> bool:
        """Whether passthrough mode is in effect."""
        return self._passthrough_base is not None

    def resolve_router_model(self, kwargs: dict) -> None:
        """Default ``model`` to the first configured model when the
        request's is empty or matches no configured name."""
        model = kwargs.get("model", "")
        names = self.get_model_names()
        if not names:
            raise RuntimeError("LLM router has no models configured")
        if not model or model not in names:
            kwargs["model"] = names[0]

    async def acompletion(self, **kwargs: Any) -> Any:
        """One completion: passthrough-forwarded, or routed through
        litellm with the default-model resolution."""
        if self._passthrough_base is not None:
            return await self.passthrough_completion(**kwargs)
        if self._router is None:
            raise RuntimeError("LLM router not configured")
        self.resolve_router_model(kwargs)
        return await self._router.acompletion(**kwargs)

    def passthrough_headers(self) -> dict[str, str]:
        """Headers for passthrough requests."""
        headers = {"Content-Type": "application/json"}
        if self._passthrough_key:
            headers["Authorization"] = f"Bearer {self._passthrough_key}"
        return headers

    async def passthrough_completion(self, **kwargs: Any) -> dict:
        """Forward a non-streaming completion to the upstream."""
        assert self._http_client is not None
        resp = await self._http_client.post(
            f"{self._passthrough_base}/chat/completions",
            json=kwargs,
            headers=self.passthrough_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def passthrough_completion_stream(
        self, body: dict[str, Any]
    ) -> httpx.Response:
        """Forward a streaming completion; the caller streams the
        response and owns its close."""
        assert self._http_client is not None
        resp = await self._http_client.send(
            self._http_client.build_request(
                "POST",
                f"{self._passthrough_base}/chat/completions",
                json=body,
                headers=self.passthrough_headers(),
            ),
            stream=True,
        )
        if resp.is_error:
            # Release the streamed response's pooled connection
            # before raising (ported from klangk): the exception
            # keeps the response referenced, so nobody downstream
            # closes it.
            await resp.aclose()
            resp.raise_for_status()
        return resp

    async def list_upstream_models(self) -> list[dict[str, Any]]:
        """The model list: the upstream's own in passthrough mode
        (dynamic discovery), the configured names otherwise."""
        if self._passthrough_base is not None:
            assert self._http_client is not None
            try:
                resp = await self._http_client.get(
                    f"{self._passthrough_base}/models",
                    headers=self.passthrough_headers(),
                )
                resp.raise_for_status()
                return resp.json().get("data", [])
            except Exception:
                logger.exception(
                    "failed to query upstream models at %s",
                    self._passthrough_base,
                )
                return []
        return [
            {"id": name, "object": "model", "owned_by": "msksd"}
            for name in self.get_model_names()
        ]

    def get_model_names(self) -> list[str]:
        """The configured logical model names (router mode)."""
        if self._router is None:
            return []
        return list(self._router.get_model_names())


# --- the per-tap proxy -----------------------------------------------------


def mint_token() -> str:
    """A fresh workspace credential (#259): the prefix plus one
    URL-safe secret."""
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


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


def token_matches(presented: str, stored: str | None) -> bool:
    """Constant-time equality on the encoded forms; no stored token
    authenticates nothing. Bytes, because HTTP header bytes are
    latin-1-decoded and the digest comparison refuses non-ASCII
    strings — a crafted header must answer 401, never a 500."""
    return stored is not None and hmac.compare_digest(
        presented.encode("utf-8", "replace"), stored.encode("utf-8")
    )


class TapListener:
    """One tap's proxy listener: a uvicorn server bound to the tap's
    own address, serving the shared proxy app.

    The bind IS the scoping: the address exists only on that tap's
    /30, and the per-VM input chain admits exactly this port from
    exactly that tap. The socket binds eagerly (a taken port raises
    where the manager can tolerate it, not inside the serve task),
    and signal handlers stay the process's own — an embedded
    server has no business owning SIGTERM."""

    def __init__(self, app, *, tap_ip: str, port: int) -> None:
        self._app = app
        self.tap_ip = tap_ip
        self.port = port
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None

    def bind(self) -> None:
        """Bind the listening socket synchronously: a refused bind
        (another daemon on the port) raises here, in the caller's
        exception frame."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.tap_ip, self.port))
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
            log_level="warning",
            access_log=False,
            lifespan="off",
            # A bound on the graceful shutdown: an in-flight proxied
            # request gets this long to finish after the stop ask —
            # explicit because uvicorn's default (no bound) is
            # internal behavior, not a contract a daemon teardown
            # should lean on.
            timeout_graceful_shutdown=5,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
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
        """The proxy's one gate: the workspace's own token.

        Rejects anonymous requests and every other credential alike
        (a daemon API bearer token is not a workspace credential):
        the proxy is usable only from inside a workspace, by the
        workspace. The workspace is the one whose tap served the
        request, and the token is its row's — the listener's bind
        already scoped the connection to that tap."""
        workspace_id = self.workspace_for_request(request)
        presented = None if workspace_id is None else self.bearer_of(request)
        if presented is None:
            raise HTTPException(status_code=401, detail="no workspace")
        row = await self.app.state.model.get_llm_token(workspace_id)
        if row is None or not token_matches(presented, row.get("llm_token")):
            raise HTTPException(
                status_code=401, detail="invalid workspace token"
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
    """The OpenAI-shaped proxy app: ``/v1/models`` and
    ``/v1/chat/completions``, workspace-token gated."""

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

    app = FastAPI()
    app.router.add_api_route("/v1/models", list_models, methods=["GET"])
    app.router.add_api_route(
        "/v1/chat/completions", chat_completions, methods=["POST"]
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
