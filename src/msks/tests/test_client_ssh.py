"""``msks ssh`` tests: the transient agent, argv plumbing, the run loop.

The agent's wire protocol is pinned against a real socket (identities,
signing for every minted key type, refusal of everything else), the
command's pieces are pinned separately, and — where a local sshd
exists — one integration test runs stock ssh against it through the
agent, proving the IdentityAgent path ssh itself will take.
"""

import asyncio
import base64
import os
import shutil
import socket
import struct
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from msks.client import agent, cli, ssh
from msks.identity import mint

PEM = mint("ed25519")[0]
ECDSA_PEM = mint("ecdsa")[0]
RSA_PEM = mint("rsa")[0]
KEY = {
    "public_key": "ecdsa-sha2-nistp256 AAAA msksd:alpha",
    "private_key": PEM,
}
RUNNING_ROW = {"id": "alpha", "status": "running"}

SSH_BIN = shutil.which("ssh")
SSHD_BIN = shutil.which("sshd")
KEYGEN_BIN = shutil.which("ssh-keygen")

needs_sshd = pytest.mark.skipif(
    SSHD_BIN is None or KEYGEN_BIN is None,
    reason="sshd and ssh-keygen must be on PATH (the devenv shell ships them)",
)


@pytest.fixture
def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")


def mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- the wire helpers ---


def test_wire_string_and_reader_roundtrip() -> None:
    payload = agent.wire_string(b"abc") + agent.wire_string(b"")
    reader = agent.Reader(payload)
    assert reader.string() == b"abc"
    assert reader.string() == b""


def test_reader_refuses_reads_past_the_end() -> None:
    with pytest.raises(ValueError, match="uint32 past end"):
        agent.Reader(b"ab").uint32()
    with pytest.raises(ValueError, match="string past end"):
        agent.Reader(struct.pack(">I", 9) + b"abc").string()


def test_mpint_keeps_the_high_bit_clear() -> None:
    # 0x80... would read as negative without the padding zero.
    assert agent.mpint(0xFF) == struct.pack(">I", 2) + b"\x00\xff"
    assert agent.mpint(0) == struct.pack(">I", 1) + b"\x00"


# --- the served identity ---


@pytest.mark.parametrize(
    "pem", [PEM, ECDSA_PEM, RSA_PEM], ids=["ed25519", "ecdsa", "rsa"]
)
def test_public_parts_match_the_authorized_keys_line(pem: str) -> None:
    blob, algo = agent.public_parts(agent.load_private(pem))
    minted_public = agent_public_line(pem)
    fields = minted_public.split()
    assert algo == fields[0]
    assert blob == base64.b64decode(fields[1])


def agent_public_line(pem: str) -> str:
    """The minted public line (what authorized_keys carries)."""
    private = serialization.load_ssh_private_key(pem.encode(), password=None)
    return (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )


# --- signing, verified against the public halves ---


@pytest.mark.parametrize(
    "pem", [PEM, ECDSA_PEM, RSA_PEM], ids=["ed25519", "ecdsa", "rsa"]
)
def test_sign_answers_with_a_verifiable_signature(pem: str) -> None:
    private = agent.load_private(pem)
    challenge = b"prove it"
    blob = agent.sign(private, challenge, rsa_sha512=False)
    verify(blob, private, challenge)


def test_sign_refuses_an_unserved_curve() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec as ec_curves

    odd = ec_curves.generate_private_key(ec_curves.SECP384R1())
    with pytest.raises(ValueError, match="unsupported ECDSA curve"):
        agent.sign(odd, b"x", rsa_sha512=False)


def test_rsa_honors_the_sha512_flag() -> None:
    private = agent.load_private(RSA_PEM)
    challenge = b"larger digest"
    blob = agent.sign(private, challenge, rsa_sha512=True)
    assert blob.startswith(b"\x00\x00\x00\x0crsa-sha2-512")
    verify(blob, private, challenge, sha512=True)


def verify(blob: bytes, private, challenge: bytes, sha512: bool = False) -> None:
    reader = agent.Reader(blob)
    algo = reader.string().decode()
    sig = reader.string()
    public = private.public_key()
    if algo == "ssh-ed25519":
        public.verify(sig, challenge)
    elif algo.startswith("ecdsa-"):
        r, s = decode_dss_signature(der_from_mpints(sig))
        public.verify(der_from_rs(r, s), challenge, ec.ECDSA(hashes.SHA256()))
    else:
        digest = hashes.SHA512() if sha512 else hashes.SHA256()
        public.verify(sig, challenge, padding.PKCS1v15(), digest)


def der_from_mpints(sig: bytes) -> bytes:
    """(r, s) as a DER ECDSA-Signature — what verify wants."""
    reader = agent.Reader(sig)
    r = int.from_bytes(reader.string(), "big")
    s = int.from_bytes(reader.string(), "big")
    return der_from_rs(r, s)


def der_from_rs(r: int, s: int) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    return encode_dss_signature(r, s)


# --- the agent over a real socket ---


def call_agent(sock: socket.socket, payload: bytes) -> bytes:
    """One request/response round trip."""
    sock.settimeout(10)
    sock.sendall(agent.frame(payload))
    header = recv_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    return recv_exact(sock, length)


def recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        assert chunk, "agent closed early"
        data += chunk
    return data


def test_agent_lists_and_signs_over_the_socket() -> None:
    private = agent.load_private(PEM)
    with agent.serve(private, "msksd:alpha") as served:
        assert agent.socket_mode(served.server_address) == 0o600
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(served.server_address)
            listing = call_agent(sock, struct.pack("B", agent.REQUEST_IDENTITIES))
            assert listing[0] == agent.IDENTITIES_ANSWER
            reader = agent.Reader(listing)
            reader.offset = 1
            assert reader.uint32() == 1
            assert reader.string() == served.blob
            assert reader.string() == b"msksd:alpha"
            challenge = b"nonce"
            answer = call_agent(
                sock,
                struct.pack("B", agent.SIGN_REQUEST)
                + agent.wire_string(served.blob)
                + agent.wire_string(challenge)
                + struct.pack(">I", 0),
            )
            assert answer[0] == agent.SIGN_RESPONSE
            response = agent.Reader(answer)
            response.offset = 1
            verify(response.string(), private, challenge)
            # The socket is private to this user.
            assert agent.socket_mode(served.server_address) == 0o600


def test_agent_refuses_other_keys_and_unknown_requests() -> None:
    with agent.serve(agent.load_private(PEM), "") as served:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(served.server_address)
            wrong = call_agent(
                sock,
                struct.pack("B", agent.SIGN_REQUEST)
                + agent.wire_string(b"\x00" * 32)
                + agent.wire_string(b"x")
                + struct.pack(">I", 0),
            )
            assert wrong == struct.pack("B", agent.FAILURE)
            unknown = call_agent(sock, struct.pack("B", 26) + b"session-bind")
            assert unknown == struct.pack("B", agent.FAILURE)
            # A garbled sign request (strings past the payload's end)
            # answers FAILURE, not a dropped connection.
            garbled = call_agent(
                sock, struct.pack("B", agent.SIGN_REQUEST) + b"\x00\x00"
            )
            assert garbled == struct.pack("B", agent.FAILURE)
            # A frame whose length field passes the ceiling closes us out.
            sock.sendall(struct.pack(">I", agent.MAX_MESSAGE + 1))
            assert sock.recv(16) == b""


def test_agent_ends_the_connection_on_truncation() -> None:
    with agent.serve(agent.load_private(PEM), "") as served:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(served.server_address)
            sock.sendall(struct.pack(">I", 64) + b"short")
            # Half-close: the promised bytes never come, so the server
            # must give up on EOF rather than wait for the rest.
            sock.shutdown(socket.SHUT_WR)
            assert sock.recv(16) == b""


# --- stock ssh against a local sshd, through the agent ---


@needs_sshd
def test_ssh_logs_in_through_the_agent(tmp_path: Path) -> None:
    """The semantics ``msks ssh`` depends on, proven against the
    real client: ssh authenticates from an IdentityAgent listing
    alone (no identity file), under IdentitiesOnly, and ``-A``
    forwards that same agent into the session."""
    user = os.environ.get("USER") or "nobody"
    hostkey = tmp_path / "host_ed25519"
    keygen = subprocess.run(
        [KEYGEN_BIN, "-t", "ed25519", "-N", "", "-q", "-f", str(hostkey)],
        timeout=30,
    )
    assert keygen.returncode == 0, keygen.stderr
    authorized = tmp_path / "authorized_keys"
    authorized.write_text(agent_public_line(PEM) + "\n")
    config = tmp_path / "sshd_config"
    port = free_port()
    config.write_text(
        f"Port {port}\n"
        "ListenAddress 127.0.0.1\n"
        f"HostKey {hostkey}\n"
        f"AuthorizedKeysFile {authorized}\n"
        "UsePAM no\n"
        # The harness's tmpdir sits under /tmp (world-writable parent):
        # StrictModes would reject the authorized_keys file for it.
        "StrictModes no\n"
        "PasswordAuthentication no\n"
        "AllowAgentForwarding yes\n"
        f"PidFile {tmp_path / 'sshd.pid'}\n",
    )
    sshd = subprocess.Popen(
        [SSHD_BIN, "-D", "-e", "-f", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_listening(port)
        with agent.serve(agent.load_private(PEM), "msksd:alpha") as served:
            login = subprocess.run(
                [
                    SSH_BIN,
                    "-o",
                    f"IdentityAgent={served.server_address}",
                    "-i",
                    served.identity_path,
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "BatchMode=yes",
                    "-A",
                    "-p",
                    str(port),
                    f"{user}@127.0.0.1",
                    "whoami",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert login.returncode == 0, login.stderr
            assert login.stdout.strip() == user
            # -A forwarded the session agent: ssh-add inside lists
            # the identity it serves.
            listed = subprocess.run(
                [
                    SSH_BIN,
                    "-o",
                    f"IdentityAgent={served.server_address}",
                    "-i",
                    served.identity_path,
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "BatchMode=yes",
                    "-A",
                    "-p",
                    str(port),
                    f"{user}@127.0.0.1",
                    "ssh-add -l",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert listed.returncode == 0, listed.stderr
            assert "msksd:alpha" in listed.stdout, listed.stdout
    finally:
        sshd.terminate()
        sshd.wait(timeout=10)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_listening(port: int, timeout_s: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"sshd never listened on {port}")


# --- the per-workspace known_hosts file ---


def test_known_hosts_path_creates_its_directory(tmp_path: Path) -> None:
    path = ssh.known_hosts_path("alpha", base=tmp_path)
    assert path == str(tmp_path / "alpha" / "known_hosts")
    assert (tmp_path / "alpha").is_dir()


def test_cache_dir_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    assert ssh.cache_dir() == Path("/tmp/xdg-cache/msks")


# --- passthrough handling ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["-l", "root"], ["-l", "root"]),
        (["-A"], ["-A"]),
        ([], []),
        # An ssh-style -- typed past argparse's own separator
        # reaches ssh untouched: it is the options/command split.
        (["--", "uname", "-a"], ["--", "uname", "-a"]),
    ],
)
def test_passthrough_args_passes_verbatim(raw: list[str], expected: list[str]) -> None:
    assert ssh.passthrough_args(raw) == expected


@pytest.mark.parametrize(
    ("args", "split"),
    [
        (["-A"], (["-A"], [])),
        (["-A", "--", "uname"], (["-A"], ["uname"])),
        (["--", "uname"], ([], ["uname"])),
        (["--", "--", "x"], ([], ["--", "x"])),  # split at the first --
    ],
)
def test_split_command_splits_at_ssh_separator(
    args: list[str], split: tuple[list[str], list[str]]
) -> None:
    assert ssh.split_command(args) == split


@pytest.mark.parametrize(
    ("args", "names"),
    [
        (["-l", "root"], True),
        (["-o", "User=root"], True),
        (["-o", "User root"], True),
        (["-o", "ProxyCommand=x"], False),
        (["-A"], False),
        (["-l"], False),  # a trailing -l with no value names nothing
        (["-oUser=root"], True),  # the inline -o form
        (["-oProxyCommand=x"], False),
    ],
)
def test_wants_user(args: list[str], names: bool) -> None:
    assert ssh.wants_user(args) is names


# --- the argv ---


def test_build_args_injects_the_default_user() -> None:
    assert ssh.build_args("alpha", "/agent.sock", "/id.pub", "/kh", ["-A"]) == [
        "ssh",
        "-o",
        "ProxyCommand=msks forward alpha 22",
        "-o",
        "UserKnownHostsFile=/kh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=/agent.sock",
        "-i",
        "/id.pub",
        "-l",
        "msks",
        "-A",
        "alpha",
    ]


def test_build_args_leaves_the_user_to_ssh() -> None:
    argv = ssh.build_args("alpha", "/agent.sock", "/id.pub", "/kh", ["-l", "root"])
    assert argv[-3:] == ["-l", "root", "alpha"]


def test_build_args_carries_a_remote_command_after_the_host() -> None:
    argv = ssh.build_args(
        "alpha", "/agent.sock", "/id.pub", "/kh", ["-A", "--", "uname", "-a"]
    )
    assert argv[-4:] == ["-A", "alpha", "uname", "-a"]


# --- prepare: the boot pre-flight and the key fetch ---


def test_prepare_boots_then_fetches() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(200, json=KEY)
        return httpx.Response(200, json=RUNNING_ROW)

    key = asyncio.run(
        ssh.prepare("alpha", "https://daemon", "tok", transport=mock(handler))
    )
    assert key == KEY
    assert seen == [
        "GET /api/v1/workspaces/alpha",
        "GET /api/v1/workspaces/alpha/ssh-key",
    ]


def test_identity_comment_reads_the_public_line() -> None:
    assert ssh.identity_comment(KEY) == "msksd:alpha"
    bare = {"public_key": "ssh-ed25519 AAAA", "private_key": PEM}
    assert ssh.identity_comment(bare) == ""


# --- the run loop ---


class FakeAgent:
    """The served agent stand-in: one fixed socket path."""

    server_address = "/faked/agent.sock"
    identity_path = "/faked/identity.pub"


def test_run_workspace_ssh_runs_ssh_and_stops_the_agent(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    async def fake_prepare(*args, **kwargs) -> dict:
        return KEY

    stopped: list = []
    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(ssh, "known_hosts_path", lambda ws, base=None: str(tmp_path))

    @contextmanager
    def fake_serve(private, comment):
        try:
            yield FakeAgent()
        finally:
            stopped.append("stopped")

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)
    calls: list[dict] = []

    def fake_run(argv, **kwargs) -> SimpleNamespace:
        calls.append({"argv": argv})
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    rc = ssh.run_workspace_ssh("alpha", ["-l", "root"])
    assert rc == 7
    argv = calls[0]["argv"]
    assert argv[0] == "ssh"
    assert "IdentityAgent=/faked/agent.sock" in argv
    assert argv[-3:] == ["-l", "root", "alpha"]
    assert stopped == ["stopped"]  # the agent tears down after ssh exits


def test_run_workspace_ssh_names_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    async def fake_prepare(*args, **kwargs) -> dict:
        return KEY

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(ssh, "known_hosts_path", lambda ws, base=None: str(tmp_path))

    def missing(argv, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(ssh.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="ssh not found"):
        ssh.run_workspace_ssh("alpha", [])  # the real agent stops around the failure


# --- CLI wiring ---


def test_cli_parses_the_ssh_passthrough() -> None:
    args = cli.build_parser().parse_args(["ssh", "alpha", "--", "-l", "root"])
    assert args.workspace_id == "alpha"
    assert args.passthrough == ["-l", "root"]  # argparse eats the --


def test_cli_dispatch_reaches_the_ssh_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        cli,
        "run_workspace_ssh",
        lambda ws, passthrough, transport=None: (
            calls.append((ws, passthrough)),
            5,
        )[1],
    )
    args = cli.build_parser().parse_args(["ssh", "alpha", "-A"])
    assert cli.dispatch(args) == 5
    assert calls == [("alpha", ["-A"])]
