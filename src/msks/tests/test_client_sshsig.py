"""SSHSIG for the console challenge (#123): the blob construction,
the in-process signer, and the agent client — every signature
round-tripped through the real ``ssh-keygen -Y verify``, the same
verdict the guest helper will reach.
"""

import base64
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from msks.client import agent, sshsig
from msks.identity import mint

KEYGEN = shutil.which("ssh-keygen")

needs_keygen = pytest.mark.skipif(
    KEYGEN is None,
    reason="ssh-keygen must be on PATH (the devenv shell ships it)",
)


def armor(body: bytes) -> str:
    """The armored file a verifier reads, from the wire body."""
    b64 = base64.b64encode(body).decode()
    wrapped = "\n".join(b64[i : i + 70] for i in range(0, len(b64), 70))
    return (
        f"-----BEGIN SSH SIGNATURE-----\n{wrapped}\n"
        "-----END SSH SIGNATURE-----\n"
    )


def verify(
    sig_body: bytes,
    payload: bytes,
    public_line: str,
    namespace: str,
    tmp_path: Path,
) -> bool:
    """ssh-keygen -Y verify: the guest helper's exact verdict."""
    fields = public_line.split()
    signers = tmp_path / "allowed_signers"
    signers.write_text(f"ws1 {fields[0]} {fields[1]}\n")
    sig = tmp_path / "sig"
    sig.write_text(armor(sig_body))
    payload_file = tmp_path / "payload"
    payload_file.write_bytes(payload)
    result = subprocess.run(
        [
            KEYGEN,
            "-Y",
            "verify",
            "-f",
            str(signers),
            "-I",
            "ws1",
            "-n",
            namespace,
            "-s",
            str(sig),
        ],
        input=payload,
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


@needs_keygen
@pytest.mark.parametrize("key_type", ["ecdsa", "ed25519", "rsa"])
def test_sign_payload_round_trips_through_ssh_keygen(
    key_type: str, tmp_path: Path
) -> None:
    """The in-process signer's output satisfies the guest's verifier
    for every minted type: the blob, the digest, the signature
    algorithm, and the armor all agree with PROTOCOL.sshsig."""
    private_pem, public = mint(key_type)
    body = sshsig.sign_payload(private_pem, b"nonce bytes", sshsig.NAMESPACE)
    assert base64.b64decode(body)  # the wire form is plain base64
    assert verify(
        base64.b64decode(body),
        b"nonce bytes",
        public,
        sshsig.NAMESPACE,
        tmp_path,
    )


@needs_keygen
def test_namespace_binds_the_signature(tmp_path: Path) -> None:
    """A signature for one namespace does not verify for another:
    the challenge cannot be answered by a signature minted for some
    other purpose."""
    private_pem, public = mint("ed25519")
    body = sshsig.sign_payload(private_pem, b"nonce", "other-namespace")
    assert not verify(
        base64.b64decode(body), b"nonce", public, sshsig.NAMESPACE, tmp_path
    )


@needs_keygen
def test_sign_via_agent_round_trips_and_needs_the_key(tmp_path: Path) -> None:
    """Through the agent protocol (here msks's own transient agent
    standing in for the operator's): the workspace's public line
    picks the identity, the signature verifies, and an agent that
    does not hold the key is refused with one line."""
    private_pem, public = mint("ed25519")
    other_pem, _other_public = mint("ed25519")
    private = agent.load_private(private_pem)
    with agent.serve(private, "test") as served:
        body = sshsig.sign_via_agent(
            served.server_address, public, b"nonce", sshsig.NAMESPACE
        )
        assert verify(
            base64.b64decode(body),
            b"nonce",
            public,
            sshsig.NAMESPACE,
            tmp_path,
        )
        # The wrong workspace key is not in the agent: one line, not
        # a traceback.
        with pytest.raises(SystemExit, match="produced no signature"):
            sshsig.sign_via_agent(
                served.server_address,
                f"{other_pem and ''}ssh-ed25519 AAAA wrong",
                b"nonce",
                sshsig.NAMESPACE,
            )


def test_agent_client_frame_round_trip(tmp_path: Path) -> None:
    """The client's framing against the real agent server: list,
    sign, and the failure answer both ways."""
    private_pem, public = mint("ecdsa")
    private = agent.load_private(private_pem)
    with agent.serve(private, "test") as served:
        client = sshsig.AgentClient(served.server_address)
        try:
            listed = client.identities()
            assert len(listed) == 1
            want = public.split()[1]
            assert base64.b64encode(listed[0][0]).decode() == want
            blob = sshsig.sshsig_blob(b"payload", sshsig.NAMESPACE)
            signature = client.sign(listed[0][0], blob, 0)
            assert signature is not None
            reader = agent.Reader(signature)
            assert reader.string() == b"ecdsa-sha2-nistp256"
            # A signature for a key the agent does not hold refuses.
            assert client.sign(b"nope", blob, 0) is None
        finally:
            client.close()


def test_environment_agent_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert sshsig.environment_agent() is None


def test_environment_agent_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    assert sshsig.environment_agent() is None  # not there yet
    (tmp_path / "agent.sock").write_bytes(b"")
    assert sshsig.environment_agent() == str(tmp_path / "agent.sock")


def test_mpint_padding() -> None:
    assert sshsig.mpint(0xFF) == struct.pack(">I", 2) + b"\x00\xff"
    assert sshsig.mpint(0) == struct.pack(">I", 1) + b"\x00"


def test_unsupported_ecdsa_curve_is_a_named_error() -> None:
    """A curve outside the wire names cannot produce a signature the
    guest would accept: it refuses before signing, not after."""
    from cryptography.hazmat.primitives.asymmetric import ec as curves

    odd = curves.generate_private_key(curves.SECP192R1())
    with pytest.raises(ValueError, match="unsupported ECDSA curve"):
        sshsig.ecdsa_hash(odd.curve)
    with pytest.raises(ValueError, match="unsupported ECDSA curve"):
        sshsig.sign_blob(odd, b"blob")


def _raw_agent(server_side):
    """A socketpair whose server end speaks canned agent answers."""
    import socket

    client, server = socket.socketpair()
    return client, server


def _serve(script, server):
    """Feed the scripted replies as the server end drains requests."""
    import threading

    def run():
        for reply in script:
            _drain_frame(server)
            if reply is not None:
                server.sendall(reply)
        server.close()

    threading.Thread(target=run, daemon=True).start()


def _drain_frame(sock) -> bytes:
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            return header
        header += chunk
    (length,) = struct.unpack(">I", header)
    body = b""
    while len(body) < length:
        body += sock.recv(length - len(body))
    return header + body


def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def test_agent_client_tolerates_a_broken_agent() -> None:
    """Every way a fake agent can break — silence (EOF), a garbage
    length, a failure answer — reads as no-identities, never a
    traceback or a hang."""
    import socket

    # EOF for reads while the send still lands: the peer shut its
    # write side down without answering.
    client, server = socket.socketpair()
    server.shutdown(socket.SHUT_WR)
    broken = sshsig.AgentClient.__new__(sshsig.AgentClient)
    broken.socket = client
    assert broken.exchange(b"x") is None
    client.close()
    # A length outside the ceiling.
    client, server = socket.socketpair()
    _serve([struct.pack(">I", 1 << 20) + b""], server)
    broken = sshsig.AgentClient.__new__(sshsig.AgentClient)
    broken.socket = client
    assert broken.exchange(b"x") is None
    client.close()
    # A short read cut by EOF.
    client, server = socket.socketpair()
    _serve([struct.pack(">I", 64) + b"partial"], server)
    broken = sshsig.AgentClient.__new__(sshsig.AgentClient)
    broken.socket = client
    assert broken.exchange(b"x") is None
    client.close()
    # A peer that closed outright: the first exchange drains (the
    # close's FIN reads as EOF), the second hits the dead send.
    client, server = socket.socketpair()
    server.close()
    broken = sshsig.AgentClient.__new__(sshsig.AgentClient)
    broken.socket = client
    assert broken.exchange(b"x") is None
    import time

    time.sleep(0.05)  # let the peer's reset land
    assert broken.exchange(b"x") is None
    client.close()
    # A FAILURE answer to the identities request.
    client, server = socket.socketpair()
    _serve([_frame(struct.pack("B", 5))], server)
    broken = sshsig.AgentClient.__new__(sshsig.AgentClient)
    broken.socket = client
    assert broken.identities() == []
    client.close()


def test_sign_via_agent_reports_a_refused_signature() -> None:
    """An agent that lists the key but refuses to sign (a constrained
    key, a canceled touch) is one readable line, not a hang."""
    import socket

    from msks.identity import mint as _mint

    pem, public = _mint("ed25519")
    want = base64.b64decode(public.split()[1])
    identities = _frame(
        struct.pack(">BI", 12, 1)
        + struct.pack(">I", len(want))
        + want
        + struct.pack(">I", 4)
        + b"held"
    )
    failure = _frame(struct.pack("B", 5))
    client, server = socket.socketpair()
    _serve([identities, failure], server)
    server_path = "/nonexistent"
    # Point the client's socket at the pair directly.
    original = sshsig.AgentClient.__init__

    def patched(self, path):
        self.socket = client

    sshsig.AgentClient.__init__ = patched
    try:
        with pytest.raises(SystemExit, match="produced no signature"):
            sshsig.sign_via_agent(
                server_path, public, b"nonce", "msks-console"
            )
    finally:
        sshsig.AgentClient.__init__ = original


def test_agent_stall_reads_as_death_not_a_traceback() -> None:
    """A peer that accepts the request and then stalls past the
    socket's timeout reads as None — the touch-to-sign window is
    waited out, not crashed through."""
    import socket
    import time

    client, server = socket.socketpair()
    stalled = sshsig.AgentClient.__new__(sshsig.AgentClient)
    stalled.socket = client
    stalled.socket.settimeout(0.2)
    try:
        assert stalled.exchange(b"x") is None
    finally:
        client.close()
        server.close()
    assert time.monotonic() > 0  # reached without raising


def test_agent_garbage_identity_body_lists_nothing() -> None:
    """An IDENTITIES_ANSWER whose body truncates mid-string reads
    as an empty list, per the docstring's contract."""
    import socket

    # A valid answer type byte, a count of one, then a blob length
    # that outruns the body.
    body = struct.pack("B", 12) + struct.pack(">I", 1) + struct.pack(">I", 999)
    client, server = socket.socketpair()
    _serve([_frame(body)], server)
    broken = sshsig.AgentClient.__new__(sshsig.AgentClient)
    broken.socket = client
    assert broken.identities() == []
    client.close()
