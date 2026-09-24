"""The embedded interceptor, live on loopback (#199).

The spike's evidence, replayed against the production manager: the
real mitmproxy master, a real per-tap listener, real TLS origins,
and a patched ``SO_ORIGINAL_DST`` seam that plays the nft redirect's
part (the production path needs CAP_NET_ADMIN the test host does
not grant). 127.0.0.2 stands in for the tap address: the whole
127/8 is loopback, so the per-tap listener binds without privileges.
"""

import asyncio
import json
import socket
import ssl
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from msks.app import build_app
from msks.interceptor import Interceptor, ca
from msks.microvm import VmSpec
from msks.secretstore import backend_ref, new_sentinel
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings

TAP_IP = "127.0.0.2"
API = "api.example.com"
OTHER = "other.example.com"
OTHER2 = "other2.example.net"
PLAIN = "plain.example.net"


class FakeNet:
    """The attachment the armed workspace runs against."""

    def __init__(self) -> None:
        self.interceptions: list[tuple[str, int | None]] = []

    def attachment_for(self, workspace_id: str):
        return type("Attachment", (), {"tap_ip": TAP_IP})()

    async def apply_interception(self, workspace_id, port) -> None:
        self.interceptions.append((workspace_id, port))


class FakeSecrets:
    """The store cache, pre-seeded with the real values."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def read(self, ref: str) -> str:
        return self.values[ref]


def free_port() -> int:
    sock = socket.socket()
    sock.bind((TAP_IP, 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class Origin:
    """A loopback TLS origin echoing what it received."""

    def __init__(self, tmp: Path, name: str, hosts: list[str]) -> None:
        self.dir = tmp / f"origin-{name}"
        self.dir.mkdir()
        self.authority = ca.load_or_mint(self.dir, name)
        self.leaf_key, self.leaf = ca.mint_leaf(
            self.authority, hosts[0], altnames=tuple(hosts)
        )
        self.ca_pem = (self.dir / ca.CA_CERT_FILE).read_bytes()
        self.server: asyncio.AbstractServer | None = None
        self.ssl_kwargs = {}

    async def start(self) -> None:
        chain = self.dir / "chain.pem"
        chain.write_bytes(pem(self.leaf) + self.ca_pem)
        leaf_key = self.dir / "leaf.key"
        leaf_key.write_bytes(key_pem(self.leaf_key))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain, leaf_key)
        self.server = await asyncio.start_server(
            self.serve, "127.0.0.1", 0, ssl=context
        )

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.sockets[0].getsockname()[1]

    async def serve(self, reader, writer) -> None:
        try:
            line = await reader.readline()
            headers = {}
            while True:
                raw = await reader.readline()
                if raw in (b"\r\n", b"\n", b""):
                    break
                name, _, value = raw.decode().partition(":")
                headers[name.strip().lower()] = value.strip()
            path = line.decode().split()[1]
            body = json.dumps(
                {
                    "auth": headers.get("authorization"),
                    "path": path,
                    "host": headers.get("host"),
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except Exception:  # noqa: BLE001 - a flaked origin fails the test
            pass
        finally:
            writer.close()

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


class PlainOrigin(Origin):
    """The plain-HTTP stand-in: the same echo, no TLS."""

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)


def read_response(sock: socket.socket) -> tuple[int, str]:
    chunks = []
    while chunk := sock.recv(65536):
        chunks.append(chunk)
    raw = b"".join(chunks).decode()
    status, _, body = raw.partition("\r\n\r\n")
    return int(status.split()[1]), body


def https_get(
    listener_port, sni, path, headers, cafile, host_header=None
) -> tuple[int, str]:
    """One guest-side HTTPS request through the redirect stand-in;
    *host_header* overrides the Host the guest claims (the fronting
    leg)."""
    context = ssl.create_default_context(cafile=cafile)
    with socket.create_connection((TAP_IP, listener_port), timeout=20) as sock:
        with context.wrap_socket(sock, server_hostname=sni) as tls:
            tls.sendall(request_bytes(host_header or sni, path, headers))
            return read_response(tls)


def plain_get(listener_port, host, path, headers) -> tuple[int, str]:
    with socket.create_connection((TAP_IP, listener_port), timeout=20) as sock:
        sock.sendall(request_bytes(host, path, headers))
        return read_response(sock)


def request_bytes(host: str, path: str, headers: dict) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        + "Connection: close\r\n\r\n"
    ).encode()


async def seed(app, name: str, dests: list[str], secret: str) -> dict:
    """One placeholder row and its cached store value."""
    if await app.state.model.get_workspace("ws-live") is None:
        await app.state.model.create_workspace(
            VmSpec(
                workspace_id="ws-live", kernel=Path("/k"), rootfs=Path("/r")
            )
        )
    sentinel = new_sentinel()
    ref = backend_ref("ws-live", name)
    row = await app.state.model.create_placeholder(
        "ws-live", name, sentinel, dests, ref, None
    )
    app.state.secrets.values[ref] = secret
    return row


@pytest.mark.timeout(180)
async def test_the_live_interceptor(tmp_path, monkeypatch) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="msks-live-"))
    origins = {
        "a": Origin(scratch, "a", [API]),
        "b": Origin(scratch, "b", [OTHER, OTHER2]),
        "plain": PlainOrigin(scratch, "plain", [PLAIN]),
    }
    for origin in origins.values():
        await origin.start()

    port = free_port()
    app = build_app(
        Settings(
            vmm=VmmSettings(state_dir=tmp_path),
            net=NetSettings(interceptor_port=port),
            server=ServerSettings(db_path=tmp_path / "msks.db"),
        )
    )
    app.state.model.migrate()
    app.state.net = FakeNet()
    secrets = FakeSecrets()
    app.state.secrets = secrets
    interceptor = Interceptor(app)
    app.state.interceptor = interceptor

    # The redirect's stand-in: the original destination the nft rule
    # would have preserved in conntrack.
    holder: dict = {}

    def fake_original_addr(sock) -> tuple[str, int]:
        return holder["dst"]

    import mitmproxy.platform

    monkeypatch.setattr(
        mitmproxy.platform, "original_addr", fake_original_addr
    )

    first = await seed(app, "api", [API], "real-secret-one")
    second = await seed(app, "wide", [API, OTHER2], "real-secret-two")
    queue = app.state.hub.subscribe()

    await interceptor.refresh("ws-live")
    assert app.state.net.interceptions == [("ws-live", port)]
    master = interceptor._master
    assert master is not None
    bundle = scratch / "upstream-bundle.pem"
    bundle.write_bytes(origins["a"].ca_pem + origins["b"].ca_pem)
    master.options.update(ssl_verify_upstream_trusted_ca=str(bundle))

    ws_ca = str(tmp_path / "vms" / "ws-live" / ca.CA_CERT_FILE)
    origin_b_ca = str(origins["b"].dir / ca.CA_CERT_FILE)
    auth = {"Authorization": f"Bearer {first['sentinel']}"}

    # 1. The swap: the origin sees the real secret in both places.
    holder["dst"] = ("127.0.0.1", origins["a"].port)
    status, body = await asyncio.to_thread(
        https_get, port, API, f"/echo?api_key={first['sentinel']}", auth, ws_ca
    )
    assert status == 200
    assert "real-secret-one" in body
    assert first["sentinel"] not in body

    # 2. The splice: undecrypted relay — a client that trusts only
    # the origin's own CA completes, and the sentinel rides raw.
    holder["dst"] = ("127.0.0.1", origins["b"].port)
    status, body = await asyncio.to_thread(
        https_get, port, OTHER, "/echo", auth, origin_b_ca
    )
    assert status == 200
    assert first["sentinel"] in body

    # 3. The sighting: decrypted (the wide placeholder covers the
    # host), unrewritten, announced.
    holder["dst"] = ("127.0.0.1", origins["b"].port)
    status, body = await asyncio.to_thread(
        https_get, port, OTHER2, "/echo", auth, ws_ca
    )
    assert status == 200
    assert first["sentinel"] in body

    # 4. Revocation: the row is gone but the workspace stays armed
    # through the second placeholder — decrypted, unrewritten.
    await app.state.model.delete_placeholder(first["id"])
    await interceptor.refresh("ws-live")
    holder["dst"] = ("127.0.0.1", origins["a"].port)
    status, body = await asyncio.to_thread(
        https_get, port, API, "/echo", auth, ws_ca
    )
    assert status == 200
    assert first["sentinel"] in body

    # 5. Plain HTTP: decrypted by construction, and an uncovered
    # Host is a sighting — the raw sentinel reaches the origin.
    holder["dst"] = ("127.0.0.1", origins["plain"].port)
    live_auth = {"Authorization": f"Bearer {second['sentinel']}"}
    status, body = await asyncio.to_thread(
        plain_get, port, PLAIN, "/echo", live_auth
    )
    assert status == 200
    assert second["sentinel"] in body

    # 6. Fronting: the guest claims an allowlisted SNI with a Host
    # header naming its own vhost — the swap pins the header to the
    # matched name, and the allowlisted origin answers.
    holder["dst"] = ("127.0.0.1", origins["a"].port)
    status, body = await asyncio.to_thread(
        https_get,
        port,
        API,
        "/echo",
        {"Authorization": f"Bearer {second['sentinel']}"},
        ws_ca,
        host_header="attacker-vhost.example.net",
    )
    assert status == 200
    seen = json.loads(body)
    # The pin names the allowlisted host; the port is the origin's
    # real (here ephemeral) port — production's redirect only ever
    # hands 443 to this path, where the name rides bare.
    assert seen["host"] == f"{API}:{origins['a'].port}"
    assert "real-secret-two" in seen["auth"]

    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    kinds = [event["event"] for event in events]
    assert "secret.swap" in kinds
    assert kinds.count("secret.sighting") == 2
    swap = next(event for event in events if event["event"] == "secret.swap")
    assert swap["data"]["workspace_id"] == "ws-live"
    assert swap["data"]["name"] == "api"
    assert swap["data"]["host"] == API
    assert swap["data"]["placeholder_id"] == first["id"]
    assert swap["data"]["ts"] > 0.0

    await interceptor.stop()
    for origin in origins.values():
        origin.close()
