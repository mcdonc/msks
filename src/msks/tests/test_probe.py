"""Tests for the probe service (#424): the emulated external HTTPS
endpoint a workspace operator verifies secret interception with."""

import asyncio
import base64
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from msks.app import build_app
from msks.interceptor import ca
from msks.interceptor import probe as probe_mod
from msks.interceptor.probe import (
    PROBE_PASSWORD,
    PROBE_PORT,
    PROBE_USERNAME,
    build_probe_app,
    parse_basic_auth,
    service_material,
    upstream_bundle,
)
from msks.llm import TapListener
from msks.settings import Settings, VmmSettings
from msks.spec.probe import PROBE_SECRET_B64


def basic(username: str, password: str) -> str:
    """One Authorization header value from its pair."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


VALID = {"authorization": basic(PROBE_USERNAME, PROBE_PASSWORD)}


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_probe_app()),
        base_url="http://secretprobe.msks",
    )


async def test_valid_credentials_answer_ok(client: httpx.AsyncClient) -> None:
    reply = await client.get("/", headers=VALID)
    assert reply.status_code == 200
    assert reply.text == "ok"
    assert reply.headers["content-type"].startswith("text/plain")


async def test_wrong_answers_are_the_challenge(
    client: httpx.AsyncClient,
) -> None:
    """A wrong password, a wrong username, a missing header: one
    401 with the basic-auth challenge — the external-service
    shape."""
    wrong_password = await client.get(
        "/", headers={"authorization": basic("msks", "12346")}
    )
    assert wrong_password.status_code == 401
    assert wrong_password.headers["WWW-Authenticate"] == 'Basic realm="msks"'
    wrong_user = await client.get(
        "/", headers={"authorization": basic("other", PROBE_PASSWORD)}
    )
    assert wrong_user.status_code == 401
    missing = await client.get("/")
    assert missing.status_code == 401


async def test_malformed_credentials_answer_401_never_500(
    client: httpx.AsyncClient,
) -> None:
    """Base64 that does not decode, a pair with no colon, non-UTF-8
    bytes, a foreign scheme: all no credential at all."""
    undecodable = await client.get("/", headers={"authorization": "Basic !!!"})
    assert undecodable.status_code == 401
    colonless = base64.b64encode(b"nocolon").decode()
    no_colon = await client.get(
        "/", headers={"authorization": f"Basic {colonless}"}
    )
    assert no_colon.status_code == 401
    raw = base64.b64encode(b"msks:\xff\xfe").decode()
    non_utf8 = await client.get("/", headers={"authorization": f"Basic {raw}"})
    assert non_utf8.status_code == 401
    bearer = await client.get("/", headers={"authorization": "Bearer zzz"})
    assert bearer.status_code == 401


def test_parse_basic_auth_shapes() -> None:
    assert parse_basic_auth("basic bXNrczptc2tz") == ("msks", PROBE_PASSWORD)
    assert parse_basic_auth("BASIC bXNrczptc2tz") == (
        "msks",
        PROBE_PASSWORD,
    )
    # The first colon bounds the username; a password may hold more.
    assert parse_basic_auth(basic("u", "a:b")) == ("u", "a:b")
    assert parse_basic_auth("") is None
    assert parse_basic_auth("Basic ") is None
    assert parse_basic_auth("Basic not-base64!!") is None
    assert parse_basic_auth("Basic bnVsbA==") is None  # "null": no colon


def test_the_minted_secret_is_the_credential_blob() -> None:
    """The swap contract (#424): the placeholder's minted secret is
    the base64 of the whole credential, so the byte-level rewrite of
    the raw Basic blob lands as a well-formed header."""
    assert PROBE_SECRET_B64 == "bXNrczptc2tz"
    decoded = base64.b64decode(PROBE_SECRET_B64).decode()
    assert decoded == f"{PROBE_USERNAME}:{PROBE_PASSWORD}"


# --- the TLS material --------------------------------------------------------


def probe_settings(tmp_path: Path) -> Settings:
    return Settings(vmm=VmmSettings(state_dir=tmp_path / "state"))


def test_service_material_mints_once_and_reuses(tmp_path: Path) -> None:
    settings = probe_settings(tmp_path)
    first = service_material(settings)
    second = service_material(settings)
    assert first.cert == second.cert
    leaf = tmp_path / "state" / "probe" / "service.crt"
    assert leaf.is_file()
    cert = x509.load_pem_x509_certificate(leaf.read_bytes())
    assert "secretprobe.msks" in cert.subject.rfc4514_string()


def test_a_stale_leaf_remints(tmp_path: Path, monkeypatch) -> None:
    """A leaf close to expiry is replaced at the next material call
    (the service runs longer than one leaf's window)."""
    settings = probe_settings(tmp_path)
    # Mint the leaf at the epoch: its one-day window is long past,
    # so the next material call finds it stale and remints under
    # the real clock.
    from datetime import UTC, datetime

    monkeypatch.setattr(ca, "now_utc", lambda: datetime.fromtimestamp(0, UTC))
    service_material(settings)
    stale = (tmp_path / "state" / "probe" / "service.crt").read_bytes()
    monkeypatch.setattr(ca, "now_utc", datetime.now)
    service_material(settings)
    fresh = (tmp_path / "state" / "probe" / "service.crt").read_bytes()
    assert fresh != stale
    cert = x509.load_pem_x509_certificate(fresh)
    assert cert.not_valid_after_utc > datetime.now(UTC)


# --- the per-tap listener ----------------------------------------------------


@dataclass
class Attachment:
    """The NetAttachment surface the listener factory reads."""

    tap_ip: str
    workspace_id: str = "ws1"
    tap: str = "msks-tap"


def test_listener_for_binds_443_with_the_service_leaf(
    tmp_path: Path,
) -> None:
    app = build_app(probe_settings(tmp_path))
    certfile, keyfile = asyncio.run(app.state.probe.material())
    listener = app.state.probe.listener_for(
        Attachment("10.0.0.1"), certfile, keyfile
    )
    assert listener.tap_ip == "10.0.0.1"
    # The device pin rides every per-tap listener (#483).
    assert listener.tap == "msks-tap"
    assert listener.port == PROBE_PORT == 443
    assert listener._ssl_certfile == certfile
    assert listener._ssl_keyfile == keyfile


async def test_concurrent_material_calls_mint_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Two taps minting at once share one mint: the second caller
    waits on the lock and finds the first caller's material — the
    listener-start race at attach (#424)."""
    import threading

    app = build_app(probe_settings(tmp_path))
    server = app.state.probe
    entered = threading.Event()
    release = threading.Event()
    calls: list = []

    def held_material(settings):
        calls.append(1)
        entered.set()
        release.wait(5)
        return service_material(settings)

    monkeypatch.setattr(probe_mod, "service_material", held_material)

    async def held_call():
        # The first caller parks inside the mint until the second
        # is queued on the lock.
        await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(server.material())
        await asyncio.sleep(0)
        release.set()
        return await second

    first, second = await asyncio.gather(server.material(), held_call())
    assert first == second
    assert len(calls) == 1
    assert server._lock is not None
    # The memoized fast path: a third caller reads, never mints.
    assert await server.material() == first
    assert len(calls) == 1


async def test_concurrent_bundle_calls_mint_once(
    tmp_path: Path, monkeypatch
) -> None:
    """The trust bundle races the same way the material does (#424
    review): the second caller waits on the lock and reads the
    first's bundle — one file, one writer."""
    import threading

    app = build_app(probe_settings(tmp_path))
    server = app.state.probe
    entered = threading.Event()
    release = threading.Event()
    calls: list = []

    def held_bundle(settings):
        calls.append(1)
        entered.set()
        release.wait(5)
        return upstream_bundle(settings)

    monkeypatch.setattr(probe_mod, "upstream_bundle", held_bundle)

    async def held_call():
        await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(server.upstream_trust_bundle())
        await asyncio.sleep(0)
        release.set()
        return await second

    first, second = await asyncio.gather(
        server.upstream_trust_bundle(), held_call()
    )
    assert first == second
    assert len(calls) == 1
    # The memoized fast path: a third caller reads, never mints.
    assert await server.upstream_trust_bundle() == first
    assert len(calls) == 1


async def test_listener_serves_real_https_and_stops(tmp_path: Path) -> None:
    """The service speaks TLS from its own CA: a client that trusts
    that CA completes, and the fixed credential answers ok over it."""
    app = build_app(probe_settings(tmp_path))
    server = app.state.probe
    certfile, keyfile = await server.material()
    ca_file = tmp_path / "state" / "probe" / ca.CA_CERT_FILE
    listener = TapListener(
        server.probe_app,
        tap="lo",
        tap_ip="127.0.0.1",
        port=0,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )
    await server.start_listener("ws1", listener)
    port = listener._sock.getsockname()[1]
    context = ssl.create_default_context(cafile=str(ca_file))
    context.check_hostname = False  # the loopback dial names no host
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{port}", verify=context
    ) as http:
        ok = await http.get("/", headers=VALID)
        denied = await http.get("/")
    assert ok.status_code == 200
    assert ok.text == "ok"
    assert denied.status_code == 401
    await server.stop_listener("ws1")
    await server.stop_listener("ws1")  # idempotent
    assert server._listeners == {}


async def test_listener_bind_failure_unregisters(tmp_path: Path) -> None:
    app = build_app(probe_settings(tmp_path))
    server = app.state.probe
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    listener = TapListener(
        server.probe_app, tap="lo", tap_ip="127.0.0.1", port=port
    )
    with pytest.raises(OSError):
        await server.start_listener("ws1", listener)
    assert server._listeners == {}
    squatter.close()


def test_stop_mapping_leaves_a_replaced_listener(tmp_path: Path) -> None:
    """The registry forgets an entry only when it still names the
    listener asked about."""
    server = build_app(probe_settings(tmp_path)).state.probe
    first = TapListener(server.probe_app, tap="lo", tap_ip="10.0.0.1", port=1)
    second = TapListener(server.probe_app, tap="lo", tap_ip="10.0.0.1", port=1)
    server._listeners["ws1"] = first
    server.stop_mapping("ws1", second)
    assert server._listeners == {"ws1": first}
    server.stop_mapping("ws1", first)
    assert server._listeners == {}


def test_the_module_states_its_constants() -> None:
    assert probe_mod.PROBE_HOST == "secretprobe.msks"
