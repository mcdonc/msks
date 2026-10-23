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
import time
from pathlib import Path

import h2.config
import h2.connection
import h2.events
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

#: One streamed h2 response body (#600): large enough to cross the
#: protocol's initial flow-control windows many times over.
STREAM_BODY = 512 * 1024
STREAM_CHUNK = 64 * 1024


class FakeNet:
    """The attachment the armed workspace runs against."""

    def __init__(self) -> None:
        self.interceptions: list[tuple[str, int | None]] = []
        # The naming memory's stand-in: address -> the name the
        # daemon's resolver last learned for it.
        self.names: dict[str, str] = {}

    def host_for(self, workspace_id: str, address: str) -> str | None:
        return self.names.get(address)

    def attachment_for(self, workspace_id: str):
        return type("Attachment", (), {"tap_ip": TAP_IP})()

    async def apply_interception(self, workspace_id, port) -> None:
        self.interceptions.append((workspace_id, port))


class FakePlain:
    """The unlock cache, pre-seeded with the real values (#514)."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, ref: str) -> str:
        return self.values[ref]


class FakeSecrets:
    def __init__(self) -> None:
        self.plain = FakePlain()


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

    def __init__(
        self, tmp: Path, name: str, hosts: list[str], addr: str = "127.0.0.1"
    ) -> None:
        self.addr = addr
        self.dir = tmp / f"origin-{name}"
        self.dir.mkdir()
        self.authority = ca.load_or_mint(self.dir)
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
            self.serve, self.addr, 0, ssl=context
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
        self.server = await asyncio.start_server(self.serve, self.addr, 0)


class H2Origin(Origin):
    """The GitHub-shaped origin (#600): negotiates h2 when the dial
    offers it, streams one large body per request (flow control
    honored, connection kept alive), and records the protocol each
    connection negotiated so a test can pin the translation shape."""

    def __init__(self, tmp: Path, name: str, hosts: list[str]) -> None:
        super().__init__(tmp, name, hosts)
        self.negotiated: list[str | None] = []

    async def start(self) -> None:
        chain = self.dir / "chain.pem"
        chain.write_bytes(pem(self.leaf) + self.ca_pem)
        leaf_key = self.dir / "leaf.key"
        leaf_key.write_bytes(key_pem(self.leaf_key))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.set_alpn_protocols(["h2", "http/1.1"])
        context.load_cert_chain(chain, leaf_key)
        self.server = await asyncio.start_server(
            self.serve, self.addr, 0, ssl=context
        )

    async def serve(self, reader, writer) -> None:
        tls = writer.get_extra_info("ssl_object")
        alpn = tls.selected_alpn_protocol() if tls else None
        self.negotiated.append(alpn)
        if alpn != "h2":
            await super().serve(reader, writer)
            return
        config = h2.config.H2Configuration(
            client_side=False, header_encoding="utf-8"
        )
        conn = h2.connection.H2Connection(config=config)
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        await writer.drain()
        pending: dict[int, int] = {}  # stream id -> bytes left to send

        async def pump(stream_id: int) -> None:
            while pending[stream_id] > 0:
                window = min(
                    conn.local_flow_control_window(stream_id),
                    conn.max_outbound_frame_size,
                )
                if window <= 0:
                    return  # a WindowUpdated event resumes the pump
                size = min(window, STREAM_CHUNK, pending[stream_id])
                conn.send_data(stream_id, b"x" * size)
                pending[stream_id] -= size
                out = conn.data_to_send()
                if out:
                    writer.write(out)
                    await writer.drain()
            conn.end_stream(stream_id)
            out = conn.data_to_send()
            if out:
                writer.write(out)
                await writer.drain()

        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        conn.send_headers(
                            event.stream_id,
                            [(":status", "200")],
                        )
                        pending[event.stream_id] = STREAM_BODY
                        await pump(event.stream_id)
                    elif isinstance(event, h2.events.WindowUpdated):
                        for sid in list(pending):
                            if pending[sid] > 0:
                                await pump(sid)
                    elif isinstance(event, h2.events.DataReceived):
                        conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                out = conn.data_to_send()
                if out:
                    writer.write(out)
                    await writer.drain()
        except Exception:  # noqa: BLE001 - a flaked origin fails the test
            pass
        finally:
            writer.close()


def read_framed(tls) -> dict:
    """One HTTP/1.1 response with its framing honored: the status
    line, the headers, then exactly the framed body. A response with
    no length of its own reads to connection close — the shape that
    broke git (#600) — and reports itself closed."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = tls.recv(65536)
        if not chunk:
            raise AssertionError(f"EOF mid-headers, got {raw!r}")
        raw += chunk
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for row in lines[1:]:
        name, _, value = row.partition(":")
        headers[name.strip().lower()] = value.strip()
    body = bytearray(rest)
    if "content-length" in headers:
        length = int(headers["content-length"])
        while len(body) < length:
            chunk = tls.recv(65536)
            if not chunk:
                raise AssertionError(
                    f"EOF after {len(body)}/{length} body bytes"
                )
            body += chunk
        del body[length:]
        closed = False
    else:
        while chunk := tls.recv(65536):
            body += chunk
        closed = True
    return {
        "status": status,
        "headers": headers,
        "body": bytes(body),
        "closed": closed,
    }


def alpn_gets(
    listener_port, sni, paths: list[str], cafile
) -> tuple[str | None, list[dict]]:
    """The git/curl guest (#600): offers ALPN h2 + http/1.1 (so the
    upstream dial carries both and an h2 origin answers h2), speaks
    HTTP/1.1 on whatever the interceptor selects, and sends one
    request per path on a single kept-alive connection."""
    context = ssl.create_default_context(cafile=cafile)
    context.set_alpn_protocols(["h2", "http/1.1"])
    out = []
    with socket.create_connection((TAP_IP, listener_port), timeout=60) as sock:
        with context.wrap_socket(sock, server_hostname=sni) as tls:
            alpn = tls.selected_alpn_protocol()
            for path in paths:
                tls.sendall(
                    (
                        f"GET {path} HTTP/1.1\r\nHost: {sni}\r\n"
                        "User-Agent: git/2.39.5\r\nAccept: */*\r\n\r\n"
                    ).encode()
                )
                out.append(read_framed(tls))
    return alpn, out


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
    ref = backend_ref(["ws-live"], name)
    row = await app.state.model.create_placeholder(
        ["ws-live"], name, sentinel, dests, ref, None
    )
    app.state.secrets.plain.values[ref] = secret
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

    # #485: the interceptor CA is the daemon state root's, not
    # the workspace directory's.
    ws_ca = str(tmp_path / ca.CA_CERT_FILE)
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


@pytest.mark.timeout(180)
async def test_the_live_probe_chain(tmp_path, monkeypatch) -> None:
    """The #424 probe, end to end through the real machinery: the
    guest's HTTPS request toward probe.msms — redirected, spliced,
    leaf-minted, its raw Basic sentinel blob swapped for the minted
    credential, the upstream dial verified against the probe CA —
    answers ``ok`` from the real probe service; a bogus blob answers
    401 with the challenge."""
    from msks.llm import TapListener
    from msks.spec.probe import PROBE_HOST, PROBE_SECRET_B64

    app = build_app(
        Settings(
            vmm=VmmSettings(state_dir=tmp_path),
            net=NetSettings(interceptor_port=0),
            server=ServerSettings(db_path=tmp_path / "msks.db"),
        )
    )
    # The armed listener's port, picked free like the origins'.
    port = free_port()
    app.state.settings.net.interceptor_port = port
    app.state.model.migrate()
    app.state.net = FakeNet()
    app.state.secrets = FakeSecrets()
    interceptor = Interceptor(app)
    app.state.interceptor = interceptor

    def fake_original_addr(sock) -> tuple[str, int]:
        # Production shape (#532): the guest dialed 443, so the
        # original destination carries 443 — the addon's dial
        # repoint must move it to the service's own port below.
        return TAP_IP, 443

    import mitmproxy.platform

    monkeypatch.setattr(
        mitmproxy.platform, "original_addr", fake_original_addr
    )

    # The probe service's listener on the tap address, asked for
    # an ephemeral port: the repoint (443 -> the service's own
    # port) must resolve the RUNNING listener's recorded port from
    # the registry — the setting still names 8644, so only the
    # registry path can pass this test.
    service = app.state.probe
    certfile, keyfile = await service.material()
    listener = TapListener(
        service.probe_app,
        tap="lo",
        tap_ip=TAP_IP,
        port=0,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )
    await service.start_listener("ws-live", listener)

    row = await seed(app, "probe", [PROBE_HOST], PROBE_SECRET_B64)
    await interceptor.refresh("ws-live")
    master = interceptor._master
    assert master is not None
    # build_master's default trust (#424): the bundle carries the
    # probe CA appended to the platform roots — the option REPLACES
    # the default lookup, so the file must hold both. The upstream
    # dial to the service verifies against it below.
    trust = master.options.ssl_verify_upstream_trusted_ca
    assert trust is not None and trust.endswith("upstream-bundle.pem")
    body = Path(trust).read_bytes()
    assert body.startswith(
        (tmp_path / "probe" / "interceptor-ca.crt").read_bytes()
    )
    assert len(body) > len(
        (tmp_path / "probe" / "interceptor-ca.crt").read_bytes()
    )

    # #485: the interceptor CA is the daemon state root's, not
    # the workspace directory's.
    ws_ca = str(tmp_path / ca.CA_CERT_FILE)
    auth = {"Authorization": f"Basic {row['sentinel']}"}

    try:
        status, body = await asyncio.to_thread(
            https_get, port, PROBE_HOST, "/", auth, ws_ca
        )
        assert status == 200
        assert body == "ok"
        wrong = {"Authorization": f"Basic {row['sentinel']}XX"}
        status, body = await asyncio.to_thread(
            https_get, port, PROBE_HOST, "/", wrong, ws_ca
        )
        assert status == 401
        assert body == "invalid credentials"
    finally:
        await interceptor.stop()
        await service.stop_listener("ws-live")


async def pending_request(app) -> dict:
    """The workspace's first pending consent row, polled until it
    appears (the gate creates it when the ClientHello holds)."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        rows = await app.state.model.egress_consent.list_requests(
            "ws-live", decision="pending"
        )
        if rows:
            return rows[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no pending consent request appeared")


@pytest.mark.timeout(180)
async def test_the_live_consent_gate(tmp_path, monkeypatch) -> None:
    """#452, live: an armed interceptor on an interactive workspace
    holds the guest's web flows for a decider verdict — the gate
    the prerouting redirect made the kernel queue unable to serve.
    An allowed destination completes its handshake and swaps; a
    denied one answers the local refusal without forwarding."""
    scratch = Path(tempfile.mkdtemp(prefix="msks-live-"))
    origins = {
        # One loopback address per origin, so the naming memory's
        # address→name binding stands in cleanly (production's
        # destinations hold distinct addresses; the swap test's
        # shared 127.0.0.1 would map one name for both).
        "a": Origin(scratch, "a", [API], addr="127.0.0.11"),
        "b": Origin(scratch, "b", [OTHER], addr="127.0.0.12"),
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
    app.state.net.names = {"127.0.0.11": API, "127.0.0.12": OTHER}
    app.state.secrets = FakeSecrets()
    interceptor = Interceptor(app)
    app.state.interceptor = interceptor

    holder: dict = {}

    def fake_original_addr(sock) -> tuple[str, int]:
        return holder["dst"]

    import mitmproxy.platform

    monkeypatch.setattr(
        mitmproxy.platform, "original_addr", fake_original_addr
    )

    first = await seed(app, "api", [API], "real-secret-one")
    await app.state.model.set_egress_policy("ws-live", "interactive", None)
    app.state.deciders.register(1, "ws-live")

    await interceptor.refresh("ws-live")
    master = interceptor._master
    assert master is not None
    bundle = scratch / "upstream-bundle.pem"
    bundle.write_bytes(origins["a"].ca_pem + origins["b"].ca_pem)
    master.options.update(ssl_verify_upstream_trusted_ca=str(bundle))

    # #485: the interceptor CA is the daemon state root's, not
    # the workspace directory's.
    ws_ca = str(tmp_path / ca.CA_CERT_FILE)
    auth = {"Authorization": f"Bearer {first['sentinel']}"}

    try:
        # 1. The hold: the ClientHello waits for a verdict, keyed
        # by the SNI the naming memory binds to the connection's
        # address (the origin's ephemeral port here stands in for
        # the 443 production's redirect preserves).
        holder["dst"] = ("127.0.0.11", origins["a"].port)
        task = asyncio.create_task(
            asyncio.to_thread(https_get, port, API, "/echo", auth, ws_ca)
        )
        request = await pending_request(app)
        assert request["dest_host"] == API
        # The port the redirect stand-in preserved: the origin's
        # ephemeral port here (production's redirect hands 443).
        assert request["dest_port"] == origins["a"].port

        # 2. The allow: the handshake completes and the swap runs.
        await app.state.consent.resolve(
            request["id"], "allowed", "tester", "5m"
        )
        status, body = await task
        assert status == 200
        assert "real-secret-one" in body
        assert first["sentinel"] not in body

        # 3. The session allow covers the next connection: no new
        # hold, same swap.
        holder["dst"] = ("127.0.0.11", origins["a"].port)
        status, body = await asyncio.to_thread(
            https_get, port, API, "/echo", auth, ws_ca
        )
        assert status == 200
        assert "real-secret-one" in body
        await asyncio.sleep(0.3)
        rows = await app.state.model.egress_consent.list_requests(
            "ws-live", decision="pending"
        )
        assert rows == []

        # 4. The deny: a second destination holds; the verdict
        # answers locally — the handshake completes against the
        # daemon CA, the request refuses, nothing forwards.
        holder["dst"] = ("127.0.0.12", origins["b"].port)
        task = asyncio.create_task(
            asyncio.to_thread(https_get, port, OTHER, "/echo", auth, ws_ca)
        )
        request = await pending_request(app)
        assert request["dest_host"] == OTHER
        await app.state.consent.resolve(
            request["id"], "denied", "tester", "5m"
        )
        status, body = await task
        assert status == 403
        assert "not forwarded" in body
        assert first["sentinel"] not in body
    finally:
        await interceptor.stop()
        for origin in origins.values():
            origin.close()


@pytest.mark.timeout(180)
async def test_the_decrypted_path_frames_translated_streams(
    tmp_path, monkeypatch
) -> None:
    """#600, live: the guest offers ALPN h2 + http/1.1 — git and curl
    both do — and the interceptor's client context selects none, so
    the guest speaks HTTP/1.1 while the upstream dial carries the h2
    offer and the origin answers h2, whose responses carry no length
    of their own. The decrypted path frames them: every streamed
    response arrives whole behind a matching Content-Length, and the
    kept-alive connection serves the next request instead of dying
    mid-stream (the git-clone failure)."""
    scratch = Path(tempfile.mkdtemp(prefix="msks-h2-"))
    origin = H2Origin(scratch, "gh", [API])
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
    app.state.secrets = FakeSecrets()
    interceptor = Interceptor(app)
    app.state.interceptor = interceptor

    holder: dict = {}

    def fake_original_addr(sock) -> tuple[str, int]:
        return holder["dst"]

    import mitmproxy.platform

    monkeypatch.setattr(
        mitmproxy.platform, "original_addr", fake_original_addr
    )

    await seed(app, "api", [API], "real-secret-one")
    await interceptor.refresh("ws-live")
    master = interceptor._master
    assert master is not None
    bundle = scratch / "upstream-bundle.pem"
    bundle.write_bytes(origin.ca_pem)
    master.options.update(ssl_verify_upstream_trusted_ca=str(bundle))
    ws_ca = str(tmp_path / ca.CA_CERT_FILE)

    try:
        holder["dst"] = ("127.0.0.1", origin.port)
        alpn, responses = await asyncio.to_thread(
            alpn_gets,
            port,
            API,
            ["/info/refs?service=git-upload-pack", "/git-upload-pack"],
            ws_ca,
        )
        # The translation shape: the guest fell back to HTTP/1.1
        # (the client context selects no ALPN) while the origin
        # negotiated h2 — the mixed pair whose framing #600 pinned.
        assert alpn is None
        assert "h2" in origin.negotiated
        assert len(responses) == 2
        for response in responses:
            assert response["status"] == 200
            assert response["headers"].get("content-length") == str(
                STREAM_BODY
            )
            assert response["body"] == b"x" * STREAM_BODY
            assert response["closed"] is False
    finally:
        await interceptor.stop()
        origin.close()
