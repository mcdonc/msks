"""Tests for the workspace LLM proxy (#259)."""

import asyncio
import json
import socket
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from fastapi.responses import JSONResponse, StreamingResponse
from msks.app import build_app
from msks.llm import (
    ANTHROPIC_FIELDS,
    AnthropicStream,
    LlmProxy,
    LlmRouter,
    TapListener,
    anthropic_fields,
    anthropic_to_openai,
    bind_to_device,
    build_model_list,
    chat_fields,
    dispatch_completion,
    events_from_message,
    finish_aclose_task,
    is_passthrough,
    normalize_dict_entry,
    openai_to_anthropic,
    parse_model_entry,
    resolve_indirection,
    spawn_aclose,
    split_entry,
    sse_line,
)
from msks.settings import LlmSettings, Settings

from msks import llm as llm_mod

#: The device pin rides SO_BINDTODEVICE, a Linux socket option
#: absent elsewhere — and the daemon's taps are Linux interfaces
#: — so the listener's bind-and-serve rows run on the Linux host
#: the daemon runs on, with the reason named.
needs_bindtodevice = pytest.mark.skipif(
    sys.platform != "linux",
    reason="SO_BINDTODEVICE pins a listener to its tap on Linux "
    "alone; the daemon's taps are Linux's (#483)",
)

TOKEN = "msksllm1_testtoken"  # a credential-shaped value the proxy ignores
AUTH = {"authorization": f"Bearer {TOKEN}"}
DAEMON = {"authorization": "Bearer a-daemon-api-token"}


@pytest.fixture(scope="module")
def real_litellm():
    """Import litellm once per worker, ahead of the tests that
    build a real Router.

    litellm's ``__init__`` pulls every provider adapter (~5 s of
    import work, irreducible from here). Requested only by the
    real-Router tests, the tax lands in one named module setup
    instead of each test's call time; the xdist group keeps them
    all on one worker, so the whole suite pays it once."""
    import litellm

    return litellm


def llm_settings(
    models: tuple[str | dict, ...] = (), api_key: str | None = None
) -> Settings:
    """Settings with only the LLM group shaped (a fresh object each
    call, so ``ensure`` rebuilds on identity)."""
    return Settings(llm=LlmSettings(models=models, api_key=api_key))


# --- entry parsing ---------------------------------------------------------


def test_split_entry_bounds_first_and_last_colon() -> None:
    # The first colon bounds the model, the last bounds the key.
    assert split_entry("m:a:b:c") == ("m", "a:b", "c")
    assert split_entry("m") == ("m", "", "")
    assert split_entry("m:base") == ("m", "base", "")


def test_split_entry_bounds_indirection_markers() -> None:
    # A file:/cmd: key keeps everything from the marker — paths and
    # commands may carry colons.
    assert split_entry("m:https://h:file:/etc/key") == (
        "m",
        "https://h",
        "file:/etc/key",
    )
    assert split_entry("m::cmd:pass show llm") == (
        "m",
        "",
        "cmd:pass show llm",
    )
    assert split_entry("m:file:/etc/key") == ("m", "", "file:/etc/key")


def test_split_entry_keeps_a_digit_tail_on_a_scheme_as_port() -> None:
    # http://gpu:11434 — the tail after the final colon is the port.
    assert split_entry("m:http://gpu:11434") == (
        "m",
        "http://gpu:11434",
        "",
    )


def test_parse_entry_fills_known_provider_base() -> None:
    parsed = parse_model_entry("openai/gpt-4o::sk-x")
    assert parsed["model_name"] == "gpt-4o"
    assert parsed["litellm_params"]["api_base"] == (
        "https://api.openai.com/v1"
    )
    assert parsed["litellm_params"]["api_key"] == "sk-x"


def test_parse_entry_keeps_explicit_base_and_bare_model() -> None:
    parsed = parse_model_entry("local/m:http://h:9")
    assert parsed["model_name"] == "m"
    # An unknown provider names no default base; the entry's own
    # stands, port included (the authority's colon is a port, and a
    # key may follow the path).
    assert parsed["litellm_params"]["api_base"] == "http://h:9"
    assert "api_key" not in parsed["litellm_params"]
    # A model with no provider slash resolves no default either,
    # and a key after a path splits off cleanly.
    bare = parse_model_entry("m:https://h/v1")
    assert bare["litellm_params"]["api_base"] == "https://h/v1"
    keyed = parse_model_entry("m:https://h/v1:sk-x")
    assert keyed["litellm_params"]["api_base"] == "https://h/v1"
    assert keyed["litellm_params"]["api_key"] == "sk-x"


def test_parse_entry_resolves_file_indirection_on_the_key(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("sk-from-file\n")
    parsed = parse_model_entry(f"m:https://h:file:{key_file}")
    assert parsed["litellm_params"]["api_key"] == "sk-from-file"


def test_indirection_forms(tmp_path: Path) -> None:
    cmd_file = tmp_path / "cmd"
    cmd_file.write_text("echo sk-from-cmd")
    assert (
        resolve_indirection(f"cmd:sh {cmd_file}", "the key") == "sk-from-cmd"
    )
    assert resolve_indirection("plain", "the key") == "plain"
    failing = tmp_path / "failing.sh"
    failing.write_text("echo oops >&2; exit 3")
    with pytest.raises(ValueError, match="exited 3"):
        resolve_indirection(f"cmd:sh {failing}", "the key")
    with pytest.raises(ValueError, match="cannot read file: reference"):
        resolve_indirection("file:/nonexistent/key", "the key")


def test_build_model_list_fills_the_default_key() -> None:
    items = build_model_list(
        ("openai/one::sk-1", "openai/two::"), "sk-default"
    )
    assert items[0]["litellm_params"]["api_key"] == "sk-1"
    assert items[1]["litellm_params"]["api_key"] == "sk-default"


def test_is_passthrough_is_exactly_one_wildcard() -> None:
    assert is_passthrough([{"model_name": "*"}])
    assert not is_passthrough([{"model_name": "my*model"}])
    assert not is_passthrough([{"model_name": "*"}, {"model_name": "o"}])


# --- the router ------------------------------------------------------------


class FakeLitellmRouter:
    """The litellm.Router stand-in: records the call, answers preset."""

    def __init__(self, answer=None, chunks=None) -> None:
        self.answer = answer if answer is not None else {"ok": True}
        self.chunks = chunks
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        if self.chunks is not None:
            return _chunk_generator(self.chunks)
        return self.answer

    def get_model_names(self) -> list[str]:
        return ["one", "two"]


async def _chunk_generator(chunks):
    for chunk in chunks:
        yield chunk


class Dumpable:
    """A response object litellm shapes: model_dump to a dict."""

    def model_dump(self) -> dict:
        return {"dumped": True}


async def upstream_handler(request: httpx.Request) -> httpx.Response:
    """The upstream's answers: models list, one completion, SSE."""
    auth = request.headers.get("authorization")
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "up-model"}]})
    body = json.loads(request.content.decode())
    if body.get("stream"):
        return httpx.Response(
            200,
            content=b"data: chunk-one\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(200, json={"echo": body, "auth": auth})


def passthrough_router(handler) -> LlmRouter:
    """A router in single-upstream passthrough mode over a mock
    transport (the client-factory test seam)."""
    router = LlmRouter()
    router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=timeout
    )
    router.ensure(llm_settings(("*:http://up.stream/v1:sk-x",)))
    return router


async def test_passthrough_completion_forwards_and_answers() -> None:
    router = passthrough_router(upstream_handler)
    assert router.active and router.passthrough
    answer = await router.acompletion(
        model="anything", messages=[{"role": "user", "content": "hi"}]
    )
    # The request went through verbatim (model included — passthrough
    # resolves nothing) with the upstream key attached.
    assert answer["echo"]["model"] == "anything"
    assert answer["auth"] == "Bearer sk-x"


async def test_passthrough_models_discover_from_upstream() -> None:
    router = passthrough_router(upstream_handler)
    assert await router.list_upstream_models() == [{"id": "up-model"}]


async def test_passthrough_models_swallows_upstream_failure(caplog) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    router = passthrough_router(down)
    with caplog.at_level("ERROR"):
        assert await router.list_upstream_models() == []
    assert any("upstream models" in r.message for r in caplog.records)


async def test_passthrough_stream_forwards_sse_lines() -> None:
    router = passthrough_router(upstream_handler)
    resp = await router.passthrough_completion_stream({"stream": True})
    response = await dispatch_completion(router, {"stream": True})
    assert isinstance(response, StreamingResponse)
    body = "".join([chunk async for chunk in response.body_iterator])
    assert body == "data: chunk-one\n\ndata: [DONE]\n\n"
    await resp.aclose()


@pytest.mark.xdist_group("litellm-import")
async def test_litellm_mode_builds_a_real_router_and_defaults_the_model(
    tmp_path: Path, real_litellm
) -> None:
    # The real litellm import (its tax is paid once per worker by
    # the fixture): two entries, no wildcard — router mode.
    key_file = tmp_path / "k"
    key_file.write_text("sk-1")
    router = LlmRouter()
    router.ensure(
        llm_settings(("openai/one::file:" + str(key_file), "openai/two::sk-2"))
    )
    assert router.active and not router.passthrough
    assert router.get_model_names() == ["one", "two"]
    fake = FakeLitellmRouter()
    router._router = fake
    await router.acompletion(messages=[])
    # An empty model names the first configured one.
    assert fake.calls[0]["model"] == "one"
    # Router mode's model list is the configured names, msksd-owned.
    models = await router.list_upstream_models()
    assert [m["id"] for m in models] == ["one", "two"]
    assert models[0]["owned_by"] == "msksd"


async def test_litellm_mode_streams_chunks_in_openai_sse() -> None:
    router = LlmRouter()
    router._router = FakeLitellmRouter(chunks=[{"delta": 1}, Dumpable(), "r"])
    response = await dispatch_completion(router, {"stream": True})
    assert isinstance(response, StreamingResponse)
    lines = "".join([chunk async for chunk in response.body_iterator])
    assert '"delta": 1' in lines
    assert '"dumped": true' in lines
    assert "r" in lines
    assert lines.endswith("data: [DONE]\n\n")


async def test_json_response_covers_the_mapping_flavors() -> None:
    router = LlmRouter()
    router._router = FakeLitellmRouter(answer={"plain": 1})
    assert isinstance(await dispatch_completion(router, {}), JSONResponse)
    router._router = FakeLitellmRouter(answer=Dumpable())
    assert isinstance(await dispatch_completion(router, {}), JSONResponse)
    router._router = FakeLitellmRouter(answer=MappingProxyType({"m": 2}))
    response = await dispatch_completion(router, {})
    assert isinstance(response, JSONResponse)
    assert json.loads(response.body) == {"m": 2}


def test_ensure_rebuilds_only_on_settings_change(monkeypatch) -> None:
    router = LlmRouter()
    first = llm_settings(("*:http://a:9",))
    second = llm_settings(("*:http://b:9",))
    closed: list = []
    monkeypatch.setattr(
        "msks.llm.spawn_aclose", lambda client: closed.append(client)
    )
    router.ensure(first)
    client_one = router._http_client
    router.ensure(first)  # same object: nothing rebuilds
    assert router._http_client is client_one
    router.ensure(second)
    assert closed == [client_one]
    assert router._passthrough_base == "http://b:9"


def test_reconfigure_to_empty_closes_and_deactivates(monkeypatch) -> None:
    router = LlmRouter()
    closed: list = []
    monkeypatch.setattr(
        "msks.llm.spawn_aclose", lambda client: closed.append(client)
    )
    router.ensure(llm_settings(("*:http://a:9",)))
    assert router.active
    router.ensure(llm_settings(()))
    assert closed and not router.active
    assert router._http_client is None


def test_configure_with_a_broken_key_reference_fails_named() -> None:
    router = LlmRouter()
    with pytest.raises(ValueError, match="default api key"):
        router.ensure(
            llm_settings(("*:http://a:9",), api_key="file:/nonexistent")
        )


async def test_spawn_aclose_and_its_done_callback() -> None:
    # Outside a loop: a no-op (the sync construction path).
    client = httpx.AsyncClient()
    spawn_aclose(client)
    await client.aclose()

    # A failed close surfaces as a logged error; a cancelled one
    # returns quietly.
    async def boom() -> None:
        raise RuntimeError("close failed")

    failing = asyncio.create_task(boom())
    with pytest.raises(RuntimeError):
        await failing
    finish_aclose_task(failing)
    cancelled = asyncio.create_task(asyncio.sleep(30))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    finish_aclose_task(cancelled)


# --- the device pin (#483) -------------------------------------------------


@needs_bindtodevice
def test_bind_records_an_ephemeral_asks_chosen_port() -> None:
    """``port=0`` binds a kernel-chosen number and the listener
    records it (#532): the probe dial repoint reads the served
    port off the running listener's registry entry, so the
    attribute must name the bound port, not the ask."""
    listener = TapListener(StubModel(), tap="lo", tap_ip="127.0.0.1", port=0)
    try:
        listener.bind()
        assert listener.port != 0
        assert listener.port == listener._sock.getsockname()[1]
    finally:
        if listener._sock is not None:
            listener._sock.close()


@needs_bindtodevice
def test_bind_to_device_pins_the_named_tap() -> None:
    """The pin is one setsockopt carrying the tap name as a NUL-
    terminated bytes value — the shape SO_BINDTODEVICE reads."""
    calls: list[tuple[int, int, bytes]] = []

    class Sock:
        def setsockopt(self, level, opt, value):
            calls.append((level, opt, value))

    bind_to_device(Sock(), "tap-msks-ws1")  # type: ignore[arg-type]
    assert calls == [
        (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tap-msks-ws1\0")
    ]


# --- auth ------------------------------------------------------------------


# --- the proxy app ---------------------------------------------------------


class StubModel:
    """The model surface the proxy reads: the daemon-token check.
    A presented bearer the daemon accepts is refused at the proxy;
    every other value passes."""

    def __init__(self, daemon_tokens: set[str] | None = None) -> None:
        self.daemon_tokens = daemon_tokens or set()

    async def token_valid(self, plaintext: str) -> bool:
        return plaintext in self.daemon_tokens


@dataclass
class FakeListener:
    """The manager seam's listener: records its lifecycle."""

    tap_ip: str
    started: bool = False
    stopped: bool = False
    fail: bool = False
    starts: int = 0

    async def start(self) -> None:
        if self.fail:
            raise OSError("address in use")
        self.started = True
        self.starts += 1

    async def stop(self) -> None:
        self.stopped = True


def proxy_under_test(
    model: StubModel | None = None, settings: Settings | None = None
) -> LlmProxy:
    app = build_app(settings or llm_settings())
    app.state.model = model or StubModel()
    return app.state.llm


def async_client(
    proxy: LlmProxy, tap_ip: str, *, mapped: bool = True
) -> httpx.AsyncClient:
    if mapped:
        proxy._by_tap_ip[tap_ip] = "ws1"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.proxy_app),
        base_url=f"http://{tap_ip}",
    )


async def test_proxy_auth_is_the_tap_not_the_header() -> None:
    """The tap serves; the header carries no authority (#483): a
    credential-less request, the seeded placeholder, and garbage
    all pass on a mapped tap."""
    proxy = proxy_under_test()
    async with async_client(proxy, "10.0.0.1") as http:
        assert (await http.get("/v1/models")).status_code == 503
        assert (await http.get("/v1/models", headers=AUTH)).status_code == 503
        assert (
            await http.get(
                "/v1/models", headers={"authorization": "Bearer junk"}
            )
        ).status_code == 503


async def test_proxy_auth_refuses_a_daemon_api_token() -> None:
    """The one refused credential: a bearer the daemon's own token
    table accepts — the API surface's credential stays a different
    class than the proxy's no-credential surface."""
    proxy = proxy_under_test(StubModel({"a-daemon-api-token"}))
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.get("/v1/models", headers=DAEMON)
    assert reply.status_code == 401
    assert "not a proxy credential" in reply.json()["detail"]


async def test_proxy_auth_rejects_an_unmapped_tap() -> None:
    proxy = proxy_under_test()
    async with async_client(proxy, "10.9.9.9", mapped=False) as http:
        assert (await http.get("/v1/models", headers=AUTH)).status_code == 401


async def test_models_endpoint_answers_the_openai_shape() -> None:
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.get("/v1/models", headers=AUTH)
    assert reply.status_code == 200
    assert reply.json() == {"object": "list", "data": [{"id": "up-model"}]}


async def test_completions_endpoint_serves_json() -> None:
    # Unconfigured: the ported klangk posture — the route exists,
    # 503 answers.
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "m", "messages": []},
        )
    # Configured passthrough: the upstream's answer comes home.
    assert reply.status_code == 200
    assert reply.json()["echo"]["model"] == "m"


async def test_completions_endpoint_answers_503_unconfigured() -> None:
    proxy = proxy_under_test()
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/chat/completions", headers=AUTH, json={"x": 1}
        )
        assert reply.status_code == 503


async def test_completions_endpoint_streams_and_maps_upstream_failure() -> (
    None
):
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        stream = await http.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "m", "messages": [], "stream": True},
        )
        assert stream.status_code == 200
        assert stream.text == "data: chunk-one\n\ndata: [DONE]\n\n"

        def down(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(down), timeout=timeout
        )
        # A fresh settings object: ensure rebuilds onto the new
        # factory's client (the request path's own ensure then sees
        # the app's object and rebuilds again — same factory).
        proxy.router.ensure(llm_settings(("*:http://up.stream/v1:sk-x",)))
        failure = await http.post(
            "/v1/chat/completions", headers=AUTH, json={"model": "m"}
        )
        assert failure.status_code == 502
        assert failure.json() == {"error": "LLM upstream request failed"}


# --- the tap listener ------------------------------------------------------


@needs_bindtodevice
async def test_tap_listener_serves_real_http_and_stops() -> None:
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    # The pin uses "lo" here: the listener serves loopback in
    # this test, and packets from 127.0.0.1 arrive on lo — the
    # same device-match rule a tap listener applies to its tap.
    listener = TapListener(
        proxy.proxy_app, tap="lo", tap_ip="127.0.0.1", port=0
    )
    await proxy.start_listener("ws1", listener)
    assert proxy._by_tap_ip["127.0.0.1"] == "ws1"
    port = listener._sock.getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        reply = await http.get("/v1/models", headers=AUTH)
    assert reply.status_code == 200
    assert reply.json()["data"] == [{"id": "up-model"}]
    await proxy.stop_listener("ws1")
    await proxy.stop_listener("ws1")  # idempotent
    assert listener._task is None


@needs_bindtodevice
async def test_tap_listener_leaves_process_logging_and_signals_alone() -> None:
    """The embedded server configures nothing global (#433 fix).

    uvicorn's Config applies its log level to the shared
    "uvicorn.error" logger at load time, and its serve() wraps
    itself in process-wide signal capture — an embedded "warning"
    silenced the outer server's INFO records for the rest of the
    process life (its "Application shutdown complete." proof among
    them; the shutdown itself ran), and the capture swapping
    handlers on a listener's stop left the outer server's arm
    disarmed. The listener's config carries no level and no config,
    and its serve captures no signals.
    """
    import contextlib

    proxy = proxy_under_test({}, llm_settings(()))
    # The pin uses "lo" here: the listener serves loopback in
    # this test, and packets from 127.0.0.1 arrive on lo — the
    # same device-match rule a tap listener applies to its tap.
    listener = TapListener(
        proxy.proxy_app, tap="lo", tap_ip="127.0.0.1", port=0
    )
    await proxy.start_listener("ws1", listener)
    try:
        config = listener._server.config
        assert config.log_level is None
        assert config.log_config is None
        assert listener._server.capture_signals is contextlib.nullcontext
        assert not hasattr(listener._server, "install_signal_handlers")
    finally:
        await proxy.stop_listener("ws1")


async def test_listener_bind_failure_unregisters_the_mapping() -> None:
    proxy = proxy_under_test()
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    listener = TapListener(
        proxy.proxy_app, tap="lo", tap_ip="127.0.0.1", port=port
    )
    with pytest.raises(OSError):
        await proxy.start_listener("ws1", listener)
    assert "ws1" not in proxy._by_tap_ip
    squatter.close()


# --- coverage: the remaining branches ---------------------------------------


def test_spawn_aclose_outside_a_loop_is_a_noop() -> None:
    # The sync construction path: no loop to schedule on.
    client = httpx.AsyncClient()
    spawn_aclose(client)
    asyncio.run(client.aclose())


def test_parse_entry_without_a_base_or_key() -> None:
    parsed = parse_model_entry("m::k")
    assert "api_base" not in parsed["litellm_params"]
    assert parsed["litellm_params"]["api_key"] == "k"


async def test_resolve_router_model_names_the_empty_list() -> None:
    class Empty(FakeLitellmRouter):
        def get_model_names(self) -> list[str]:
            return []

    router = LlmRouter()
    router._router = Empty()
    with pytest.raises(RuntimeError, match="no models configured"):
        router.resolve_router_model({"model": ""})


async def test_resolve_router_model_keeps_a_valid_name() -> None:
    router = LlmRouter()
    fake = FakeLitellmRouter()
    router._router = fake
    kwargs = {"model": "two", "messages": []}
    router.resolve_router_model(kwargs)
    assert kwargs["model"] == "two"


async def test_acompletion_unconfigured_names_the_state() -> None:
    router = LlmRouter()
    with pytest.raises(RuntimeError, match="not configured"):
        await router.acompletion(messages=[])


async def test_passthrough_without_a_key_sends_no_authorization() -> None:
    router = LlmRouter()
    router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    router.ensure(llm_settings(("*:http://up.stream/v1",)))
    answer = await router.acompletion(messages=[])
    assert answer["auth"] is None
    assert router.get_model_names() == []


async def test_streaming_upstream_failure_maps_to_502() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(down), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        failure = await http.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "m", "messages": [], "stream": True},
        )
        assert failure.status_code == 502


def test_listener_for_follows_the_model_list() -> None:
    from types import SimpleNamespace

    attachment = SimpleNamespace(tap="tap-x", tap_ip="172.31.0.2")
    proxy = proxy_under_test()
    assert proxy.listener_for(attachment) is None
    configured = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    listener = configured.listener_for(attachment)
    assert isinstance(listener, TapListener)
    assert listener.tap_ip == "172.31.0.2"
    assert listener.port == 8770


@needs_bindtodevice
async def test_tap_listener_accepts_a_prebound_socket() -> None:
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    # The pin uses "lo" here: the listener serves loopback in
    # this test, and packets from 127.0.0.1 arrive on lo — the
    # same device-match rule a tap listener applies to its tap.
    listener = TapListener(
        proxy.proxy_app, tap="lo", tap_ip="127.0.0.1", port=0
    )
    listener.bind()  # the eager path start() would otherwise take
    await proxy.start_listener("ws1", listener)
    port = listener._sock.getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        reply = await http.get("/v1/models", headers=AUTH)
    assert reply.status_code == 200
    await listener.stop()
    await listener.stop()  # the listener's own idempotent stop


def test_stop_mapping_leaves_a_reassigned_address() -> None:
    proxy = proxy_under_test({})
    listener = FakeListener("172.31.0.2")
    proxy._listeners["ws1"] = listener
    proxy._by_tap_ip["172.31.0.2"] = "ws9"  # a successor took the tap
    proxy.stop_mapping("ws1", listener)
    assert "ws1" not in proxy._listeners
    assert proxy._by_tap_ip["172.31.0.2"] == "ws9"


def test_workspace_for_request_without_a_server_scope() -> None:
    from types import SimpleNamespace

    proxy = proxy_under_test({})
    assert proxy.workspace_for_request(SimpleNamespace(scope={})) is None


# --- review findings: injection, malformed bodies, oversize ----------------


async def test_injected_routing_fields_never_reach_the_router() -> None:
    """The allowlist is the whole defense (#259 review): a guest body
    naming api_base/api_key/headers must not redirect the daemon's
    upstream or ride the provider key out. Router mode: the kwargs
    litellm sees carry none of it."""
    router = LlmRouter()
    fake = FakeLitellmRouter()
    router._router = fake
    await dispatch_completion(
        router,
        chat_fields(
            {
                "model": "one",
                "messages": [],
                "api_base": "http://attacker/v1",
                "api_key": "sk-guess",
                "base_url": "http://attacker/v1",
                "headers": {"x": "y"},
                "timeout": 1,
            }
        ),
    )
    assert fake.calls[0]["model"] == "one"
    for field in ("api_base", "api_key", "base_url", "headers", "timeout"):
        assert field not in fake.calls[0]


async def test_injected_routing_fields_never_leave_passthrough() -> None:
    """Passthrough mode: the upstream receives the filtered body —
    the injected fields drop before the wire."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        seen.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "m",
                "messages": [],
                "api_base": "http://attacker/v1",
                "api_key": "sk-guess",
            },
        )
    assert reply.status_code == 200
    assert seen == {"model": "m", "messages": []}


async def test_non_ascii_bearers_still_pass_the_tap() -> None:
    """HTTP header bytes are latin-1-decoded: a crafted bearer the
    daemon check cannot accept still passes on a mapped tap (the
    proxy reads no credential), never a decode error. httpx
    refuses to build non-ascii headers, so the request rides the
    ASGI scope directly — the same bytes a socket would carry."""
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    proxy._by_tap_ip["10.0.0.1"] = "ws1"

    status: dict = {}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/v1/models",
        "raw_path": b"/v1/models",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "client": ("10.0.0.1", 12345),
        "server": ("10.0.0.1", 8770),
        "headers": [
            (b"host", b"10.0.0.1:8770"),
            (b"authorization", "Bearer caf\xe9tok".encode("latin-1")),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await proxy.proxy_app(scope, receive, send)
    assert status["code"] == 200


async def test_malformed_and_oversized_bodies_answer_named_codes(
    monkeypatch,
) -> None:
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    monkeypatch.setattr(llm_mod, "MAX_BODY_BYTES", 16)
    async with async_client(proxy, "10.0.0.1") as http:
        not_json = await http.post(
            "/v1/chat/completions",
            headers={**AUTH, "content-type": "application/json"},
            content=b"this is not json",
        )
        assert not_json.status_code == 400
        assert not_json.json() == {"error": "request body is not JSON"}
        not_object = await http.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=[1, 2, 3],
        )
        assert not_object.status_code == 400
        oversized = await http.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "m", "messages": [{"role": "u", "content": "x"}]},
        )
        assert oversized.status_code == 413
        assert oversized.json() == {"error": "request body exceeds 16 bytes"}


async def test_models_endpoint_answers_503_once_unconfigured() -> None:
    """Models removed by a SIGHUP swap: the open port answers 503 —
    the closed-port posture arrives with the next stop/start."""
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    # The SIGHUP shape: the app's own settings object is swapped for
    # the empty one — the request path re-reads it live.
    proxy.app.state.settings = llm_settings(())
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.get("/v1/models", headers=AUTH)
    assert reply.status_code == 503


async def test_ensure_async_serializes_one_reconfigure() -> None:
    """A concurrent second wave after one SIGHUP swap: both coros
    see the new settings, the lock admits one configure, the other
    returns at the inner recheck — one rebuild, not two."""
    router = LlmRouter()
    first = llm_settings(("*:http://a.stream/v1:9",))
    second = llm_settings(("*:http://b.stream/v1:9",))
    await router.ensure_async(first)
    assert router._lock is not None
    await asyncio.gather(
        router.ensure_async(second), router.ensure_async(second)
    )
    assert router._passthrough_base == "http://b.stream/v1"


class StreamedRequest:
    """A request stub whose body arrives without Content-Length."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers = {}

    def stream(self):
        return self._agen()

    async def _agen(self):
        for chunk in self._chunks:
            yield chunk


async def test_parse_body_bounds_a_headerless_stream(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod, "MAX_BODY_BYTES", 8)
    refusal, body = await llm_mod.parse_body(
        StreamedRequest([b'{"model":', b' "m"}'])
    )
    assert body is None
    assert refusal.status_code == 413
    refusal, body = await llm_mod.parse_body(StreamedRequest([b'{"a": 1}']))
    assert refusal is None
    assert body == {"a": 1}


# --- second review: client retirement, config-failure posture ---------------


async def test_ensure_async_closes_the_replaced_client_on_the_loop() -> None:
    """The to_thread configure cannot schedule closes itself (no
    loop in the worker): the replaced client retires, and the
    serving loop's ensure drains it — the close task really runs."""
    router = LlmRouter()
    router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    await router.ensure_async(llm_settings(("*:http://a.stream/v1:k",)))
    first = router._http_client
    await router.ensure_async(llm_settings(("*:http://b.stream/v1:k",)))
    assert first is not None and not first.is_closed
    # The drain scheduled the close on this loop; give it a tick.
    for _ in range(10):
        await asyncio.sleep(0)
    assert first.is_closed
    assert not router._retired


async def test_a_failed_configure_answers_a_named_503(caplog) -> None:
    """A SIGHUP onto a dead file: reference: both routes answer a
    503 naming the configure failure — and the body never carries
    the entry's text (it may hold an inline key)."""
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-live-key",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    proxy.app.state.settings = llm_settings(
        ("*:http://up.stream/v1:file:/nonexistent/key",)
    )
    async with async_client(proxy, "10.0.0.1") as http:
        with caplog.at_level("ERROR"):
            models = await http.get("/v1/models", headers=AUTH)
            completion = await http.post(
                "/v1/chat/completions",
                headers=AUTH,
                json={"model": "m", "messages": []},
            )
    for reply in (models, completion):
        assert reply.status_code == 503
        assert reply.json() == {"error": "LLM router configuration failed"}
        assert "sk-live-key" not in reply.text
        assert "/nonexistent" not in reply.text
    # The daemon log keeps the named cause.
    assert any(
        "LLM router configuration failed" in r.message for r in caplog.records
    )


# --- dict entries (the file's list form, klangk's shape) ---------------------


def test_dict_entries_normalize_keys_and_indirection(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("sk-dict\n")
    parsed = normalize_dict_entry(
        {
            "model-name": "claude",
            "litellm-params": {
                "model": "anthropic/claude-sonnet-4",
                "api-key": f"file:{key_file}",
                "rpm": 10,
            },
        }
    )
    assert parsed["model_name"] == "claude"
    assert parsed["litellm_params"]["api_key"] == "sk-dict"
    assert parsed["litellm_params"]["rpm"] == 10


def test_params_is_the_litellm_params_shorthand() -> None:
    parsed = normalize_dict_entry(
        {"model_name": "m", "params": {"model": "openai/m"}}
    )
    assert parsed["litellm_params"] == {"model": "openai/m"}


def test_build_model_list_mixed_entries_and_default_key() -> None:
    items = build_model_list(
        ("openai/one::sk-1", {"model_name": "two", "params": {}}),
        "sk-default",
    )
    assert items[0]["litellm_params"]["api_key"] == "sk-1"
    assert items[1]["litellm_params"]["api_key"] == "sk-default"
    # An entry that arrived without a params block still takes the
    # default key — the block attaches, never a detached write
    # (klangk's original lost it).
    bare = build_model_list(({"model_name": "bare"},), "sk-default")
    assert bare[0]["litellm_params"]["api_key"] == "sk-default"


def test_a_dict_entry_can_name_passthrough() -> None:
    router = LlmRouter()
    router.ensure(
        llm_settings(
            (
                {
                    "model_name": "*",
                    "litellm_params": {
                        "api_base": "http://up.stream/v1",
                        "api_key": "sk-x",
                    },
                },
            )
        )
    )
    assert router.passthrough and router._passthrough_base == (
        "http://up.stream/v1"
    )


@pytest.mark.xdist_group("litellm-import")
def test_a_dict_list_reaches_a_real_litellm_router(real_litellm) -> None:
    """The dict form is not normalize-only: a configured entry with
    routing knobs constructs the real Router and serves its
    model_name — the docs' yaml example, proven against litellm
    itself."""
    router = LlmRouter()
    router.ensure(
        llm_settings(
            (
                {
                    "model_name": "cl-sonnet",
                    "litellm_params": {
                        "model": "anthropic/claude-sonnet-4",
                        "api_key": "sk-2",
                        "rpm": 10,
                    },
                },
                "openai/one::sk-1",
            )
        )
    )
    assert not router.passthrough
    assert router.get_model_names() == ["cl-sonnet", "one"]


@pytest.mark.xdist_group("litellm-import")
def test_a_real_litellm_router_never_rides_the_aiohttp_transport(
    real_litellm,
) -> None:
    """Building the real Router leaves litellm's aiohttp transport
    off: litellm's async handlers default to an aiohttp ClientSession
    wrapped as an httpx transport, and the closure's aiohttp sits
    below litellm's own floor (nix/litellm-pkg.nix records the
    CVE-bearing history), so the daemon's completions stay on plain
    httpx — the transport the passthrough mode already uses. Both
    halves of the contract are pinned: msks sets the module flag,
    and litellm's own transport selector — the same function its
    health endpoints report — answers that no aiohttp transport is
    used, so a litellm upgrade that renames or drops the flag fails
    here instead of silently reverting the request path."""
    router = LlmRouter()
    router.ensure(llm_settings(("openai/one::sk-1",)))
    assert real_litellm.disable_aiohttp_transport is True
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    assert AsyncHTTPHandler.should_use_aiohttp_transport() is False


def test_a_baseless_passthrough_entry_is_a_named_configure_error() -> None:
    router = LlmRouter()
    with pytest.raises(ValueError, match="names no api_base"):
        router.ensure(llm_settings(({"model_name": "*"},)))


@pytest.mark.xdist_group("litellm-import")
def test_normalize_copies_deep_between_configures(real_litellm) -> None:
    """The settings entry survives configure: the Router's copy is
    its own, at depth — an entry configured twice reads the same
    both times."""
    entry = {
        "model_name": "m",
        "litellm_params": {
            "model": "openai/m",
            "api_key": "sk-1",
            "metadata": {"team": "core"},
        },
    }
    router = LlmRouter()
    router.ensure(llm_settings((entry,)))
    first = router._router.model_list[0]
    first["litellm_params"]["metadata"]["team"] = "mutated"
    router.ensure(llm_settings((entry,)))
    again = router._router.model_list[0]
    assert again["litellm_params"]["metadata"]["team"] == "core"
    assert entry["litellm_params"]["metadata"]["team"] == "core"


# --- the Anthropic Messages translation (#765) ----------------------------


def test_anthropic_fields_drops_routing_and_unknown_fields() -> None:
    """The Messages allowlist behaves like the OpenAI one: routing
    parameters a guest injected never reach the converter, and
    ``top_k`` (no OpenAI field) drops by design."""
    kept = anthropic_fields(
        {
            "model": "claude-x",
            "messages": [],
            "max_tokens": 64,
            "api_key": "sk-guess",
            "api_base": "http://attacker/v1",
            "top_k": 3,
            "unknown": True,
        }
    )
    assert kept == {"model": "claude-x", "messages": [], "max_tokens": 64}
    assert "top_k" not in ANTHROPIC_FIELDS


def test_anthropic_to_openai_converts_a_tool_conversation() -> None:
    """A full claude-code-shaped body converts: system blocks to
    one system message, a tool_use turn to an assistant
    tool_calls message, the tool_result turn to tool messages
    that come first, images to data-URL parts, tools to the
    function shape, and the scalar fields map name for name."""
    kwargs = anthropic_to_openai(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "temperature": 0.2,
            "top_p": 0.9,
            "stop_sequences": ["\n\nHuman:"],
            "stream": False,
            "system": [
                {"type": "text", "text": "Be terse."},
                {"type": "text", "text": "Use tools."},
            ],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Weather for a city.",
                    "input_schema": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "any"},
            "metadata": {"user_id": "u-7"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Weather in Trenton?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aGk=",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "Trenton"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": "Sunny"}],
                        },
                        {"type": "text", "text": "Go on."},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "hmm",
                            "signature": "s",
                        }
                    ],
                },
            ],
        }
    )
    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["max_tokens"] == 1024
    assert kwargs["temperature"] == 0.2
    assert kwargs["top_p"] == 0.9
    assert kwargs["stop"] == ["\n\nHuman:"]
    assert kwargs["stream"] is False
    assert kwargs["user"] == "u-7"
    assert kwargs["tool_choice"] == "required"
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Weather for a city.",
                "parameters": {"type": "object"},
            },
        }
    ]
    messages = kwargs["messages"]
    assert messages[0] == {
        "role": "system",
        "content": "Be terse.\n\nUse tools.",
    }
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0] == {
        "type": "text",
        "text": "Weather in Trenton?",
    }
    assert messages[1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert messages[2] == {
        "role": "assistant",
        "content": "Checking.",
        "tool_calls": [
            {
                "type": "function",
                "id": "toolu_1",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Trenton"}',
                },
            }
        ],
    }
    # The tool message comes first, then the turn's own text.
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "Sunny",
    }
    assert messages[4] == {
        "role": "user",
        "content": [{"type": "text", "text": "Go on."}],
    }
    # The thinking-only turn contributed nothing.
    assert messages[-1] is messages[4]


def test_anthropic_to_openai_tool_choice_and_string_shapes() -> None:
    """The tool_choice variants map; a plain string turn and an
    empty system block keep the simple shapes."""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "system": "",
        "tool_choice": {"type": "tool", "name": "f"},
    }
    kwargs = anthropic_to_openai(body)
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "f"},
    }
    assert anthropic_to_openai({"tool_choice": {"type": "auto"}}) == {
        "messages": [],
        "tool_choice": "auto",
    }
    assert anthropic_to_openai({"tool_choice": {"type": "none"}}) == {
        "messages": [],
        "tool_choice": "none",
    }
    # A shape the map does not know drops; a non-dict one too.
    assert "tool_choice" not in anthropic_to_openai(
        {"tool_choice": {"type": "weird"}}
    )
    assert "tool_choice" not in anthropic_to_openai({"tool_choice": "auto"})
    # A tool_result-only user turn contributes the tool message
    # alone; a tool_use-only assistant turn carries null content.
    kwargs = anthropic_to_openai(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": "done",
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t",
                            "name": "f",
                            "input": {},
                        }
                    ],
                },
            ]
        }
    )
    assert kwargs["messages"] == [
        {"role": "tool", "tool_call_id": "t", "content": "done"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "type": "function",
                    "id": "t",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ],
        },
    ]


def test_openai_to_anthropic_covers_text_tools_and_usage() -> None:
    """The answer conversion: text and tool calls become blocks,
    the finish reasons map, usage maps with zeros when absent,
    and an unparsable arguments string answers an empty input."""
    message = openai_to_anthropic(
        {
            "id": "chatcmpl-1",
            "model": "gpt-test",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "Let me check.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "f",
                                    "arguments": '{"a": 1}',
                                },
                            },
                            {
                                "id": "call_2",
                                "function": {
                                    "name": "g",
                                    "arguments": "not json",
                                },
                            },
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4},
        }
    )
    assert message["id"] == "chatcmpl-1"
    assert message["type"] == "message"
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 9, "output_tokens": 4}
    assert message["content"][0] == {"type": "text", "text": "Let me check."}
    assert message["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "f",
        "input": {"a": 1},
    }
    assert message["content"][2]["input"] == {}

    # The other finish reasons, part-list content, and the empty
    # answer's placeholder block.
    def answer(finish, content):
        return openai_to_anthropic(
            {
                "model": "m",
                "choices": [
                    {"finish_reason": finish, "message": {"content": content}}
                ],
            }
        )

    assert answer("stop", "bye")["stop_reason"] == "end_turn"
    assert answer("length", None)["stop_reason"] == "max_tokens"
    assert answer(None, None)["content"] == [{"type": "text", "text": ""}]
    assert answer("stop", [{"type": "text", "text": "part"}])["content"] == [
        {"type": "text", "text": "part"}
    ]


def test_anthropic_stream_converts_a_chunk_sequence() -> None:
    """The stream state machine: message_start with the first
    chunk's usage, text deltas under one block index, tool-call
    fragments opening their own block after the text closes, the
    final usage recorded, and the closing pair carrying the
    stop reason."""
    stream = AnthropicStream()
    events: list[dict] = []
    for chunk in [
        {
            "model": "gpt-test",
            "usage": {"prompt_tokens": 7},
            "choices": [{"delta": {"content": "Hel"}}],
        },
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "f", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"a"'}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"completion_tokens": 6},
        },
    ]:
        events.extend(stream.feed(chunk))
    events.extend(stream.finish())
    kinds = [event["type"] for event in events]
    assert kinds == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = events[0]
    assert start["message"]["model"] == "gpt-test"
    assert start["message"]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 0,
    }
    assert events[2]["content_block"] == {"type": "text", "text": ""}
    assert events[3]["delta"] == {"type": "text_delta", "text": "Hel"}
    assert events[4]["delta"] == {"type": "text_delta", "text": "lo"}
    assert events[5]["index"] == 0
    assert events[6]["content_block"]["type"] == "tool_use"
    assert events[6]["content_block"]["id"] == "call_1"
    assert events[6]["index"] == 1
    assert events[7]["delta"] == {
        "type": "input_json_delta",
        "partial_json": '{"a"',
    }
    closing = events[-2]
    assert closing["delta"] == {
        "stop_reason": "tool_use",
        "stop_sequence": None,
    }
    assert closing["usage"] == {"output_tokens": 6}
    # Every rendered line carries the event name beside the data.
    assert sse_line(events[0]).startswith("event: message_start\ndata: {")


def test_anthropic_stream_finishes_what_never_started() -> None:
    """A chunk stream that ends before its first chunk (an empty
    upstream answer) still renders a well-formed event sequence."""
    events = AnthropicStream().finish()
    assert [event["type"] for event in events] == [
        "message_start",
        "ping",
        "message_delta",
        "message_stop",
    ]
    assert events[-2]["delta"]["stop_reason"] == "end_turn"


def test_events_from_message_renders_the_full_sequence() -> None:
    """The single-shot path: a converted message becomes the
    whole stream an upstream that ignored ``stream`` still owes."""
    message = openai_to_anthropic(
        {
            "model": "m",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "hi"},
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }
    )
    events = events_from_message(message)
    assert [event["type"] for event in events] == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[3]["delta"] == {"type": "text_delta", "text": "hi"}
    assert events[-2]["usage"] == {
        "input_tokens": 2,
        "output_tokens": 1,
    }


# --- the /v1/messages route (#765) ----------------------------------------


def anthropic_upstream_handler(request: httpx.Request) -> httpx.Response:
    """The emulated Messages upstream: answers the request's own
    body under an ``echo`` key, records the headers the route
    built, and streams SSE lines when the body asked to."""
    seen: dict = {}
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": []})
    body = json.loads(request.content.decode())
    if body.get("stream"):
        return httpx.Response(
            200,
            content=(
                b"event: message_start\n"
                b'data: {"type": "message_start"}\n\n'
                b"event: message_stop\n"
                b'data: {"type": "message_stop"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )
    seen["body"] = body
    seen["x-api-key"] = request.headers.get("x-api-key")
    seen["anthropic-version"] = request.headers.get("anthropic-version")
    seen["anthropic-beta"] = request.headers.get("anthropic-beta")
    return httpx.Response(200, json={"echo": seen})


async def test_messages_endpoint_forwards_verbatim_in_passthrough() -> None:
    """Passthrough mode: the body reaches the upstream verbatim
    (routing fields included — the upstream's own wire shape is
    the contract), the entry's key rides as x-api-key, and the
    version and beta headers forward with a default when absent."""
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(anthropic_upstream_handler),
        timeout=timeout,
    )
    proxy.router.ensure(proxy.app.state.settings)
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post("/v1/messages", headers=AUTH, json=body)
        versioned = await http.post(
            "/v1/messages",
            headers={
                **AUTH,
                "anthropic-version": "2024-01-01",
                "anthropic-beta": "context-management-2025-06-27",
            },
            json=body,
        )
    assert reply.status_code == 200
    echo = reply.json()["echo"]
    assert echo["body"] == body
    assert echo["x-api-key"] == "sk-x"
    assert echo["anthropic-version"] == "2023-06-01"
    assert echo["anthropic-beta"] is None
    sent = versioned.json()["echo"]
    assert sent["anthropic-version"] == "2024-01-01"
    assert sent["anthropic-beta"] == "context-management-2025-06-27"


async def test_messages_endpoint_streams_passthrough_sse() -> None:
    """A streaming Messages body in passthrough mode: the SSE
    lines come home as they arrived."""
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(anthropic_upstream_handler),
        timeout=timeout,
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/messages",
            headers=AUTH,
            json={"model": "m", "messages": [], "stream": True},
        )
    assert reply.status_code == 200
    assert reply.text == (
        "event: message_start\n"
        'data: {"type": "message_start"}\n\n'
        "event: message_stop\n"
        'data: {"type": "message_stop"}\n\n'
    )


async def test_messages_endpoint_answers_503_unconfigured() -> None:
    proxy = proxy_under_test()
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/messages", headers=AUTH, json={"messages": []}
        )
    assert reply.status_code == 503


def fake_router_proxy(settings: Settings, fake) -> LlmProxy:
    """A proxy whose router-mode answering half is a fake: the
    ensure seam freezes (the settings object latches, no rebuild
    runs), so the fake stays installed through the request path's
    own configure check."""
    proxy = proxy_under_test(settings=settings)

    async def frozen(settings) -> None:
        proxy.router._settings = settings

    proxy.router._router = fake
    proxy.router.ensure_async = frozen
    return proxy


async def test_messages_endpoint_translates_in_router_mode() -> None:
    """Router mode: the Messages body translates to completion
    kwargs (a guest's routing fields never reach the router), and
    the OpenAI answer comes home as a Messages message."""
    fake = FakeLitellmRouter(
        answer={
            "id": "chatcmpl-9",
            "model": "gpt-x",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "Trenton is sunny."},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
    )
    proxy = fake_router_proxy(llm_settings(("openai/gpt-x",)), fake)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/messages",
            headers=AUTH,
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 64,
                "system": "Be terse.",
                "messages": [
                    {"role": "user", "content": "Weather in Trenton?"}
                ],
                "api_key": "sk-guess",
                "api_base": "http://attacker/v1",
            },
        )
    assert reply.status_code == 200
    message = reply.json()
    assert message["type"] == "message"
    assert message["content"] == [
        {"type": "text", "text": "Trenton is sunny."}
    ]
    assert message["stop_reason"] == "end_turn"
    assert message["usage"] == {"input_tokens": 5, "output_tokens": 3}
    call = fake.calls[0]
    assert call["model"] == "one"  # the router's default took the name
    assert call["messages"][0] == {"role": "system", "content": "Be terse."}
    assert "api_key" not in call
    assert "api_base" not in call


async def test_messages_endpoint_streams_router_mode_events() -> None:
    """Router mode with stream set: the chunk stream renders as
    Anthropic events; an upstream that answers a streaming request
    without a stream still gets the full event sequence."""
    fake = FakeLitellmRouter(
        chunks=[
            {
                "model": "gpt-x",
                "choices": [{"delta": {"content": "Hel"}}],
            },
            {
                "choices": [
                    {"delta": {"content": "lo"}, "finish_reason": "stop"}
                ]
            },
        ]
    )
    proxy = fake_router_proxy(llm_settings(("openai/gpt-x",)), fake)
    async with async_client(proxy, "10.0.0.1") as http:
        stream = await http.post(
            "/v1/messages",
            headers=AUTH,
            json={
                "model": "m",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert stream.status_code == 200
    kinds = [
        json.loads(line[len("data: ") :])["type"]
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert kinds == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]

    single = FakeLitellmRouter(
        answer={
            "model": "gpt-x",
            "choices": [
                {"finish_reason": "stop", "message": {"content": "one shot"}}
            ],
        }
    )
    proxy.router._router = single
    async with async_client(proxy, "10.0.0.1") as http:
        shot = await http.post(
            "/v1/messages",
            headers=AUTH,
            json={
                "model": "m",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert shot.status_code == 200
    assert '"text": "one shot"' in shot.text
    assert shot.text.rstrip().endswith('data: {"type": "message_stop"}')


async def test_messages_endpoint_maps_upstream_failure() -> None:
    """A failing Messages upstream answers the Messages error
    shape at 502, in both modes."""
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(down), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/messages",
            headers=AUTH,
            json={"model": "m", "messages": []},
        )
    assert reply.status_code == 502
    assert reply.json() == {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": "LLM upstream request failed",
        },
    }


# --- the emulated-Anthropic-API smoke test (#765) -------------------------


class AnthropicEmulator(BaseHTTPRequestHandler):
    """An in-process Anthropic Messages API, scripted per request:
    the conversation's shape picks the answer, so a claude-code
    shaped client loop (text, tool round-trip, stream) runs
    against real sockets end to end."""

    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(
            {
                "path": self.path,
                "x-api-key": self.headers.get("x-api-key"),
                "anthropic-version": self.headers.get("anthropic-version"),
                "body": body,
            }
        )
        if body.get("stream"):
            events = [
                {"type": "message_start", "message": {"model": body["model"]}},
                {"type": "ping"},
                {"type": "content_block_start", "index": 0},
                {"type": "content_block_delta", "index": 0},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta"},
                {"type": "message_stop"},
            ]
            payload = "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        message = self.scripted_answer(body)
        payload = json.dumps(message).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def scripted_answer(self, body: dict) -> dict:
        """The emulated model: a tool round-trip answers a
        tool_use then the result's text, everything else answers
        plain text."""
        kinds = [
            block.get("type")
            for message in body["messages"]
            for block in (
                message["content"]
                if isinstance(message["content"], list)
                else []
            )
        ]
        if "tool_result" in kinds:
            content: list[dict] = [
                {"type": "text", "text": "Sunny in Trenton"}
            ]
            stop = "end_turn"
        elif body.get("tools"):
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_smoke",
                    "name": "get_weather",
                    "input": {"city": "Trenton"},
                }
            ]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "The answer is 42"}]
            stop = "end_turn"
        return {
            "id": "msg_emulated",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    def log_message(self, format: str, *args) -> None:
        """Quiet: the emulator's traffic is the test's own
        assertions, not noise for the console."""


async def test_smoke_anthropic_conversation_through_the_proxy() -> None:
    """claude-code's own loop, end to end over real sockets: an
    emulated Anthropic API serves the upstream, the proxy forwards
    to it from its /v1/messages route, and a three-turn
    conversation (text, tool round-trip, streamed text) completes
    with the wire shapes and the credential headers intact."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicEmulator)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        proxy = proxy_under_test(
            settings=llm_settings((f"*:http://127.0.0.1:{port}/v1:sk-up",)),
        )
        proxy.router.ensure(proxy.app.state.settings)
        seen: list[dict] = []
        async with async_client(proxy, "10.0.0.1") as http:
            # Turn one: plain text, the request claude-code sends
            # (its own headers: version, beta list, bearer token).
            first = await http.post(
                "/v1/messages",
                headers={
                    **AUTH,
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 256,
                    "system": "You are terse.",
                    "messages": [
                        {"role": "user", "content": "What is the answer?"}
                    ],
                },
            )
            assert first.status_code == 200
            message = first.json()
            assert message["type"] == "message"
            assert message["content"] == [
                {"type": "text", "text": "The answer is 42"}
            ]
            assert message["stop_reason"] == "end_turn"

            # Turn two: a tool definition answers a tool_use.
            tools = [
                {
                    "name": "get_weather",
                    "description": "Weather for a city.",
                    "input_schema": {"type": "object"},
                }
            ]
            second = await http.post(
                "/v1/messages",
                headers=AUTH,
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 256,
                    "tools": tools,
                    "messages": [
                        {"role": "user", "content": "Weather in Trenton?"}
                    ],
                },
            )
            assert second.status_code == 200
            use = second.json()["content"][0]
            assert use["type"] == "tool_use"
            assert use["name"] == "get_weather"
            assert use["input"] == {"city": "Trenton"}
            assert second.json()["stop_reason"] == "tool_use"

            # Turn three: the client's tool result comes back as
            # text, and the streamed answer arrives as events.
            third = await http.post(
                "/v1/messages",
                headers=AUTH,
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 256,
                    "stream": True,
                    "tools": tools,
                    "messages": [
                        {"role": "user", "content": "Weather in Trenton?"},
                        {
                            "role": "assistant",
                            "content": [second.json()["content"][0]],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": use["id"],
                                    "content": "Sunny",
                                }
                            ],
                        },
                    ],
                },
            )
            assert third.status_code == 200
            events = [
                json.loads(line[len("data: ") :])
                for line in third.text.splitlines()
                if line.startswith("data: ")
            ]
            assert [event["type"] for event in events] == [
                "message_start",
                "ping",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ]
            assert events[0]["message"]["model"] == "claude-sonnet-4-5"
        seen = AnthropicEmulator.seen
        # Every upstream request kept the Messages contract: the
        # path, the entry's key, and the version (defaulted when
        # the client sent none).
        assert [request["path"] for request in seen] == [
            "/v1/messages",
            "/v1/messages",
            "/v1/messages",
        ]
        assert {request["x-api-key"] for request in seen} == {"sk-up"}
        assert seen[0]["anthropic-version"] == "2023-06-01"
        assert seen[1]["anthropic-version"] == "2023-06-01"
        # The bodies arrived verbatim — the emulation's own
        # scripted answers read the guest's fields.
        assert seen[0]["body"]["system"] == "You are terse."
        assert seen[2]["body"]["stream"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_conversion_survives_malformed_guest_shapes() -> None:
    """The converter's guards: a non-dict message, a non-dict
    block, a non-dict tool, a tool_result without content, and a
    system prompt that is neither string nor blocks each cost
    their own entry — the request converts without them."""
    kwargs = anthropic_to_openai(
        {
            "system": 42,
            "messages": [
                "not a message",
                {
                    "role": "user",
                    "content": [
                        "not a block",
                        {"type": "tool_result", "tool_use_id": "t"},
                        {"type": "text", "text": "real"},
                    ],
                },
            ],
            "tools": ["not a tool", {"name": "real", "input_schema": {}}],
        }
    )
    assert kwargs["messages"] == [
        {"role": "tool", "tool_call_id": "t", "content": ""},
        {"role": "user", "content": [{"type": "text", "text": "real"}]},
    ]
    assert [tool["function"]["name"] for tool in kwargs["tools"]] == ["real"]


def test_conversion_covers_the_assistant_text_turn() -> None:
    """An assistant turn without tool calls keeps its text as its
    own message."""
    kwargs = anthropic_to_openai(
        {
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                },
            ]
        }
    )
    assert kwargs["messages"][1] == {
        "role": "assistant",
        "content": "hello",
    }


def test_answer_conversion_guards_its_shapes() -> None:
    """The answer-side guards: an empty string content beside tool
    calls, a parts list with non-text parts, and a non-dict tool
    call each skip without breaking the message."""
    message = openai_to_anthropic(
        {
            "model": "m",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            "not a call",
                            {
                                "id": "c",
                                "function": {"name": "f", "arguments": "{}"},
                            },
                        ],
                    },
                }
            ],
        }
    )
    assert message["content"] == [
        {"type": "tool_use", "id": "c", "name": "f", "input": {}}
    ]
    parts = openai_to_anthropic(
        {
            "model": "m",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": [
                            "plain",
                            {"type": "image_url", "image_url": {"url": "x"}},
                            {"type": "text", "text": "kept"},
                        ]
                    },
                }
            ],
        }
    )
    assert parts["content"] == [{"type": "text", "text": "kept"}]


def test_stream_feed_ignores_a_non_dict_usage() -> None:
    """A chunk whose usage is not a mapping records nothing and
    converts its events as usual."""
    stream = AnthropicStream()
    events = stream.feed(
        {"model": "m", "usage": 5, "choices": [{"delta": {"content": "x"}}]}
    )
    assert events[0]["type"] == "message_start"
    assert events[0]["message"]["usage"]["input_tokens"] == 0
    assert events[-1]["delta"] == {"type": "text_delta", "text": "x"}


def test_events_from_message_covers_the_tool_use_block() -> None:
    """The single-shot sequence renders a tool_use block's input
    as one json delta."""
    message = openai_to_anthropic(
        {
            "model": "m",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c",
                                "function": {
                                    "name": "f",
                                    "arguments": '{"a": 1}',
                                },
                            }
                        ]
                    },
                }
            ],
        }
    )
    events = events_from_message(message)
    delta = events[3]
    assert delta["delta"] == {
        "type": "input_json_delta",
        "partial_json": '{"a": 1}',
    }


async def test_messages_endpoint_without_an_upstream_key_sends_none() -> None:
    """A passthrough entry with no key forwards the Messages
    request with no x-api-key header (the upstream serves an
    unauthenticated gateway)."""
    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(anthropic_upstream_handler),
        timeout=timeout,
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/messages",
            headers=AUTH,
            json={"model": "m", "messages": []},
        )
    assert reply.status_code == 200
    assert reply.json()["echo"]["x-api-key"] is None


async def test_messages_endpoint_refuses_malformed_bodies() -> None:
    """The body caps answer on the Messages route too: a
    non-JSON body answers 400, an announced oversize answers
    413."""
    proxy = proxy_under_test()
    async with async_client(proxy, "10.0.0.1") as http:
        bad = await http.post(
            "/v1/messages", headers=AUTH, content=b"not json"
        )
        assert bad.status_code == 400
        oversize = await http.post(
            "/v1/messages",
            headers={**AUTH, "content-length": str(17 * 1024 * 1024)},
            json={"model": "m", "messages": []},
        )
        assert oversize.status_code == 413


async def test_messages_endpoint_maps_a_streaming_upstream_failure() -> None:
    """A Messages stream whose upstream fails answers the
    Messages error shape at 502 (the streamed half of the
    passthrough error path)."""

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    proxy = proxy_under_test(
        settings=llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(down), timeout=timeout
    )
    proxy.router.ensure(proxy.app.state.settings)
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/messages",
            headers=AUTH,
            json={"model": "m", "messages": [], "stream": True},
        )
    assert reply.status_code == 502
    assert reply.json()["error"]["type"] == "api_error"
