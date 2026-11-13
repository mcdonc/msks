"""The routing machinery: one router per settings identity.

``LlmRouter`` wraps litellm's ``Router`` in multi-provider mode and
httpx in passthrough mode, rebuilding itself when the settings
object changes identity (the SIGHUP swap). Retired passthrough
clients close through :func:`spawn_aclose`, which holds a strong
reference to the close task.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from .anthropic import DEFAULT_ANTHROPIC_VERSION
from .entries import build_model_list, is_passthrough
from .indirect import resolve_indirection

if TYPE_CHECKING:
    from litellm import Router

logger = logging.getLogger(__name__)

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
            # #765: the Messages stream path asks OpenAI-shaped
            # upstreams for chunk usage (``stream_options``); a
            # provider whose API lacks the parameter drops it
            # silently instead of failing the request.
            default_litellm_params={"drop_params": True},
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

    async def passthrough_count_tokens(
        self, body: dict, version: str | None, beta: str | None
    ) -> dict:
        """Forward one count-tokens request (#765): the answer is
        the upstream's own, verbatim — the same header set the
        Messages route builds."""
        assert self._http_client is not None
        resp = await self._http_client.post(
            f"{self._passthrough_base}/messages/count_tokens",
            json=body,
            headers=self.anthropic_headers(version, beta),
        )
        resp.raise_for_status()
        return resp.json()

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
