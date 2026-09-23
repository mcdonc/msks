"""Tests for the workspace LLM proxy (#259)."""

import asyncio
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from fastapi.responses import JSONResponse, StreamingResponse
from msks.app import build_app
from msks.llm import (
    LlmProxy,
    LlmRouter,
    TapListener,
    build_model_list,
    chat_fields,
    dispatch_completion,
    finish_aclose_task,
    is_passthrough,
    mint_token,
    parse_model_entry,
    resolve_indirection,
    spawn_aclose,
    split_entry,
    token_matches,
)
from msks.settings import LlmSettings, Settings

from msks import llm as llm_mod

TOKEN = "msksllm1_testtoken"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def llm_settings(
    models: tuple[str, ...] = (), api_key: str | None = None
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


async def test_litellm_mode_builds_a_real_router_and_defaults_the_model(
    tmp_path: Path,
) -> None:
    # The real litellm import (slow, paid once for the suite): two
    # entries, no wildcard — router mode.
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


# --- tokens ----------------------------------------------------------------


def test_minted_tokens_carry_the_prefix_and_are_unique() -> None:
    one, two = mint_token(), mint_token()
    assert one.startswith("msksllm1_")
    assert one != two


def test_token_matches_is_constant_and_refuses_absent() -> None:
    assert token_matches(TOKEN, TOKEN)
    assert not token_matches(TOKEN, None)
    assert not token_matches("other", TOKEN)


# --- the proxy app ---------------------------------------------------------


class StubModel:
    """The model surface the proxy reads: token rows by id."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    async def get_llm_token(self, ref: str) -> dict | None:
        return self.rows.get(ref)


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
    rows: dict[str, dict], settings: Settings | None = None
) -> LlmProxy:
    app = build_app(settings or llm_settings())
    app.state.model = StubModel(rows)
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


async def test_proxy_auth_rejects_everything_but_the_workspace_token() -> None:
    proxy = proxy_under_test({"ws1": {"id": "ws1", "llm_token": TOKEN}})
    # No token, a wrong token, a bare daemon token, an unknown tap:
    # one answer.
    async with async_client(proxy, "10.0.0.1") as http:
        assert (await http.get("/v1/models")).status_code == 401
        wrong = await http.get(
            "/v1/models", headers={"authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401
        bare = await http.get("/v1/models", headers={"authorization": TOKEN})
        assert bare.status_code == 401
    async with async_client(proxy, "10.9.9.9", mapped=False) as http:
        assert (await http.get("/v1/models", headers=AUTH)).status_code == 401


async def test_proxy_auth_rejects_a_row_without_a_token() -> None:
    proxy = proxy_under_test({"ws1": {"id": "ws1", "llm_token": None}})
    async with async_client(proxy, "10.0.0.1") as http:
        assert (await http.get("/v1/models", headers=AUTH)).status_code == 401


async def test_models_endpoint_answers_the_openai_shape() -> None:
    proxy = proxy_under_test(
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
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
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
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
    proxy = proxy_under_test({"ws1": {"id": "ws1", "llm_token": TOKEN}})
    async with async_client(proxy, "10.0.0.1") as http:
        reply = await http.post(
            "/v1/chat/completions", headers=AUTH, json={"x": 1}
        )
        assert reply.status_code == 503


async def test_completions_endpoint_streams_and_maps_upstream_failure() -> (
    None
):
    proxy = proxy_under_test(
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
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


async def test_tap_listener_serves_real_http_and_stops() -> None:
    proxy = proxy_under_test(
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    listener = TapListener(proxy.proxy_app, tap_ip="127.0.0.1", port=0)
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


async def test_listener_bind_failure_unregisters_the_mapping() -> None:
    proxy = proxy_under_test({"ws1": {"id": "ws1", "llm_token": TOKEN}})
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    listener = TapListener(proxy.proxy_app, tap_ip="127.0.0.1", port=port)
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
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
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

    attachment = SimpleNamespace(tap_ip="172.31.0.2")
    proxy = proxy_under_test({"ws1": {"id": "ws1", "llm_token": TOKEN}})
    assert proxy.listener_for(attachment) is None
    configured = proxy_under_test(
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    listener = configured.listener_for(attachment)
    assert isinstance(listener, TapListener)
    assert listener.tap_ip == "172.31.0.2"
    assert listener.port == 8770


async def test_tap_listener_accepts_a_prebound_socket() -> None:
    proxy = proxy_under_test(
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
    )
    proxy.router.client_factory = lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), timeout=timeout
    )
    listener = TapListener(proxy.proxy_app, tap_ip="127.0.0.1", port=0)
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
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
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


def test_token_matches_survives_non_ascii_headers() -> None:
    """HTTP header bytes are latin-1-decoded: a crafted bearer must
    answer 401, never the digest comparison's TypeError."""
    assert not token_matches("caf\xe9tok", "stored")
    assert not token_matches("ok", "caf\xe9stored")


async def test_malformed_and_oversized_bodies_answer_named_codes(
    monkeypatch,
) -> None:
    proxy = proxy_under_test(
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
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
        {"ws1": {"id": "ws1", "llm_token": TOKEN}},
        llm_settings(("*:http://up.stream/v1:sk-x",)),
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
