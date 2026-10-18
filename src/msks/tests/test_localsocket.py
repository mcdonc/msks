"""The local listener (#555): socket vocabulary, peer-credential
policy, the auth seams, and one end-to-end pass over a real
unix socket — a kernel that answers SO_PEERCRED, uvicorn's
wrapped protocols, httpx's dial, and the websockets handshake
without a token."""

import asyncio
import contextlib
import os
import socket as socketlib
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket
from msks.server import localsocket, main
from msks.server.api.deps import authed_accept, require_token
from msks.spec.sockets import (
    default_socket_path,
    find_default_socket,
    is_socket_file,
    socket_candidates,
)

# --- the socket vocabulary -----------------------------------------------


def test_default_path_prefers_the_user_runtime_dir(monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert default_socket_path() == Path("/run/user/1234/msks/msksd.sock")


def test_default_path_without_xdg_is_no_socket(monkeypatch) -> None:
    """A process with no runtime root (a container, a system
    unit) binds no default socket — the TCP surface serves alone
    until MSKSD_SOCKET names a path."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_socket_path() is None


def test_candidates_are_the_own_runtime_dir_alone(monkeypatch) -> None:
    """The client looks where the same-user daemon bound: its own
    runtime root. A socket anywhere else is a deployment choice
    the client names explicitly, never guesses at."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert socket_candidates() == [Path("/run/user/1234/msks/msksd.sock")]
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert socket_candidates() == []


def test_candidates_follow_xdg_pointed_at_run(monkeypatch) -> None:
    """A host whose XDG_RUNTIME_DIR is /run itself still resolves
    one candidate, /run/msks/msksd.sock."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run")
    assert socket_candidates() == [Path("/run/msks/msksd.sock")]


def test_find_default_socket_skips_non_sockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_run = tmp_path / "runtime"
    (user_run / "msks").mkdir(parents=True)
    (user_run / "msks" / "msksd.sock").write_text("not a socket")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(user_run))
    assert find_default_socket() is None
    assert is_socket_file(user_run / "msks" / "msksd.sock") is False
    # A path that does not stat (absent, unreadable parent) takes
    # the OSError arm, not the mode test.
    assert is_socket_file(user_run / "msks" / "absent.sock") is False


def test_find_default_socket_answers_a_real_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_run = tmp_path / "runtime"
    path = user_run / "msks" / "msksd.sock"
    sock = localsocket.bind(path)
    try:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(user_run))
        assert find_default_socket() == path
    finally:
        sock.close()
        localsocket.unlink(path)


# --- bind / unlink --------------------------------------------------------


def test_bind_creates_the_directory_and_a_socket_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "msks" / "msksd.sock"
    sock = localsocket.bind(path)
    try:
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert path.stat().st_mode & 0o660 == 0o660
    finally:
        sock.close()
        localsocket.unlink(path)
    assert not path.exists()


def test_bind_replaces_a_stale_socket(tmp_path: Path) -> None:
    path = tmp_path / "msksd.sock"
    stale = localsocket.bind(path)
    stale.close()
    fresh = localsocket.bind(path)
    try:
        assert stat.S_ISSOCK(path.stat().st_mode)
    finally:
        fresh.close()
        localsocket.unlink(path)


def test_bind_refuses_a_non_socket_file(tmp_path: Path) -> None:
    path = tmp_path / "occupied"
    path.write_text("a config file, not a socket")
    with pytest.raises(OSError, match="not a socket"):
        localsocket.bind(path)


# --- peer credentials and policy -----------------------------------------


class FakeTransport:
    """A transport whose extra info answers a real socketpair."""

    def __init__(self, sock: socketlib.socket) -> None:
        self.sock = sock

    def get_extra_info(self, name: str):
        return self.sock if name == "socket" else None


def test_peer_credentials_answer_over_a_socketpair() -> None:
    ours, theirs = socketlib.socketpair()
    try:
        seen = localsocket.peer_credentials(FakeTransport(ours))
        assert seen == os.geteuid()
    finally:
        ours.close()
        theirs.close()


def test_peer_credentials_refuse_a_tcp_transport() -> None:
    sock = socketlib.socket(socketlib.AF_INET, socketlib.SOCK_STREAM)
    try:
        assert localsocket.peer_credentials(FakeTransport(sock)) is None
    finally:
        sock.close()


def test_peer_credentials_refuse_a_socketless_transport() -> None:
    assert localsocket.peer_credentials(FakeTransport(None)) is None


def test_policy_passes_the_daemon_uid() -> None:
    assert localsocket.peer_allowed(os.geteuid(), None) is True


def test_policy_reads_the_socket_group_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    localsocket._in_group.cache_clear()
    monkeypatch.setattr(
        localsocket.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="alice", pw_gid=1001),
    )
    monkeypatch.setattr(
        localsocket.grp,
        "getgrgid",
        lambda gid: SimpleNamespace(gr_mem=["alice", "bob"]),
    )
    assert localsocket.peer_allowed(4242, 54321) is True
    localsocket._in_group.cache_clear()
    monkeypatch.setattr(
        localsocket.grp,
        "getgrgid",
        lambda gid: SimpleNamespace(gr_mem=["bob"]),
    )
    assert localsocket.peer_allowed(4242, 54321) is False


def test_policy_reads_the_socket_group_as_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        localsocket.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="alice", pw_gid=54321),
    )
    assert localsocket.peer_allowed(4242, 54321) is True


def test_policy_denies_an_unknown_uid_without_a_socket_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(localsocket.pwd, "getpwuid", raises_keyerror)
    assert localsocket.peer_allowed(4242, None) is False


def raises_keyerror(uid):
    raise KeyError(uid)


def test_is_unix_scope_reads_the_marker() -> None:
    assert localsocket.is_unix_scope({"client": ("unix", 1000)}) == 1000
    assert localsocket.is_unix_scope({"client": ("127.0.0.1", 5)}) is None
    non_int = localsocket.is_unix_scope({"client": ("unix", "not-an-int")})
    assert non_int is None
    assert localsocket.is_unix_scope({}) is None


# --- the auth seams -------------------------------------------------------


class FakeModel:
    async def token_valid(self, token: str) -> bool:
        return token == "good"


def fake_msks_app(gid: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            model=FakeModel(),
            **({"unix_socket_gid": gid} if gid is not None else {}),
        )
    )


def http_scope(client, headers: list[tuple[str, str]] | None = None):
    # The scope's app is the FastAPI api, whose state carries the
    # msks app the way build_api mounts it.
    api = SimpleNamespace(
        state=SimpleNamespace(msks_app=fake_msks_app(os.getgid()))
    )
    return {
        "type": "http",
        "client": client,
        "headers": [
            (name.encode(), value.encode()) for name, value in (headers or [])
        ],
        "app": api,
    }


async def test_require_token_passes_a_local_peer() -> None:
    request = Request(http_scope(("unix", os.geteuid())))
    await require_token(request)  # no raise: the kernel named this peer


async def test_require_token_falls_to_the_bearer_path() -> None:
    request = Request(
        http_scope(
            ("unix", os.geteuid() + 1),
            headers=[("authorization", "Bearer good")],
        )
    )
    await require_token(request)  # a denied peer, a valid token


async def test_require_token_refuses_a_denied_peer_without_a_token() -> None:
    import fastapi

    request = Request(http_scope(("unix", os.geteuid() + 1)))
    with pytest.raises(fastapi.HTTPException) as raised:
        await require_token(request)
    assert raised.value.status_code == 401


# --- the listener's uvicorn config ---------------------------------------


def test_enabled_maps_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert localsocket.enabled(None) is None
    assert localsocket.enabled("") is None
    assert localsocket.enabled("/custom/sock") == Path("/custom/sock")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert localsocket.enabled(None) == Path("/run/user/1234/msks/msksd.sock")


def test_socket_config_serves_plain_with_no_lifespan() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                server=SimpleNamespace(port=8660, access_log=False)
            )
        )
    )
    config = main.socket_config(app, api=FastAPI())
    assert config.lifespan == "off"
    assert config.ssl_certfile is None


def test_wrap_protocols_subclasses_uvicons_own() -> None:
    config = uvicorn.Config(FastAPI(), lifespan="off")
    localsocket.wrap_protocols(config)
    http_base = config.http_protocol_class.__bases__[0]
    ws_base = config.ws_protocol_class.__bases__[0]
    assert http_base.__module__.startswith("uvicorn.protocols")
    assert ws_base.__module__.startswith("uvicorn.protocols")


# --- end to end over a real socket ----------------------------------------


async def serve_over_socket(tmp_path: Path, api: FastAPI):
    """One QuietServer over a bound socket; returns (server, path,
    task) for the caller to stop."""
    path = tmp_path / "msksd.sock"
    sock = localsocket.bind(path)
    config = uvicorn.Config(api, lifespan="off", port=0)
    localsocket.wrap_protocols(config)
    server = localsocket.QuietServer(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            return server, path, task
        await asyncio.sleep(0.05)
    raise AssertionError("the socket server never started")


async def test_the_listener_serves_and_names_the_peer(
    tmp_path: Path,
) -> None:
    """One REST exchange over the socket: the peer's uid rides the
    scope, and a route without a token answers — the same-process
    uid is the daemon's own, the policy's free pass."""
    from fastapi import Request as FastApiRequest

    api = FastAPI()
    api.state.msks_app = fake_msks_app(os.getgid())

    @api.get("/who")
    async def who(request: FastApiRequest):
        client = request.scope.get("client")
        return {"client": list(client) if client else None}

    server, path, task = await serve_over_socket(tmp_path, api)
    try:
        async with httpx.AsyncClient(
            base_url="http://unix",
            transport=httpx.AsyncHTTPTransport(uds=str(path)),
        ) as client:
            row = (await client.get("/who")).json()
        assert row["client"] == ["unix", os.geteuid()]
    finally:
        server.should_exit = True
        await task
        localsocket.unlink(path)


async def test_the_listener_authenticates_a_websocket_by_peer(
    tmp_path: Path,
) -> None:
    """The websocket handshake with no Authorization header: the
    peer credential answers it."""
    api = FastAPI()
    fake_app = fake_msks_app(os.getgid())

    @api.websocket("/stream")
    async def stream(websocket: WebSocket):
        if await authed_accept(fake_app, websocket):
            await websocket.send_text("hello")
            await websocket.close()

    server, path, task = await serve_over_socket(tmp_path, api)
    try:
        async with websockets.unix_connect(
            str(path), uri="ws://unix/stream"
        ) as ws:
            assert await ws.recv() == "hello"
    finally:
        server.should_exit = True
        await task
        localsocket.unlink(path)


def test_platform_supported_on_linux() -> None:
    assert localsocket.platform_supported() is True


# --- serve_listeners: the two-listener orchestration --------------------


def fake_listener_app(
    monkeypatch: pytest.MonkeyPatch, port: int, socket_setting
):
    """An app whose api is a bare FastAPI: serve_listeners' own
    orchestration is the test subject, not the daemon behind it."""
    from types import SimpleNamespace

    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                server=SimpleNamespace(
                    host="127.0.0.1",
                    port=port,
                    socket=socket_setting,
                    access_log=False,
                    tls_cert=None,
                    tls_key=None,
                )
            )
        )
    )
    api = FastAPI()
    monkeypatch.setattr(main, "build_api", lambda app: api)
    return app


def occupy_a_port() -> tuple[socketlib.socket, int]:
    sock = socketlib.socket(socketlib.AF_INET, socketlib.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


async def test_serve_listeners_cleans_the_socket_when_tcp_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TCP bind that cannot happen (the port is taken) ends the
    run with the socket file removed — a restart binds cleanly."""
    holder, port = occupy_a_port()
    path = tmp_path / "msksd.sock"
    app = fake_listener_app(monkeypatch, port, str(path))
    try:
        with pytest.raises((SystemExit, asyncio.CancelledError)):
            await main.serve_listeners(app)
    finally:
        holder.close()
    assert not path.exists()


async def test_serve_listeners_without_a_socket_serves_tcp_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-string setting disables the listener: no socket
    file appears beside the (failing) TCP bind."""
    holder, port = occupy_a_port()
    path = tmp_path / "msksd.sock"
    app = fake_listener_app(monkeypatch, port, "")
    try:
        with pytest.raises((SystemExit, asyncio.CancelledError)):
            await main.serve_listeners(app)
    finally:
        holder.close()
    assert not path.exists()


def test_peer_credentials_refuse_a_socket_that_will_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Refusing:
        family = socketlib.AF_UNIX

        def get_extra_info(self, name: str):
            return self if name == "socket" else None

        def getsockopt(self, *args):
            raise OSError("refused")

    assert localsocket.peer_credentials(Refusing()) is None


def test_bind_failure_closes_the_socket(tmp_path: Path) -> None:
    """A bind the kernel refuses (a sun_path longer than the
    kernel's 108 bytes) closes the socket it made — no fd leaks
    out of a failed listener."""
    too_long = tmp_path / ("x" * 120)
    with pytest.raises(OSError):
        localsocket.bind(too_long)


# --- the client's address and dial vocabulary -----------------------------


def test_unix_url_round_trips_the_path() -> None:
    from msks.client.env import unix_path, unix_url

    address = unix_url("/run/msks/msksd.sock")
    assert address == "http+unix://%2Frun%2Fmsks%2Fmsksd.sock"
    assert unix_path(address) == "/run/msks/msksd.sock"


def test_env_url_takes_the_socket_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from msks.client.env import env_url

    monkeypatch.setenv("MSKSC_URL", "")
    monkeypatch.setenv("MSKSC_SOCKET", "/run/msks/msksd.sock")
    assert env_url() == "http+unix://%2Frun%2Fmsks%2Fmsksd.sock"


def test_env_url_finds_the_default_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from msks.client import env

    path = tmp_path / "msksd.sock"
    sock = localsocket.bind(path)
    try:
        monkeypatch.setattr(env, "find_default_socket", lambda: path)
        monkeypatch.setenv("MSKSC_URL", "")
        monkeypatch.setenv("MSKSC_SOCKET", "")
        assert env.env_url() == env.unix_url(str(path))
    finally:
        sock.close()
        localsocket.unlink(path)


def test_env_token_is_none_over_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from msks.client.env import env_token

    monkeypatch.setenv("MSKSC_URL", "")
    monkeypatch.setenv("MSKSC_SOCKET", "/run/msks/msksd.sock")
    assert env_token() is None


def test_ws_url_forms() -> None:
    from msks.client.wsdial import ws_url

    assert ws_url("https://h:1", "/p") == "wss://h:1/p"
    assert ws_url("http://h:1", "/p") == "ws://h:1/p"
    assert ws_url("http+unix://%2Frun%2Fs", "/p") == "ws+unix://%2Frun%2Fs/p"
    assert ws_url("bare.example", "/p") == "wss://bare.example/p"


def test_unix_parts_splits_the_address() -> None:
    from msks.client.wsdial import unix_parts

    sock, path = unix_parts("ws+unix://%2Frun%2Fmsks%2Fmsksd.sock/api/v1/x")
    assert sock == "/run/msks/msksd.sock"
    assert path == "/api/v1/x"


def test_dial_takes_the_unix_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from msks.client import wsdial

    seen = {}

    def fake_unix_connect(path, uri=None, **kwargs):
        seen["path"], seen["uri"], seen["kwargs"] = path, uri, kwargs
        return "connection"

    monkeypatch.setattr(wsdial.websockets, "unix_connect", fake_unix_connect)
    out = wsdial.dial(
        "ws+unix://%2Frun%2Fmsks%2Fmsksd.sock/console", None, None, 2**22
    )
    assert out == "connection"
    assert seen["path"] == "/run/msks/msksd.sock"
    assert seen["uri"] == "ws://unix/console"
    assert seen["kwargs"]["additional_headers"] == []


def test_api_client_dials_the_socket_without_a_token() -> None:
    from msks.client.env import unix_url
    from msks.client.rest import api_client

    client = api_client(unix_url("/run/msks/msksd.sock"), None)
    assert client.base_url == "http://unix"
    assert "Authorization" not in client.headers


async def test_serve_listeners_serves_both_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole shape on a working daemon: TCP binds, the socket
    answers beside it, and cancelling the run removes the socket
    file."""
    api = FastAPI()
    api.state.msks_app = fake_msks_app(os.getgid())

    @api.get("/who")
    async def who(request: Request):
        return {"client": list(request.scope.get("client") or [])}

    app = fake_listener_app(monkeypatch, 0, str(tmp_path / "msksd.sock"))
    monkeypatch.setattr(main, "build_api", lambda app: api)
    path = tmp_path / "msksd.sock"
    task = asyncio.create_task(main.serve_listeners(app))
    deadline = asyncio.get_event_loop().time() + 10
    while not path.exists() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert path.exists(), "the local listener never bound"
    async with httpx.AsyncClient(
        base_url="http://unix",
        transport=httpx.AsyncHTTPTransport(uds=str(path)),
    ) as client:
        row = (await client.get("/who")).json()
    assert row["client"] == ["unix", os.geteuid()]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert not path.exists()


def test_wrap_protocols_is_idempotent_over_a_loaded_config() -> None:
    config = uvicorn.Config(FastAPI(), lifespan="off")
    localsocket.wrap_protocols(config)
    wrapped = config.http_protocol_class
    localsocket.wrap_protocols(config)  # already loaded: load is skipped
    assert config.http_protocol_class is not wrapped


def test_group_membership_answers_false_for_an_unknown_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(gid):
        raise KeyError(gid)

    monkeypatch.setattr(localsocket.grp, "getgrgid", missing)
    localsocket._in_group.cache_clear()
    assert localsocket._in_group("alice", 99999) is False
    localsocket._in_group.cache_clear()


def test_wrap_protocols_skips_a_ws_disabled_config() -> None:
    config = uvicorn.Config(FastAPI(), lifespan="off", ws="none")
    localsocket.wrap_protocols(config)
    assert config.ws_protocol_class is None


# --- review round 1: the proxy-header forgery and the live socket ----


def test_both_listener_configs_refuse_proxy_headers() -> None:
    """uvicorn's proxy-header middleware rewrites scope["client"]
    from X-Forwarded-For on a TCP connection from 127.0.0.1 — a
    forged header reached the local-peer marker with it on (#555
    review). No proxy fronts msksd: both listeners refuse the
    middleware outright."""
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                server=SimpleNamespace(
                    host="127.0.0.1",
                    port=8660,
                    socket=None,
                    access_log=False,
                    tls_cert=None,
                    tls_key=None,
                )
            )
        )
    )
    assert main.server_config(app, api=FastAPI()).proxy_headers is False
    assert main.socket_config(app, api=FastAPI()).proxy_headers is False


def test_bind_refuses_a_live_daemons_socket(tmp_path: Path) -> None:
    """A second daemon must not steal a first's listener: the bind
    probes the existing socket and refuses when someone answers
    (#555 review)."""
    path = tmp_path / "msksd.sock"
    live = localsocket.bind(path)
    try:
        with pytest.raises(OSError, match="already listening"):
            localsocket.bind(path)
    finally:
        live.close()
        localsocket.unlink(path)


def test_a_socket_setting_survives_the_env_reader() -> None:
    """MSKSD_SOCKET="" disables the listener (#555 review): the
    settings read keeps the empty string instead of collapsing it
    into the default-path bind."""
    from msks.settings import ServerSettings

    settings = ServerSettings.from_env({"MSKSD_SOCKET": ""})
    assert settings.socket == ""
    assert localsocket.enabled(settings.socket) is None


async def test_a_forged_forwarded_header_cannot_mark_a_tcp_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy-header forgery, pinned as behavior (#555 review
    round 2): a TCP request carrying X-Forwarded-For: unix must
    leave scope["client"] the kernel's address pair, not the
    local-listener marker — whatever a future uvicorn grows, this
    is the regression that must not return."""
    from fastapi import Request as FastApiRequest

    api = FastAPI()

    @api.get("/who")
    async def who(request: FastApiRequest):
        client = request.scope.get("client")
        return {"client": list(client) if client else None}

    app = fake_listener_app(monkeypatch, 0, "")
    monkeypatch.setattr(main, "build_api", lambda app: api)
    config = main.server_config(app, api=api)
    localsocket.wrap_protocols(
        config
    )  # the socket-side wrap must not leak here
    server = localsocket.QuietServer(config)
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_event_loop().time() + 10
    while not server.started and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert server.started
    try:
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            row = (
                await c.get("/who", headers={"X-Forwarded-For": "unix"})
            ).json()
        assert row["client"][0] != localsocket.SCOPE_KIND
    finally:
        server.should_exit = True
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, timeout=10)
