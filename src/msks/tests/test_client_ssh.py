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
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)
from msks.client import agent, cli, ssh
from msks.identity import mint

PEM = mint("ed25519")[0]
ECDSA_PEM = mint("ecdsa")[0]
RSA_PEM = mint("rsa")[0]
KEY = {
    "public_key": "ecdsa-sha2-nistp256 AAAA msksd:alpha",
    "private_key": PEM,
    "user": "alice",
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
    # RSA needs a sha2 bit named; the other types ignore the flags.
    blob = agent.sign(private, challenge, agent.RSA_SHA2_256)
    verify(blob, private, challenge)


def test_rsa_sign_refuses_an_unpinned_digest() -> None:
    """flags=0 asks for SHA-1 (ssh-rsa) and flags=6 is no valid
    request (RFC 9987): both are refused, never mis-answered — a
    wrong-tagged signature would fail server-side with no clue.
    ed25519 and ECDSA ignore the flag bits entirely."""
    rsa_private = agent.load_private(RSA_PEM)
    assert agent.sign(rsa_private, b"x", 0) is None
    assert (
        agent.sign(rsa_private, b"x", agent.RSA_SHA2_256 | agent.RSA_SHA2_512)
        is None
    )
    assert agent.sign(agent.load_private(PEM), b"x", 0) is not None
    assert agent.sign(agent.load_private(ECDSA_PEM), b"x", 0) is not None


def test_sign_refuses_an_unserved_curve() -> None:
    """A curve outside the agent's wire map still refuses by name —
    secp256k1 stands in (no OpenSSH client offers it, so it never
    reaches a session; P-384/P-521 are served since #336)."""
    from cryptography.hazmat.primitives.asymmetric import ec as ec_curves

    odd = ec_curves.generate_private_key(ec_curves.SECP256K1())
    with pytest.raises(ValueError, match="unsupported ECDSA curve"):
        agent.sign(odd, b"x", 0)


def test_rsa_honors_the_sha512_flag() -> None:
    private = agent.load_private(RSA_PEM)
    challenge = b"larger digest"
    blob = agent.sign(private, challenge, agent.RSA_SHA2_512)
    assert blob.startswith(b"\x00\x00\x00\x0crsa-sha2-512")
    verify(blob, private, challenge, sha512=True)


def verify(
    blob: bytes, private, challenge: bytes, sha512: bool = False
) -> None:
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
    from cryptography.hazmat.primitives.asymmetric.utils import (
        encode_dss_signature,
    )

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
            listing = call_agent(
                sock, struct.pack("B", agent.REQUEST_IDENTITIES)
            )
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


def test_agent_refuses_an_unpinned_rsa_digest_over_the_socket() -> None:
    """The server-side face of the flags rule: an RSA sign request
    with no sha2 bit (SHA-1) answers FAILURE, not a mis-tagged
    signature the server would reject with no clue."""
    with agent.serve(agent.load_private(RSA_PEM), "") as served:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(served.server_address)
            refused = call_agent(
                sock,
                struct.pack("B", agent.SIGN_REQUEST)
                + agent.wire_string(served.blob)
                + agent.wire_string(b"x")
                + struct.pack(">I", 0),
            )
            assert refused == struct.pack("B", agent.FAILURE)


def test_serve_cleans_up_when_staging_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failure between the bind and the yield (here: the pubkey
    file's write) takes the socket and tempdir with it."""
    home = tmp_path / "agent-home"

    def fake_mkdtemp(**kwargs) -> str:
        home.mkdir(parents=True, exist_ok=True)
        return str(home)

    real_path = agent.Path

    class exploding_path(real_path):
        def write_text(self, *args, **kwargs) -> int:
            raise OSError("no space")

    monkeypatch.setattr(agent.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(agent, "Path", exploding_path)
    with pytest.raises(OSError, match="no space"):
        with agent.serve(agent.load_private(PEM), ""):
            pass
    assert not home.exists()


def test_agent_stays_quiet_when_the_peer_resets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A peer that vanishes mid-exchange ends its connection — the
    agent never prints a traceback into the ssh session's stderr."""
    with agent.serve(agent.load_private(PEM), "") as served:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as gone:
            gone.connect(served.server_address)
            gone.sendall(
                agent.frame(struct.pack("B", agent.REQUEST_IDENTITIES))
            )
            # SO_LINGER 0: close sends RST, so the server's reply
            # write fails after the recv succeeded.
            gone.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            gone.close()
        import time

        time.sleep(0.1)  # let the handler thread hit the reset
        # The agent still serves the next connection.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as fresh:
            fresh.connect(served.server_address)
            answer = call_agent(
                fresh, struct.pack("B", agent.REQUEST_IDENTITIES)
            )
            assert answer[0] == agent.IDENTITIES_ANSWER
    assert capsys.readouterr().err == ""


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


# --- the operator's agent in the environment ---


def test_environment_agent_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert agent.environment_agent() is None


def test_environment_agent_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    assert agent.environment_agent() is None  # not there yet
    (tmp_path / "agent.sock").write_bytes(b"")
    assert agent.environment_agent() == str(tmp_path / "agent.sock")


@needs_sshd
@pytest.mark.parametrize("pem", [PEM, ECDSA_PEM], ids=["ed25519", "ecdsa"])
def test_ssh_logs_in_through_the_agent(pem: str, tmp_path: Path) -> None:
    """The semantics ``msks ssh`` depends on, proven against the
    real client: ssh authenticates from an IdentityAgent listing
    alone (no identity file), under IdentitiesOnly, and ``-A``
    forwards that same agent into the session — the stock-ssh
    behavior :func:`ssh.forward_agent_args` rewrites command-line
    forwarding away from, onto the operator's agent. Parametrized over
    two mint types, ecdsa (P-256, #138's former default) and
    ed25519 (the current default) — with -F
    /dev/null, whose absence lets a host ssh_config drop ECDSA from
    PubkeyAcceptedAlgorithms (the #110 lesson applied here too)."""
    import pwd

    user = pwd.getpwuid(os.getuid()).pw_name
    hostkey = tmp_path / "host_ed25519"
    keygen = subprocess.run(
        [KEYGEN_BIN, "-t", "ed25519", "-N", "", "-q", "-f", str(hostkey)],
        timeout=30,
    )
    assert keygen.returncode == 0, keygen.stderr
    authorized = tmp_path / "authorized_keys"
    authorized.write_text(agent_public_line(pem) + "\n")
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
    sshd_stderr = tmp_path / "sshd.log"
    sshd_log = open(sshd_stderr, "ab")
    sshd = subprocess.Popen(
        [SSHD_BIN, "-D", "-e", "-f", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=sshd_log,
    )
    try:
        wait_listening(port, evidence=sshd_stderr)
        with agent.serve(agent.load_private(pem), "msksd:alpha") as served:
            common = [
                SSH_BIN,
                "-F",
                os.devnull,
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
            ]
            login = subprocess.run(
                [*common, "whoami"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert login.returncode == 0, login.stderr
            assert login.stdout.strip() == user
            # -A forwarded the session agent: ssh-add inside lists
            # the identity it serves.
            listed = subprocess.run(
                [*common, "ssh-add -l"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert listed.returncode == 0, listed.stderr
            assert "msksd:alpha" in listed.stdout, listed.stdout
    finally:
        sshd.terminate()
        sshd.wait(timeout=10)
        sshd_log.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_listening(
    port: int, timeout_s: float = 10.0, evidence: Path | None = None
) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    detail = ""
    if evidence is not None and evidence.exists():
        detail = f"; sshd log:\n{evidence.read_text(errors='replace')[-800:]}"
    raise AssertionError(f"sshd never listened on {port}{detail}")


# --- the per-workspace known_hosts file ---


def test_known_hosts_path_creates_its_directory(tmp_path: Path) -> None:
    path = ssh.known_hosts_path("alpha", base=tmp_path)
    assert path == str(tmp_path / "alpha" / "known_hosts")
    assert (tmp_path / "alpha").is_dir()


def test_known_hosts_path_separates_workspace_instances(
    tmp_path: Path,
) -> None:
    """#245: two workspaces created under one name cache their host
    keys apart — a recreated instance presents fresh first-boot
    keys, and the name-keyed cache refused the change forever."""
    first = ssh.instance_token({"created_at": "2026-09-22 10:12:24"})
    second = ssh.instance_token({"created_at": "2026-09-23 09:00:00"})
    assert first and second and first != second
    one = ssh.known_hosts_path("ws", base=tmp_path, instance=first)
    two = ssh.known_hosts_path("ws", base=tmp_path, instance=second)
    legacy = ssh.known_hosts_path("ws", base=tmp_path)
    assert one != two and one != legacy
    assert one.endswith(f"/ws.{first}/known_hosts")
    # A key without the stamp (an older daemon) keeps the legacy path.
    assert ssh.instance_token({}) is None
    assert ssh.instance_token({"created_at": None}) is None


def test_cache_key_uses_the_immutable_id_when_served(tmp_path: Path) -> None:
    """#246: a daemon that serves the workspace's id keys the
    host-key cache on it — the id names the instance, so two
    workspaces created under one name never share a cache and no
    created_at suffix is needed. A daemon without the field keeps
    the #245 typed-ref-plus-stamp shape."""
    keyed = ssh.cache_key(
        {"id": "1a2b3c4d5e6f7890", "created_at": "2026-10-04"}, "ws"
    )
    assert keyed == ("1a2b3c4d5e6f7890", None)
    stamp = ssh.instance_token({"created_at": "2026-10-04 10:00:00"})
    legacy = ssh.cache_key({"created_at": "2026-10-04 10:00:00"}, "ws")
    assert legacy == ("ws", stamp)
    one = ssh.known_hosts_path(keyed[0], base=tmp_path, instance=keyed[1])
    other = ssh.known_hosts_path("ws", base=tmp_path, instance=stamp)
    assert one != other


def test_known_hosts_path_names_an_unusable_cache(tmp_path: Path) -> None:
    taken = tmp_path / "alpha"
    taken.write_text("a file where the cache dir should be")
    with pytest.raises(SystemExit, match="cannot create"):
        ssh.known_hosts_path("alpha", base=tmp_path)


def test_client_identity_path_lives_under_the_data_root(
    tmp_path: Path,
) -> None:
    assert ssh.client_identity_path("alpha", base=tmp_path) == (
        tmp_path / "alpha" / "identity"
    )


def test_identity_dir_keys_on_the_served_id() -> None:
    """#246: the identity directory keys on the row's immutable id
    when the daemon serves it, else the typed reference."""
    assert ssh.identity_dir({"id": "1a2b3c4d"}, "ws") == "1a2b3c4d"
    assert ssh.identity_dir({}, "ws") == "ws"


def test_resolve_private_prefers_the_daemon_half() -> None:
    """A daemon-minted workspace (#111) hands its private half over
    the API; that half wins when present."""
    assert ssh.resolve_private(KEY, "alpha") == PEM


def test_resolve_private_falls_back_to_the_client_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A client-minted workspace (#121) answers private_key: null;
    the private half then comes from the local cache, written at
    create — and it must be the pair's other half, checked against
    the served public line."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    private_pem, public = mint("ecdsa")
    path = tmp_path / "msks" / "alpha" / "identity"
    path.parent.mkdir(parents=True)
    path.write_text(private_pem)
    key = {
        "public_key": f"{public} msks-client:alpha",
        "private_key": None,
    }
    assert ssh.resolve_private(key, "alpha") == private_pem


def test_resolve_private_names_a_stale_cached_half(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cache entry from a previous incarnation of the workspace id
    fails as one named line — not as ssh's opaque publickey denial
    deep inside a session."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    stale_pem, _stale_public = mint("ecdsa")
    path = tmp_path / "msks" / "alpha" / "identity"
    path.parent.mkdir(parents=True)
    path.write_text(stale_pem)
    _current_pem, current_public = mint("ecdsa")
    key = {
        "public_key": f"{current_public} msks-client:alpha",
        "private_key": None,
    }
    with pytest.raises(SystemExit, match="does not match"):
        ssh.resolve_private(key, "alpha")


def test_resolve_private_names_a_corrupt_cached_half(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file that is not a private key at all fails as one named
    line too — the module's error contract holds for every way the
    cache can be wrong."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = tmp_path / "msks" / "alpha" / "identity"
    path.parent.mkdir(parents=True)
    path.write_text("not a key")
    key = {
        "public_key": "ecdsa-sha2-nistp256 AAAA msks-client:alpha",
        "private_key": None,
    }
    with pytest.raises(SystemExit, match="not a usable private key"):
        ssh.resolve_private(key, "alpha")


def test_resolve_private_names_a_missing_client_half(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No daemon half and no local file: one SystemExit line naming
    both recoveries — minted on another client, or the operator's
    own key — with the path and the console fallback, not a
    traceback (#121, #132)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MSKSC_IDENTITY_FILE", raising=False)
    monkeypatch.delenv("MSKSC_DATA_DIR", raising=False)
    with pytest.raises(SystemExit, match="another client"):
        ssh.resolve_private({"public_key": "x", "private_key": None}, "alpha")


def identity_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A deterministic operator-identity environment (#336): a
    fresh data root, no ambient identity_file."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("MSKSC_IDENTITY_FILE", raising=False)
    monkeypatch.delenv("MSKSC_DATA_DIR", raising=False)


def plant_operator_key(monkeypatch, tmp_path) -> tuple[str, str, Path]:
    """One operator key named by identity_file: ``(pem, public,
    path)``."""
    pem, public = mint("ed25519")
    mine = tmp_path / "my-key"
    mine.write_text(pem)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(mine))
    return pem, public, mine


def test_identity_file_outranks_the_minted_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A workspace planted with the operator's key (#336, the
    create default) has no per-workspace file: the private half
    comes from identity_file, checked against the served public
    line — and the data root's minted key loses to it when both
    exist."""
    identity_home(monkeypatch, tmp_path)
    from msks.client.create import mint_operator_identity

    mint_operator_identity()  # the losing rung
    pem, public, _mine = plant_operator_key(monkeypatch, tmp_path)
    key = {"public_key": f"{public} msks-client:ws", "private_key": None}
    assert ssh.resolve_private(key, "ws1") == pem


def test_resolve_private_matches_the_minted_operator_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key msks minted to <data_dir>/identity at create is the
    second rung: an operator-key workspace resolves through it with
    identity_file unset (#336)."""
    identity_home(monkeypatch, tmp_path)
    from msks.client.create import mint_operator_identity, public_line

    pem, path = mint_operator_identity()
    public = public_line(pem)
    assert path == tmp_path / "data" / "msks" / "identity"
    key = {"public_key": f"{public} x", "private_key": None}
    assert ssh.resolve_private(key, "ws1") == pem


def test_resolve_private_keeps_access_across_a_recreated_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A workspace re-created under the same name gets a new id but
    the same operator key: the new row's per-workspace path is
    empty and the operator rung answers, so the "re-created since
    that key was stored" failure mode disappears for operator-key
    workspaces (#336)."""
    identity_home(monkeypatch, tmp_path)
    pem, public, _mine = plant_operator_key(monkeypatch, tmp_path)
    # The OLD incarnation's per-workspace half still sits under the
    # old id — a stale half for a row that no longer exists.
    old_pem, _old_public = mint("ecdsa")
    old = tmp_path / "data" / "msks" / "old-id" / "identity"
    old.parent.mkdir(parents=True)
    old.write_text(old_pem)
    new = {"public_key": f"{public} msks-client:ws", "private_key": None}
    assert ssh.resolve_private(new, "new-id") == pem


def test_resolve_private_falls_past_a_stale_half_to_the_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale per-workspace half (the id's earlier incarnation)
    falls through to the operator identity before the refusal: the
    row was re-created as an operator-key workspace (#336)."""
    identity_home(monkeypatch, tmp_path)
    from msks.client.create import mint_operator_identity, public_line

    pem, _path = mint_operator_identity()
    stale_pem, _stale_public = mint("ecdsa")
    path = tmp_path / "data" / "msks" / "ws1" / "identity"
    path.parent.mkdir(parents=True)
    path.write_text(stale_pem)
    key = {
        "public_key": f"{public_line(pem)} msks-client:ws",
        "private_key": None,
    }
    assert ssh.resolve_private(key, "ws1") == pem


def test_resolve_private_recovery_names_identity_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When nothing local matches — no per-workspace half, and an
    operator identity that either resolves to a different key or
    does not resolve at all — the recovery line names the
    identity_file setting among the rungs it tried (#336)."""
    identity_home(monkeypatch, tmp_path)
    # A resolving operator key that pairs with a DIFFERENT public
    # half: the rung declines, not guesses.
    _pem, _public, _mine = plant_operator_key(monkeypatch, tmp_path)
    key = {"public_key": "ssh-ed25519 AAAAnomatch x", "private_key": None}
    with pytest.raises(SystemExit) as caught:
        ssh.resolve_private(key, "ws1")
    line = str(caught.value)
    assert "MSKSC_IDENTITY_FILE" in line
    assert "another client" in line
    # And with nothing set at all: no identity resolves, the same
    # refusal answers.
    monkeypatch.delenv("MSKSC_IDENTITY_FILE")
    with pytest.raises(SystemExit, match="matched nothing"):
        ssh.resolve_private(key, "ws1")


def test_resolve_private_names_a_broken_identity_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A set identity_file that is missing or unusable is one named
    line pointing at the setting — not the generic recovery, and
    not a traceback (#336)."""
    identity_home(monkeypatch, tmp_path)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(tmp_path / "gone"))
    with pytest.raises(SystemExit, match="operator identity file"):
        ssh.resolve_private({"public_key": "x", "private_key": None}, "ws1")
    junk = tmp_path / "junk"
    junk.write_text("not a key")
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(junk))
    with pytest.raises(SystemExit, match="OpenSSH format"):
        ssh.resolve_private({"public_key": "x", "private_key": None}, "ws1")
    locked = tmp_path / "locked"
    unencrypted, _public = mint("ed25519")
    key_obj = serialization.load_ssh_private_key(
        unencrypted.encode(), password=b""
    )
    locked.write_text(
        key_obj.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
        ).decode()
    )
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(locked))
    with pytest.raises(SystemExit, match="encrypted"):
        ssh.resolve_private({"public_key": "x", "private_key": None}, "ws1")


def test_a_relative_identity_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative identity_file resolves per working directory —
    a different key planted from every directory — so it is refused
    with the fix named, the same rule the state roots carry
    (#336)."""
    identity_home(monkeypatch, tmp_path)
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", "keys/id_ed25519")
    with pytest.raises(SystemExit, match="absolute path"):
        ssh.operator_identity()


def test_the_agent_stages_the_wide_nist_curves() -> None:
    """The transient agent signs P-384 and P-521 keys (#336): an
    operator's own key at those curves must plant AND let msks ssh
    in — the mint never produces them, so the keys are built here."""
    for curve, algo in (
        (ec.SECP384R1(), "ecdsa-sha2-nistp384"),
        (ec.SECP521R1(), "ecdsa-sha2-nistp521"),
    ):
        private = ec.generate_private_key(curve)
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        loaded = agent.load_private(pem)
        assert agent.signable(loaded)
        _blob, wire_algo = agent.public_parts(loaded)
        assert wire_algo == algo
        sig = agent.sign(loaded, b"challenge", 0)
        assert sig is not None
        assert sig.startswith(agent.wire_string(algo.encode()))


def test_the_staged_key_must_be_signable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A key that loads but cannot sign never becomes the operator
    identity: the minted-key rung silently declines it, and an
    identity_file that names one is a named refusal pointing at
    --pubkey. The loader today accepts only signable types, so the
    guard is driven with a stub key (future cryptography support
    widens the loader; the guard is what keeps the promise)."""
    identity_home(monkeypatch, tmp_path)
    pem, _public = mint("ed25519")
    minted = tmp_path / "data" / "msks" / "identity"
    minted.parent.mkdir(parents=True)
    minted.write_text(pem)
    real_load = agent.load_private

    def stub_loader(text: str):
        # The one planted key "loads" to an object the agent has
        # no signer for; every other text loads for real.
        return SimpleNamespace() if text == pem else real_load(text)

    monkeypatch.setattr(ssh.agent, "load_private", stub_loader)
    assert ssh.operator_identity() is None
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(minted))
    with pytest.raises(SystemExit, match="cannot stage"):
        ssh.operator_identity()


def test_cache_dir_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    assert ssh.cache_dir() == Path("/tmp/xdg-cache/msks")


def test_cache_dir_honors_msksc_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MSKSC_CACHE_DIR` names the root itself and wins over the
    XDG base (#251): a checkout points it at its own state and
    the shared `~/.cache/msks` tree stays out of the session."""
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    monkeypatch.setenv("MSKSC_CACHE_DIR", "/tmp/per-checkout-cache")
    assert ssh.cache_dir() == Path("/tmp/per-checkout-cache")


def test_data_dir_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    assert ssh.data_dir() == Path("/tmp/xdg-data/msks")


def test_data_dir_honors_msksc_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MSKSC_DATA_DIR` names the root itself and wins over the
    XDG base (#251) — separately from the cache variable, since
    the minted identities outlive a disposable cache."""
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("MSKSC_DATA_DIR", "/tmp/durable-identities")
    assert ssh.data_dir() == Path("/tmp/durable-identities")


def test_cache_dir_expands_a_home_relative_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leading ``~`` expands, matching the XDG fallback branches
    (#251): `~/state` names a home directory, not a literal
    ``~/state`` under the CWD."""
    monkeypatch.setenv("MSKSC_CACHE_DIR", "~/state/cache")
    assert ssh.cache_dir() == Path.home() / "state" / "cache"


def test_data_dir_refuses_a_relative_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative root names a different directory from every
    working directory — the one thing a state root must not do —
    so it is one named line, not a silent CWD-dependent location
    (#251)."""
    monkeypatch.setenv("MSKSC_DATA_DIR", "rel/identities")
    with pytest.raises(SystemExit, match="MSKSC_DATA_DIR"):
        ssh.data_dir()


@pytest.mark.parametrize("variable", ["MSKSC_CACHE_DIR", "MSKSC_DATA_DIR"])
def test_state_dir_empty_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """An empty value is the unset state — the same rule the
    shell preset applies — so the XDG root answers (#251)."""
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv(variable, "")
    assert ssh.cache_dir() == Path("/tmp/xdg-cache/msks")
    assert ssh.data_dir() == Path("/tmp/xdg-data/msks")


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
def test_passthrough_args_passes_verbatim(
    raw: list[str], expected: list[str]
) -> None:
    assert ssh.passthrough_args(raw) == expected


@pytest.mark.parametrize(
    ("args", "split"),
    [
        (["-A"], (["-A"], [])),
        (["-A", "--", "uname"], (["-A"], ["uname"])),
        (["--", "uname"], ([], ["uname"])),
        (["--", "--", "x"], ([], ["--", "x"])),  # split at the first --
        # The natural form: a passthrough that starts with a plain
        # word is all command (ssh would read it as the destination).
        (["uname", "-a"], ([], ["uname", "-a"])),
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
        (["-o", "user=root"], True),  # ssh keywords are case-insensitive
        (["-o", "USER=root"], True),
        (["-o", "User root"], True),
        (["-o", "ProxyCommand=x"], False),
        (["-A"], False),
        (["-l"], True),  # dangling: ssh's own error is the clear one
        (["-lroot"], True),  # the attached -l spelling ssh accepts
        (["-L8080:localhost:80"], False),  # -L (a forward) is not -l
        (["-oUser=root"], True),  # the inline -o form
        (["-oProxyCommand=x"], False),
    ],
)
def test_wants_user(args: list[str], names: bool) -> None:
    assert ssh.wants_user(args) is names


# --- the argv ---


def test_build_args_injects_the_workspace_user() -> None:
    """No user in the passthrough: the workspace's login user rides
    as ``-l`` (#248) — the name the daemon recorded at create, not
    an image constant."""
    argv = ssh.build_args(
        "alpha", "/agent.sock", "/id.pub", "/kh", ["-T"], "alice"
    )
    assert argv[0] == "ssh"
    assert argv[1] == "-T"  # passthrough options come first...
    assert argv[2:4] == ["-o", ssh.proxy_command("alpha")]
    assert argv[4:14] == [
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
    ]
    assert argv[14:] == ["-l", "alice", "alpha"]


def test_build_args_leaves_the_user_to_ssh() -> None:
    argv = ssh.build_args(
        "alpha", "/agent.sock", "/id.pub", "/kh", ["-l", "root"], "alice"
    )
    assert argv[1:3] == ["-l", "root"]
    assert argv[-1] == "alpha"
    assert "-l" not in argv[3:]  # the default is not injected twice


def test_build_args_lets_an_explicit_override_win() -> None:
    """ssh takes the first obtained value for a repeated option, so
    a passthrough override lands before the injected defaults —
    exactly the stock-ssh override shape."""
    argv = ssh.build_args(
        "alpha",
        "/agent.sock",
        "/id.pub",
        "/kh",
        [
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=no",
        ],
        "alice",
    )
    assert argv[1:5] == [
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=no",
    ]
    assert argv[5] == "-o"  # the injected defaults follow, not precede


def test_build_args_carries_a_remote_command_after_the_host() -> None:
    argv = ssh.build_args(
        "alpha",
        "/agent.sock",
        "/id.pub",
        "/kh",
        ["-v", "--", "uname", "-a"],
        "alice",
    )
    assert argv[0:2] == ["ssh", "-v"]
    assert argv[-3:] == ["alpha", "uname", "-a"]


# --- agent forwarding onto the operator's agent (#174) ---


@pytest.fixture
def agent_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An SSH_AUTH_SOCK naming an existing socket — the operator's
    agent, exactly as the environment sees one."""
    sock = tmp_path / "agent.sock"
    sock.touch()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
    return str(sock)


def front_pair(agent_env: str) -> list[str]:
    """The argv tokens that state the operator's socket at the
    front of the options."""
    return ["-o", f"ForwardAgent={agent_env}"]


@pytest.mark.parametrize(
    "raw",
    [
        ["-A"],
        ["-o", "ForwardAgent=yes"],
        ["-oForwardAgent=yes"],
        ["-o", "forwardagent=YES"],  # keywords are caseless
        ["-o", "ForwardAgent yes"],  # space-separated value
        ["-o", "ForwardAgent\tyes"],  # any whitespace separates
        ["-o", "ForwardAgent =yes"],  # spaced equals
        ["-o", "ForwardAgent= yes"],  # padded value
        ["-o", "ForwardAgent = yes"],
        ["-o", "ForwardAgent=yes "],
        ["-o", 'ForwardAgent="yes"'],  # quotes ssh strips
        ["-A", "-o", "User=root"],
        ["-o", "ForwardAgent=no", "-A"],  # the flag beats the value
        ["-o", "ForwardAgent=yes", "-o", "ForwardAgent=no"],
        ["-a", "-A"],  # ON: the later flag assigns
        ["-va", "-A"],
        ["-vA"],  # a bundled request resolves the same way
        ["-tA"],
        ["-At"],
        ["-voForwardAgent=yes"],  # a request on a bundle's -o
        ["-vo", "ForwardAgent=yes"],
    ],
)
def test_a_resolved_request_states_the_operator_socket(
    agent_env: str, raw: list[str]
) -> None:
    """Every spelling stock ssh resolves to forwarding — the -A
    flag (last one wins), the first yes-valued ForwardAgent, the
    bundled forms — gets the operator's socket stated at the front,
    where a path wins over every flag and value behind it."""
    assert ssh.forward_agent_args(list(raw)) == [
        *front_pair(agent_env),
        *raw,
    ]


@pytest.mark.parametrize(
    "raw",
    [
        ["-o", "ForwardAgent=no"],
        ["-o", "ForwardAgent=/other/sock"],  # an explicit socket
        ["-oForwardAgent=/other/sock"],
        ["-a"],
        ["-l", "root"],
        ["-T"],
        ["-ta"],  # a bundle that disables, with no request behind it
        [],
        # stock resolves every one of these to OFF: the later flag
        # assigns over the value, and plain values are first-obtained
        ["-A", "-a"],
        ["-o", "ForwardAgent=yes", "-a"],
        ["-A", "-va"],
        ["-a", "-o", "ForwardAgent=yes"],
        ["-o", "ForwardAgent=no", "-o", "ForwardAgent=yes"],
        ["-vo", "ForwardAgent=no"],  # a no on a bundle's -o
        ["-vo"],  # dangling: ssh's own usage error answers
    ],
)
def test_stock_off_lines_pass_through_untouched(raw: list[str]) -> None:
    """A line stock ssh resolves to off (or that names no
    forwarding at all) passes through verbatim — no agent resolved,
    no option added."""
    assert ssh.forward_agent_args(list(raw)) == raw


@pytest.mark.parametrize(
    "raw",
    [
        ["-A", "-o", "ForwardAgent=/own"],
        ["-o", "ForwardAgent=/own", "-A"],
        ["-o", "ForwardAgent=/own", "-o", "ForwardAgent=yes"],
        ["-vA", "-o", "ForwardAgent=/own"],
        ["-A", "-vo", "ForwardAgent=/own"],  # explicit on a bundle
        ["-A", "-o", "ForwardAgent=sock.rel"],  # relative is a socket
        ["-A", "-o", "ForwardAgent="],  # empty: ssh's usage error
        ["-A", "-o", "ForwardAgent=banana"],  # garbage reads as a path
        ["-A", "-o", "ForwardAgent\t/own"],  # tab separator
        ["-A", "-o", "ForwardAgent=/own", "-a"],  # sticky over -a
    ],
)
def test_an_explicit_socket_wins_over_everything(raw: list[str]) -> None:
    """A ForwardAgent value naming a socket — path (absolute or
    relative), garbage, empty — is sticky in stock ssh: it wins over
    every flag and value in any order, so the rewrite stands down
    entirely, without resolving any agent (no live SSH_AUTH_SOCK
    needed here)."""
    assert ssh.forward_agent_args(list(raw)) == raw


def test_value_flag_bundles_are_not_requests() -> None:
    """The value-taking options own everything after themselves, so
    a capital A inside a value (-JAdmin@h, -vR8000:Alpha:80,
    -vEAuth.log, -vcArcfour) names no forwarding."""
    for arg in (
        "-JAdmin@h",
        "-vR8000:Alpha:80",
        "-BAgent0",
        "-vEAuth.log",
        "-vcArcfour",
    ):
        assert ssh.forward_agent_args([arg]) == [arg]


def test_forward_agent_args_needs_no_agent_when_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A session that stock ssh resolves to off runs with no agent
    # in sight.
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert ssh.forward_agent_args(["-A", "-a"]) == ["-A", "-a"]
    assert ssh.forward_agent_args(["-T"]) == ["-T"]


@pytest.mark.parametrize("raw", [["-A"], ["-o", "ForwardAgent=yes"], ["-vA"]])
def test_forward_agent_args_names_a_missing_agent(
    monkeypatch: pytest.MonkeyPatch, raw: list[str]
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/no/such/agent.sock")
    with pytest.raises(SystemExit, match="no agent socket"):
        ssh.forward_agent_args(list(raw))


def test_forward_agent_args_names_an_unset_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    with pytest.raises(SystemExit, match=r"SSH_AUTH_SOCK \(unset\)"):
        ssh.forward_agent_args(["-A"])


def test_a_socket_with_whitespace_is_quoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "agent sock"
    sock.touch()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
    assert ssh.forward_agent_args(["-A"]) == [
        "-o",
        f'ForwardAgent="{sock}"',
        "-A",
    ]


def test_build_args_points_forwarding_at_the_operators_agent(
    agent_env: str,
) -> None:
    argv = ssh.build_args(
        "alpha", "/agent.sock", "/id.pub", "/kh", ["-A"], "alice"
    )
    assert argv[1:3] == front_pair(agent_env)
    assert "-A" in argv  # inert behind the stated path
    # The session agent stays the authentication path — the
    # workspace identity authenticates and reaches the guest as
    # nothing else.
    assert "-o" in argv and "IdentityAgent=/agent.sock" in " ".join(argv)


def test_build_args_keeps_an_explicit_forwardagent_socket(
    agent_env: str,
) -> None:
    argv = ssh.build_args(
        "alpha",
        "/agent.sock",
        "/id.pub",
        "/kh",
        ["-o", "ForwardAgent=/own"],
        "alice",
    )
    assert "ForwardAgent=/own" in " ".join(argv)
    assert agent_env not in " ".join(argv)


def test_probe_args_carries_the_sessions_forwarding(
    agent_env: str,
) -> None:
    argv = probe_argv_of(["-A"])
    assert argv[2:4] == front_pair(agent_env)
    assert argv[argv.index("alpha") + 1 :] == ["true"]


def test_probe_args_keeps_an_inline_forwardagent_socket() -> None:
    # An operator's own socket in the inline spelling reaches the
    # probe in the same spelling ssh parsed it in.
    argv = probe_argv_of(["-oForwardAgent=/own.sock"])
    assert "-oForwardAgent=/own.sock" in argv
    assert argv[argv.index("alpha") + 1 :] == ["true"]


def test_probe_args_without_forwarding_carries_none() -> None:
    assert "ForwardAgent" not in " ".join(probe_argv_of([]))


def test_probe_args_skips_a_dangling_login_before_agent_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ssh refuses a dangling -l before it would dial — the wait is
    # skipped without resolving any agent.
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert probe_argv_of(["-l"]) is None
    assert probe_argv_of(["-l", "-A"]) is None


# --- prepare: the boot pre-flight and the key fetch ---


def test_prepare_boots_then_fetches() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(200, json=KEY)
        return httpx.Response(200, json=RUNNING_ROW)

    key, booted = asyncio.run(
        ssh.prepare("alpha", "https://daemon", "tok", transport=mock(handler))
    )
    assert key == KEY
    assert booted is False  # the workspace was already running
    assert seen == [
        "GET /api/v1/workspaces/alpha",
        "GET /api/v1/workspaces/alpha/ssh-key",
    ]


def test_prepare_reports_the_boot_it_performed() -> None:
    stopped = {"id": "alpha", "status": "stopped"}
    statuses = iter([stopped, stopped])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "alpha"})
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(200, json=KEY)
        return httpx.Response(200, json=next(statuses))

    key, booted = asyncio.run(
        ssh.prepare("alpha", "https://daemon", "tok", transport=mock(handler))
    )
    assert key == KEY
    assert booted is True


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
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, False

    stopped: list = []
    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )

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
        return SimpleNamespace(returncode=7, stderr="")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    rc = ssh.run_workspace_ssh("alpha", ["-l", "root"])
    assert rc == 7
    argv = calls[0]["argv"]
    assert argv[0] == "ssh"
    assert "IdentityAgent=/faked/agent.sock" in argv
    assert argv[-1] == "alpha"
    assert argv[1:3] == ["-l", "root"]
    assert stopped == ["stopped"]  # the agent tears down after ssh exits


def test_run_workspace_ssh_names_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, False

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )

    def missing(argv, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(ssh.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="ssh not found"):
        ssh.run_workspace_ssh(
            "alpha", []
        )  # the real agent stops around the failure


def test_run_workspace_ssh_names_a_missing_binary_from_the_wait(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    # The booted path probes before the session: a missing ssh is
    # the same one-line exit there, not a traceback from the probe.
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, True

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )

    @contextmanager
    def fake_serve(private, comment):
        yield FakeAgent()

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)

    def missing(argv, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(ssh.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="ssh not found"):
        ssh.run_workspace_ssh("alpha", [])


# --- the first-boot wait (#168) ---


def probe_argv_of(passthrough: list[str]) -> list[str]:
    """The probe argv for a passthrough, through build_args itself."""
    return ssh.probe_args(
        "alpha",
        "/faked/agent.sock",
        "/faked/identity.pub",
        "/kh",
        passthrough,
        "alice",
    )


def test_probe_args_carries_the_user_and_a_true_command() -> None:
    argv = probe_argv_of(["-l", "root"])
    assert argv[0] == "ssh"
    assert "-q" in argv  # the probe keeps its attempts to one line
    assert "-l" in argv and "root" in argv
    assert argv[argv.index("alpha") + 1 :] == ["true"]


def test_probe_args_never_carries_a_command_form_passthrough() -> None:
    # A command-first passthrough ("msks ssh ws -- uname -a") is all
    # command to split_command, so its words cannot reach the probe —
    # the double-run guard this whole wait exists for.
    argv = probe_argv_of(["uname", "-a"])
    assert argv[argv.index("alpha") + 1 :] == ["true"]
    assert "uname" not in argv


def test_probe_args_drops_all_session_luggage() -> None:
    # Tunnels, forwards, verbosity — bundled spellings included —
    # and RemoteCommand cannot reach the probe: -N and
    # SessionType=none would hold it open running nothing, -W never
    # ends on its own, and RemoteCommand refuses a command-line
    # command outright. The session keeps every one (pinned below).
    passthrough = [
        "-fN",
        "-L",
        "8080:localhost:80",
        "-W",
        "localhost:22",
        "-oRemoteCommand=sleep 600",
        "-v",
        "-l",
        "root",
    ]
    argv = probe_argv_of(passthrough)
    tail = argv[argv.index("alpha") + 1 :]
    assert tail == ["true"]
    for word in (
        "-fN",
        "-L",
        "8080:localhost:80",
        "-W",
        "localhost:22",
        "-v",
    ):
        assert word not in argv
    assert "RemoteCommand" not in " ".join(argv)
    # The login user is the one setting the probe keeps.
    assert "-l" in argv and "root" in argv
    session = ssh.build_args(
        "alpha",
        "/faked/agent.sock",
        "/faked/identity.pub",
        "/kh",
        passthrough,
        "alice",
    )
    assert "-fN" in session and "-W" in session
    assert "-oRemoteCommand=sleep 600" in session


def test_probe_user_extracts_each_user_form() -> None:
    # -l in both spellings, split and inline -o User=..., all kept;
    # a dangling -l (an option where its value would be) stays out
    # so the probe falls back — probe_args then skips the wait
    # entirely, pinned below — and an uppercase -L forward names no
    # user.
    assert ssh.probe_user(["-l", "root"]) == ["-l", "root"]
    assert ssh.probe_user(["-lroot"]) == ["-lroot"]
    assert ssh.probe_user(["-o", "User=root"]) == ["-o", "User=root"]
    assert ssh.probe_user(["-oUser=root"]) == ["-oUser=root"]
    assert ssh.probe_user(["-l", "-A"]) == []
    assert ssh.probe_user(["-A", "-T"]) == []
    assert ssh.probe_user(["-L8080:localhost:80"]) == []


def test_probe_args_carries_the_attached_login_spelling() -> None:
    # ssh parses -lroot as user root and takes the first user it
    # sees, so the probe must carry it too — not the workspace user.
    argv = probe_argv_of(["-lroot"])
    assert "-lroot" in argv
    assert "alice" not in argv


def test_probe_args_shares_the_sessions_transport_options() -> None:
    # The probe authenticates through the same ProxyCommand,
    # known_hosts, host-key policy, agent, and identity — the
    # injected option block is the session's own, verbatim.
    session = ssh.build_args(
        "alpha", "/faked/agent.sock", "/faked/identity.pub", "/kh", [], "alice"
    )
    probe = probe_argv_of([])
    injected = session[1:-2]  # no passthrough options: the whole tail
    assert probe[2 : 2 + len(injected)] == injected


def test_probe_args_refuses_a_session_no_probe_can_mirror() -> None:
    # A dangling -l names a user ssh will refuse outright — the
    # session answers with its own immediate usage error, so no
    # probe is built and the wait is skipped.
    assert probe_argv_of(["-l"]) is None
    assert probe_argv_of(["-l", "-A"]) is None


def test_probe_args_keeps_a_defaulted_user_from_double_injection() -> None:
    argv = probe_argv_of([])
    # wants_user saw no user in the options, so the default login rides
    # the probe exactly as it rides the real session.
    assert "-l" in argv
    assert argv[argv.index("alpha") + 1 :] == ["true"]


def test_run_workspace_ssh_waits_out_a_first_boot(
    monkeypatch: pytest.MonkeyPatch,
    client_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, True

    sleeps: list[float] = []

    def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )
    monkeypatch.setattr(ssh.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(ssh.time, "sleep", fast_sleep)

    @contextmanager
    def fake_serve(private, comment):
        yield FakeAgent()

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)
    codes = iter([255, 255, 0, 7])  # two refusals, then seeded; real: 7
    commands: list[list[str]] = []

    def fake_run(argv, **kwargs) -> SimpleNamespace:
        commands.append(argv)
        clock["now"] += 1.0
        return SimpleNamespace(returncode=next(codes), stderr="")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    rc = ssh.run_workspace_ssh("alpha", [])
    assert rc == 7
    assert len(commands) == 4  # three probes, then the one real session
    assert commands[-1][-1] == "alpha"  # the real session carries no probe
    assert all(c[-1] == "true" for c in commands[:-1])
    assert sleeps == [ssh.SSH_RETRY_PAUSE_S, ssh.SSH_RETRY_PAUSE_S]
    err = capsys.readouterr().err
    assert err.count("not accepting the login yet") == 2


def test_run_workspace_ssh_treats_a_stalled_probe_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    client_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, True

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(ssh.time, "monotonic", fake_monotonic)

    def advance(seconds: float) -> None:
        clock["now"] += 1.0

    monkeypatch.setattr(ssh.time, "sleep", advance)

    @contextmanager
    def fake_serve(private, comment):
        yield FakeAgent()

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)
    timeouts: list[float] = []

    def stalled_then_ready(argv, timeout=None, **kwargs):
        clock["now"] += 1.0
        if timeout is not None:
            timeouts.append(timeout)
        if len(timeouts) == 1:
            # A wedged forward never answers: the attempt is bounded
            # by the time the deadline leaves, not by the stall.
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
        if argv[-1] == "true":
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=7, stderr="")

    monkeypatch.setattr(ssh.subprocess, "run", stalled_then_ready)
    monkeypatch.setattr(ssh, "SSH_SEED_WAIT_S", 30.0)
    rc = ssh.run_workspace_ssh("alpha", [])
    assert rc == 7
    # 30s deadline at t=0; the stall consumed 1s, the pause 1s more.
    assert timeouts == [30.0, 28.0]
    err = capsys.readouterr().err
    assert err.count("not accepting the login yet") == 1


def test_run_workspace_ssh_stops_waiting_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    client_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, True

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(ssh.time, "monotonic", fake_monotonic)

    def advance(seconds: float) -> None:
        clock["now"] += 10.0

    monkeypatch.setattr(ssh.time, "sleep", advance)

    @contextmanager
    def fake_serve(private, comment):
        yield FakeAgent()

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)
    count = {"n": 0}

    def fake_run(argv, **kwargs) -> SimpleNamespace:
        count["n"] += 1
        clock["now"] += 1.0
        # The probe never succeeds; the real session runs regardless
        # and stands or falls on its own.
        return SimpleNamespace(
            returncode=0 if argv[-1] != "true" else 255, stderr=""
        )

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    monkeypatch.setattr(ssh, "SSH_SEED_WAIT_S", 3.0)
    rc = ssh.run_workspace_ssh("alpha", [])
    assert rc == 0
    # One probe at t=0; the pause lands at t=11, past the 3s
    # deadline, so the loop stops before a second attempt and the
    # real session runs exactly once.
    assert count["n"] == 2


def test_run_workspace_ssh_skips_the_wait_when_not_booted(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, False

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )

    @contextmanager
    def fake_serve(private, comment):
        yield FakeAgent()

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)
    commands: list[list[str]] = []

    def fake_run(argv, **kwargs) -> SimpleNamespace:
        commands.append(argv)
        return SimpleNamespace(returncode=255, stderr="")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    rc = ssh.run_workspace_ssh("alpha", [])
    assert rc == 255
    assert len(commands) == 1  # an already-running guest gets one dial


def test_run_workspace_ssh_skips_the_wait_when_no_probe_exists(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    # A dangling -l: probe_args answers None, the wait is skipped,
    # and the session fails once with ssh's own immediate error.
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, True

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )

    @contextmanager
    def fake_serve(private, comment):
        yield FakeAgent()

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)
    commands: list[list[str]] = []

    def fake_run(argv, **kwargs) -> SimpleNamespace:
        commands.append(argv)
        return SimpleNamespace(returncode=255, stderr="")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    rc = ssh.run_workspace_ssh("alpha", ["-l"])
    assert rc == 255
    assert len(commands) == 1


# --- CLI wiring ---


def test_cli_parses_the_ssh_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both spellings reach the body with the same verbatim list:
    the ``--`` separator drops, and options typed right after the
    workspace id travel untouched (argparse's REMAINDER shape,
    typer's interspersed-off parse — #315)."""
    calls: list = []
    monkeypatch.setattr(
        cli,
        "run_workspace_ssh",
        lambda ws, passthrough, transport=None: (
            calls.append((ws, passthrough)),
            5,
        )[1],
    )
    assert cli.main(["ssh", "alpha", "--", "-l", "root"]) == 5
    assert cli.main(["ssh", "alpha", "-A"]) == 5
    assert calls == [("alpha", ["-l", "root"]), ("alpha", ["-A"])]


def test_wait_for_identity_surfaces_distinct_probe_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#245: a permanent refusal (the host-key mismatch of a
    recreated workspace) rides the retry loop's notices — printed
    once per distinct answer, not drowned as "not ready"."""
    answers = iter(
        [
            "Host key verification failed.",
            "Host key verification failed.",
            "Permission denied (publickey).",
        ]
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(ssh.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        ssh.time, "sleep", lambda s: clock.update(now=clock["now"] + s)
    )

    def probe(argv, timeout=None, **kwargs):
        clock["now"] += 1.0
        return SimpleNamespace(returncode=255, stderr=next(answers) + "\n")

    monkeypatch.setattr(ssh.subprocess, "run", probe)
    # Budget for three probes: each probe ticks 1s, each pause 1s.
    ssh.wait_for_identity("ws", ["ssh"], clock["now"] + 4.5)
    err = capsys.readouterr().err
    assert err.count("Host key verification failed.") == 1
    assert err.count("Permission denied (publickey).") == 1
    assert err.count("msks ssh probe:") == 2  # deduped, then the new one


# --- the workspace's login user (#248) ---


def test_workspace_user_reads_the_served_name() -> None:
    """The identity fetch carries the workspace's login user: the
    name create recorded, served back as the session's default."""
    assert ssh.workspace_user(KEY) == "alice"


def test_workspace_user_falls_back_for_an_older_daemon() -> None:
    """A daemon predating the field serves no user: the image's own
    login user is the only account those workspaces hold, so it is
    the fallback — a constant named for its job, not the old
    always-default."""
    from msks.identity import LEGACY_LOGIN_USER

    assert ssh.workspace_user({"public_key": "k", "private_key": None}) == (
        LEGACY_LOGIN_USER
    )
    assert LEGACY_LOGIN_USER == "msks"


def test_run_workspace_ssh_logs_in_as_the_workspace_user(
    monkeypatch: pytest.MonkeyPatch,
    client_env: None,
    tmp_path: Path,
) -> None:
    """No user in the passthrough: the session dials as the
    workspace's recorded login user — the name the identity fetch
    served (#248)."""

    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, False

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        ssh,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )

    @contextmanager
    def fake_serve(private, comment):
        yield FakeAgent()

    monkeypatch.setattr(ssh.agent, "serve", fake_serve)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    assert ssh.run_workspace_ssh("alpha", []) == 0
    argv = calls[0]
    assert argv[argv.index("alpha") - 2 : argv.index("alpha")] == [
        "-l",
        "alice",
    ]
