"""The workspace LLM proxy (#259): one router, one proxy per tap.

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

The app serves the OpenAI pair (``/v1/models`` and
``/v1/chat/completions``) and the Anthropic Messages route
(``/v1/messages``, #765) claude-code and its kin speak: verbatim
forward in passthrough mode, translation through the Anthropic
section below in router mode.

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
import json
import logging
import socket
import subprocess
from collections.abc import AsyncIterator
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
        import litellm  # allow-deferred-import (module top)

        # litellm's async handlers default to an aiohttp transport
        # (an aiohttp ClientSession wrapped as an httpx transport).
        # The daemon's completions ride plain httpx instead — the
        # same client the passthrough mode already uses — so the
        # closure's aiohttp (the pinned nixpkgs serves it below
        # litellm's own floor; nix/litellm-pkg.nix records why)
        # never carries a request: it stays an import-time
        # requirement of litellm's metadata, nothing more.
        litellm.disable_aiohttp_transport = True
        return litellm.Router(
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

    def anthropic_headers(
        self, version: str | None, beta: str | None
    ) -> dict[str, str]:
        """Headers for a passthrough Messages request (#765): the
        entry's key as ``x-api-key`` (the Messages wire's own
        credential header), the version the request carried or
        the default, and a beta list the request carried — the
        beta features a Messages client asked for stay on for the
        upstream to honor. Nothing else the guest sent forwards:
        a built header set is the passthrough half of the field
        allowlist's defense."""
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": version or DEFAULT_ANTHROPIC_VERSION,
        }
        if beta:
            headers["anthropic-beta"] = beta
        if self._passthrough_key:
            headers["x-api-key"] = self._passthrough_key
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

    async def passthrough_messages(
        self, body: dict, version: str | None, beta: str | None
    ) -> dict:
        """Forward one non-streaming Messages request (#765)
        verbatim: the body the guest sent is the body the
        upstream reads — the Messages API is the upstream's own
        shape, so no field walk applies."""
        assert self._http_client is not None
        resp = await self._http_client.post(
            f"{self._passthrough_base}/messages",
            json=body,
            headers=self.anthropic_headers(version, beta),
        )
        resp.raise_for_status()
        return resp.json()

    async def passthrough_messages_stream(
        self, body: dict, version: str | None, beta: str | None
    ) -> httpx.Response:
        """Forward one streaming Messages request (#765); the
        caller streams the response and owns its close."""
        assert self._http_client is not None
        resp = await self._http_client.send(
            self._http_client.build_request(
                "POST",
                f"{self._passthrough_base}/messages",
                json=body,
                headers=self.anthropic_headers(version, beta),
            ),
            stream=True,
        )
        if resp.is_error:
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


# --- the Anthropic Messages translation (#765) --------------------------


#: The Messages API version the route answers (and forwards in
#: passthrough mode) when the request carries none: 2023-06-01 is
#: the version every Messages client in the wild sends.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

#: The Messages request fields the route reads. The allowlist is
#: the whole defense on this shape too: a guest body naming
#: ``api_key``, ``api_base``, or any other routing parameter never
#: reaches the converter's output, exactly as the OpenAI route's
#: ``CHAT_FIELDS`` gate behaves. ``top_k`` is absent by choice: the
#: OpenAI completion body has no such field, and a forwarded one
#: would fail providers that reject unknown parameters.
ANTHROPIC_FIELDS = frozenset(
    {
        "model",
        "messages",
        "system",
        "max_tokens",
        "temperature",
        "top_p",
        "stop_sequences",
        "stream",
        "tools",
        "tool_choice",
        "metadata",
    }
)

#: The finish-reason vocabulary of each shape, one to the other:
#: OpenAI names three ends a Messages client distinguishes, and
#: ``stop_sequence`` has no OpenAI source (the proxy never injects
#: stop sequences), so it never appears as a conversion output.
FINISH_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
}


def anthropic_fields(body: dict) -> dict:
    """The request body's translatable subset — the allowlist
    applied."""
    return {k: v for k, v in body.items() if k in ANTHROPIC_FIELDS}


def block_texts(blocks: Any) -> list[str]:
    """A block list's text blocks as strings: the callers join
    them with the separator their shape uses."""
    if not isinstance(blocks, list):
        return []
    return [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]


def system_text(system: Any) -> str:
    """The system prompt as one string: the Messages API accepts a
    plain string or a list of text blocks, and the OpenAI shape
    wants the string."""
    if isinstance(system, str):
        return system
    return "\n\n".join(part for part in block_texts(system) if part)


def image_part(block: dict) -> dict:
    """One image block to the OpenAI data-URL part (the Messages
    shape carries base64 media; the OpenAI shape names a URL, and
    the data-URL spelling is the one every provider accepts)."""
    source = block.get("source", {})
    media_type = source.get("media_type", "application/octet-stream")
    data = source.get("data", "")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{data}"},
    }


def tool_call_part(block: dict) -> dict:
    """One ``tool_use`` block to an OpenAI tool-call item: the
    input dict serializes to the arguments string."""
    return {
        "type": "function",
        "id": block.get("id", ""),
        "function": {
            "name": block.get("name", ""),
            "arguments": json.dumps(block.get("input", {})),
        },
    }


def result_text(content: Any) -> str:
    """A ``tool_result`` block's content as the tool message's
    text: the Messages shape allows a string or a block list, and
    the text blocks join (images inside a result have no OpenAI
    tool-message shape and drop)."""
    if isinstance(content, str):
        return content
    return "\n".join(part for part in block_texts(content) if part)


def split_blocks(
    content: Any,
) -> tuple[list[str], list[dict], list[dict], list[dict]]:
    """A content block list's four bins: the texts, the content
    parts, the tool calls, and the tool results — in walk order."""
    texts: list[str] = []
    parts: list[dict] = []
    tool_calls: list[dict] = []
    results: list[dict] = []
    for block in content:
        if isinstance(block, dict):
            route_block(block, texts, parts, tool_calls, results)
    return texts, parts, tool_calls, results


def route_block(
    block: dict,
    texts: list[str],
    parts: list[dict],
    tool_calls: list[dict],
    results: list[dict],
) -> None:
    """One block into its bin, by kind; a thinking block carries
    no OpenAI shape and drops."""
    kind = block.get("type")
    if kind == "text":
        texts.append(block.get("text", ""))
        parts.append({"type": "text", "text": block.get("text", "")})
    elif kind == "image":
        parts.append(image_part(block))
    elif kind == "tool_use":
        tool_calls.append(tool_call_part(block))
    elif kind == "tool_result":
        results.append(
            {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": result_text(block.get("content")),
            }
        )


def convert_message(message: dict) -> list[dict]:
    """One Messages turn to its OpenAI messages, in order: a user
    turn's ``tool_result`` blocks become ``tool`` messages that
    come first (the OpenAI shape pins them right after the
    assistant's tool calls), the remaining blocks join as the
    turn's own content, and an assistant turn's ``tool_use``
    blocks become its ``tool_calls``. A turn that is only tool
    results contributes no message of its own."""
    role = message.get("role", "user")
    content = message.get("content", "")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    texts, parts, tool_calls, results = split_blocks(content)
    converted = list(results)
    if role == "assistant":
        converted.extend(assistant_message(texts, parts, tool_calls))
        return converted
    if parts:
        converted.append({"role": "user", "content": parts})
    return converted


def assistant_message(
    texts: list[str], parts: list[dict], tool_calls: list[dict]
) -> list[dict]:
    """An assistant turn's own message: tool calls ride as
    ``tool_calls`` with the text joined beside them, and a turn
    without tool calls keeps its text alone."""
    if tool_calls:
        return [
            {
                "role": "assistant",
                "content": "\n\n".join(texts) if texts else None,
                "tool_calls": tool_calls,
            }
        ]
    if parts or texts:
        return [{"role": "assistant", "content": "\n\n".join(texts)}]
    return []


def openai_tools(tools: Any) -> list[dict]:
    """The Messages tool list to the OpenAI function shape: the
    ``input_schema`` is the ``parameters``."""
    converted = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return converted


#: The ``tool_choice`` kinds that map to an OpenAI keyword:
#: ``any`` is the OpenAI ``required`` (a tool must run).
TOOL_CHOICE_KINDS = {"auto": "auto", "any": "required", "none": "none"}


def openai_tool_choice(choice: Any) -> Any:
    """The Messages ``tool_choice`` to the OpenAI one: ``auto``
    keeps its name, ``any`` becomes ``required`` (the OpenAI
    spelling of "a tool must run"), a named tool wraps in the
    function shape. A shape the map does not know drops — the
    upstream's default (auto) applies."""
    if not isinstance(choice, dict):
        return None
    if choice.get("type") in TOOL_CHOICE_KINDS:
        return TOOL_CHOICE_KINDS[choice["type"]]
    return named_tool_choice(choice)


def named_tool_choice(choice: dict) -> Any:
    """A named-tool choice to the OpenAI function shape; None for
    anything else."""
    if choice.get("type") == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def system_message(src: dict) -> dict | None:
    """The system prompt as an OpenAI system message; None when
    the body carried none (or carried one with no text)."""
    if "system" not in src:
        return None
    text = system_text(src["system"])
    if not text:
        return None
    return {"role": "system", "content": text}


def openai_messages(src: dict) -> list[dict]:
    """The Messages body's system and turns to OpenAI messages:
    the system prompt (a string or blocks) opens the list, and
    each turn converts in order. A malformed entry (a non-dict
    message) skips — a broken guest body costs that entry, never
    the request."""
    messages: list[dict] = []
    system = system_message(src)
    if system is not None:
        messages.append(system)
    for message in src.get("messages") or []:
        if isinstance(message, dict):
            messages.extend(convert_message(message))
    return messages


def metadata_user(src: dict) -> Any:
    """The metadata block's user id, None when absent."""
    return (src.get("metadata") or {}).get("user_id")


def openai_scalars(src: dict) -> dict:
    """The scalar fields that map name for name or to their
    renamed counterpart: the sampling knobs, the stream flag,
    the model, the token ceiling, the stop sequences, and the
    metadata's user id."""
    scalars: dict[str, Any] = {}
    for name in ("model", "max_tokens", "temperature", "top_p", "stream"):
        if name in src:
            scalars[name] = src[name]
    if "stop_sequences" in src:
        scalars["stop"] = src["stop_sequences"]
    user = metadata_user(src)
    if user:
        scalars["user"] = user
    return scalars


def anthropic_to_openai(body: dict) -> dict:
    """One Messages request body to OpenAI completion kwargs. The
    output carries only keys the OpenAI route's own allowlist
    names — the converter is the second gate behind
    :func:`anthropic_fields`, so a guest's routing parameters have
    two fields to survive and neither does."""
    src = anthropic_fields(body)
    kwargs: dict[str, Any] = openai_scalars(src)
    kwargs["messages"] = openai_messages(src)
    tools = openai_tools(src.get("tools"))
    if tools:
        kwargs["tools"] = tools
    choice = openai_tool_choice(src.get("tool_choice"))
    if choice is not None:
        kwargs["tool_choice"] = choice
    return kwargs


def stop_reason(finish: Any) -> str:
    """The OpenAI finish reason to the Messages ``stop_reason``."""
    return FINISH_REASONS.get(finish, "end_turn")


def tool_use_block(call: dict) -> dict:
    """One OpenAI tool-call item to a ``tool_use`` block; the
    arguments string parses back to the input dict, and an
    unparsable one (a provider that truncates mid-JSON) answers
    an empty input rather than an error."""
    function = call.get("function", {})
    try:
        input_obj = json.loads(function.get("arguments") or "{}")
    except ValueError:
        input_obj = {}
    return {
        "type": "tool_use",
        "id": call.get("id", ""),
        "name": function.get("name", ""),
        "input": input_obj,
    }


def is_text_part(part: Any) -> bool:
    """Whether one OpenAI content part is a text part."""
    return isinstance(part, dict) and part.get("type") == "text"


def string_block(content: str) -> list[dict]:
    """A string content as its text blocks: one block, or none
    for the empty string."""
    if content:
        return [{"type": "text", "text": content}]
    return []


def text_blocks(content: Any) -> list[dict]:
    """The text blocks of an OpenAI message's content: a string
    is one block (an empty one carries none), a parts list keeps
    its text parts, anything else carries none."""
    if isinstance(content, str):
        return string_block(content)
    if not isinstance(content, list):
        return []
    return [
        {"type": "text", "text": part.get("text", "")}
        for part in content
        if is_text_part(part)
    ]


def content_blocks(message: dict) -> list[dict]:
    """The OpenAI response message to Messages content blocks: the
    text (string or parts) becomes ``text`` blocks, the tool calls
    become ``tool_use`` blocks, and an empty answer keeps one
    empty ``text`` block — the Messages shape requires content."""
    blocks: list[dict] = list(text_blocks(message.get("content")))
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            blocks.append(tool_use_block(call))
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def openai_to_anthropic(response: dict) -> dict:
    """One OpenAI completion response to the Messages message. The
    first choice is the answer (the proxy serves one completion
    per request); usage maps field for field with zeros when the
    upstream named none."""
    choices = response.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    usage = response.get("usage") or {}
    return {
        "id": response.get("id", "msg_msks"),
        "type": "message",
        "role": "assistant",
        "model": response.get("model", ""),
        "content": content_blocks(message),
        "stop_reason": stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def sse_line(event: dict) -> str:
    """One event as Anthropic SSE: the ``event`` line the wire
    format carries beside the data, and the JSON body."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def message_start(model: str, input_tokens: int) -> dict:
    """The opening event: the message frame with empty content and
    the input usage (the Messages stream bills input here and
    output in ``message_delta``)."""
    return {
        "type": "message_start",
        "message": {
            "id": "msg_msks",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    }


def usage_input(usage: Any) -> int:
    """A usage block's prompt-token count, zero when the chunk
    carried none or carried a non-mapping."""
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("prompt_tokens") or 0)


class AnthropicStream:
    """One streamed response's conversion state: OpenAI chunks in,
    Anthropic events out.

    A chunk stream interleaves text deltas and tool-call argument
    fragments over unnamed positions, while the Messages event
    stream pins each block to an index the client tracks — so the
    converter owns the index counter, remembers which tool-call
    index maps to which block, and closes a block before the next
    one opens. :meth:`feed` takes each chunk, :meth:`finish`
    closes whatever the stream left open and ends the message.
    """

    def __init__(self) -> None:
        self.model = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop = "end_turn"
        self.next_index = 0
        self.text_index: int | None = None
        self.tool_indexes: dict[int, int] = {}
        self.started = False

    def start_events(self) -> list[dict]:
        """The events the first chunk owes: the message frame, then
        the ping a Messages client expects before content."""
        self.started = True
        return [message_start(self.model, self.input_tokens), {"type": "ping"}]

    def start_if_first(self, chunk: dict) -> list[dict]:
        """The first chunk's events: the model name and the input
        usage land on the converter before the frame renders."""
        if self.started:
            return []
        self.model = chunk.get("model", self.model)
        self.input_tokens = usage_input(chunk.get("usage"))
        return self.start_events()

    def feed(self, chunk: dict) -> list[dict]:
        """One OpenAI chunk to its events; the first chunk also
        carries the message frame, and a chunk with usage records
        it (OpenAI sends usage on the chunk that carries it, which
        is the last when the stream asked for it)."""
        events: list[dict] = self.start_if_first(chunk)
        events.extend(self.choices_events(chunk.get("choices") or []))
        self.record_usage(chunk.get("usage"))
        return events

    def choices_events(self, choices: Any) -> list[dict]:
        """Every choice's events, in order."""
        events: list[dict] = []
        for choice in choices or []:
            events.extend(self.choice_events(choice))
        return events

    def record_usage(self, usage: Any) -> None:
        """A chunk's usage onto the converter's counters; input
        lands in the frame already sent, output in the closing
        ``message_delta``."""
        if not isinstance(usage, dict):
            return
        if usage.get("prompt_tokens"):
            self.input_tokens = usage["prompt_tokens"]
        if usage.get("completion_tokens"):
            self.output_tokens = usage["completion_tokens"]

    def choice_events(self, choice: dict) -> list[dict]:
        """One choice's delta to its events: the finish reason
        records for ``message_delta``, then the delta's text and
        tool-call fragments convert."""
        if choice.get("finish_reason"):
            self.stop = stop_reason(choice["finish_reason"])
        return self.delta_events(choice.get("delta") or {})

    def delta_events(self, delta: dict) -> list[dict]:
        """One delta's text and tool calls to their events."""
        events: list[dict] = []
        text = delta.get("content")
        if isinstance(text, str) and text:
            events.extend(self.text_events(text))
        for call in delta.get("tool_calls") or []:
            events.extend(self.tool_events(call))
        return events

    def text_events(self, text: str) -> list[dict]:
        """Open the text block when none is open, then the
        delta."""
        if self.text_index is None:
            self.text_index = self.next_index
            self.next_index += 1
            events = [
                {
                    "type": "content_block_start",
                    "index": self.text_index,
                    "content_block": {"type": "text", "text": ""},
                }
            ]
        else:
            events = []
        events.append(
            {
                "type": "content_block_delta",
                "index": self.text_index,
                "delta": {"type": "text_delta", "text": text},
            }
        )
        return events

    def tool_events(self, call: dict) -> list[dict]:
        """One tool-call fragment: the first for an index opens the
        ``tool_use`` block (after closing an open text block —
        tool calls follow the text in an assistant turn), and an
        arguments fragment is an ``input_json_delta``."""
        index = call.get("index", 0)
        if index not in self.tool_indexes:
            return self.open_tool_block(index, call) + self.fragment_events(
                index, call
            )
        return self.fragment_events(index, call)

    def open_tool_block(self, index: int, call: dict) -> list[dict]:
        """The ``tool_use`` block's opening events at a fresh
        index, after an open text block closes."""
        events: list[dict] = self.close_text()
        block_index = self.next_index
        self.next_index += 1
        self.tool_indexes[index] = block_index
        function = call.get("function") or {}
        events.append(
            {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": call.get("id") or f"toolu_msks_{index}",
                    "name": function.get("name", ""),
                    "input": {},
                },
            }
        )
        return events

    def fragment_events(self, index: int, call: dict) -> list[dict]:
        """One arguments fragment as an ``input_json_delta``, when
        the fragment carries one."""
        function = call.get("function") or {}
        fragment = function.get("arguments")
        if not (isinstance(fragment, str) and fragment):
            return []
        return [
            {
                "type": "content_block_delta",
                "index": self.tool_indexes[index],
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": fragment,
                },
            }
        ]

    def close_text(self) -> list[dict]:
        """The open text block's stop, when one is open."""
        if self.text_index is None:
            return []
        event = {"type": "content_block_stop", "index": self.text_index}
        self.text_index = None
        return [event]

    def finish(self) -> list[dict]:
        """The closing events: the message frame when no chunk ever
        arrived (an empty answer is still a well-formed stream),
        every open block stops, then the usage-bearing
        ``message_delta`` and ``message_stop``."""
        events = self.start_events() if not self.started else []
        events.extend(self.close_text())
        for block_index in self.tool_indexes.values():
            events.append({"type": "content_block_stop", "index": block_index})
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": self.stop, "stop_sequence": None},
                "usage": {"output_tokens": self.output_tokens},
            }
        )
        events.append({"type": "message_stop"})
        return events


def events_from_message(message: dict) -> list[dict]:
    """A complete Messages message (the non-streaming conversion's
    output) to its full event sequence — the stream a client asked
    for and an upstream that answered without one still gets:
    ``message_start``, every block opened, its whole content as
    one delta, stopped, then the closing pair."""
    events = [message_start(message["model"], 0), {"type": "ping"}]
    for index, block in enumerate(message["content"]):
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": block,
            }
        )
        if block["type"] == "text":
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            delta = {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"]),
            }
        events.append(
            {"type": "content_block_delta", "index": index, "delta": delta}
        )
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": message["stop_reason"],
                "stop_sequence": None,
            },
            "usage": message["usage"],
        }
    )
    events.append({"type": "message_stop"})
    return events


def anthropic_api_error(message: str) -> dict:
    """The Messages error body (the shape a Messages client
    surfaces): one ``type: error`` frame around the message."""
    return {
        "type": "error",
        "error": {"type": "api_error", "message": message},
    }


async def render_anthropic_events(
    chunks: AsyncIterator[Any], dump
) -> AsyncIterator[str]:
    """One OpenAI chunk iterator to the Anthropic SSE text stream
    (the route's streaming half; *dump* is the chunk-to-dict
    helper, injected so the seam stays here beside the shapes it
    renders)."""
    stream = AnthropicStream()
    async for chunk in chunks:
        for event in stream.feed(await dump(chunk)):
            yield sse_line(event)
    for event in stream.finish():
        yield sse_line(event)


# --- the per-tap proxy -----------------------------------------------------


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

    app = FastAPI()
    app.router.add_api_route("/v1/models", list_models, methods=["GET"])
    app.router.add_api_route(
        "/v1/chat/completions", chat_completions, methods=["POST"]
    )
    app.router.add_api_route("/v1/messages", messages, methods=["POST"])
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
