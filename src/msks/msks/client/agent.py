"""A transient ssh-agent serving one minted workspace identity.

``msks ssh`` stages the workspace's private key in memory and hands
ssh an agent socket (``-o IdentityAgent=...``). The OpenSSH client
closes every inherited descriptor at startup, so a key passed as an
fd (``-i /proc/self/fd/<n>``) never survives to authentication —
but ssh opens the agent socket itself, after that cleanup. This
module is that agent: it speaks the ssh-agent wire protocol
(draft-miller-ssh-agent) for exactly two operations — list the
identity, and sign with it — and refuses everything else.

The key material never becomes a file. The socket lives in a
mode-0700 temporary directory and is removed with it; the private
half stays a Python object in process memory.
"""

import base64
import os
import shutil
import socketserver
import struct
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

#: Protocol message types this agent answers (draft-miller-ssh-agent).
REQUEST_IDENTITIES = 11
IDENTITIES_ANSWER = 12
SIGN_REQUEST = 13
SIGN_RESPONSE = 14
FAILURE = 5

#: The sign-request flag bits that select an RSA digest (all other
#: flags are none this agent needs to honor: constrained signing and
#: extension-awareness are refused as FAILURE above).
RSA_SHA2_256 = 2
RSA_SHA2_512 = 4

#: One protocol frame is at most this long; a bigger length field is
#: a broken peer, and the connection closes instead of allocating.
MAX_FRAME = 1 << 20

#: The largest accepted message: identity listings are bounded by
#: the one key this agent serves, sign payloads by ssh's own
#: challenge sizes.
MAX_MESSAGE = 1 << 16

CURVE_NAMES = {ec.SECP256R1: "ecdsa-sha2-nistp256"}


def wire_string(data: bytes) -> bytes:
    """One ssh wire ``string``: length-prefixed bytes."""
    return struct.pack(">I", len(data)) + data


def frame(payload: bytes) -> bytes:
    """One agent protocol frame: length-prefixed payload."""
    return wire_string(payload)


class Reader:
    """Sequential reads over one message payload."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    def uint32(self) -> int:
        """The next uint32; raises ValueError past the payload's end."""
        if self.offset + 4 > len(self.payload):
            raise ValueError("uint32 past end")
        (value,) = struct.unpack_from(">I", self.payload, self.offset)
        self.offset += 4
        return value

    def string(self) -> bytes:
        """The next wire ``string``."""
        length = self.uint32()
        if self.offset + length > len(self.payload):
            raise ValueError("string past end")
        data = self.payload[self.offset : self.offset + length]
        self.offset += length
        return data


def load_private(pem: str):
    """The private key object from its OpenSSH-format PEM half."""
    return serialization.load_ssh_private_key(pem.encode(), password=None)


def public_parts(private) -> tuple[bytes, str]:
    """(wire blob, algorithm name) of the private key's public half.

    The blob is what the agent's identity listing carries — the
    base64-decoded body of the authorized_keys line, with the key
    type embedded as its first string.
    """
    line = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    algo, encoded = line.split()[:2]
    return base64.b64decode(encoded), algo


def mpint(value: int) -> bytes:
    """One ssh wire ``mpint``: two's complement, negative-able —
    every value this agent signs is positive, so a leading zero byte
    keeps the high bit clear."""
    data = value.to_bytes((value.bit_length() + 8) // 8, "big")
    return wire_string(data)


def signature(algo: str, sig: bytes) -> bytes:
    """The signature blob a sign response carries: the algorithm
    name and the raw signature, each a wire string."""
    return wire_string(algo.encode()) + wire_string(sig)


def sign(private, challenge: bytes, rsa_sha512: bool) -> bytes:
    """Sign ``challenge`` and return the algorithm-tagged blob."""
    if isinstance(private, ed25519.Ed25519PrivateKey):
        return signature("ssh-ed25519", private.sign(challenge))
    if isinstance(private, ec.EllipticCurvePrivateKey):
        return ecdsa_signature(private, challenge)
    return rsa_signature(private, challenge, rsa_sha512)


def ecdsa_signature(private, challenge: bytes) -> bytes:
    """The ECDSA signature blob: r and s as mpints under the curve's
    algorithm name."""
    raw = private.sign(challenge, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(raw)
    algo = CURVE_NAMES.get(type(private.curve))
    if algo is None:
        raise ValueError("unsupported ECDSA curve")
    return signature(algo, mpint(r) + mpint(s))


def rsa_signature(private, challenge: bytes, sha512: bool) -> bytes:
    """The RSA signature blob: PKCS#1 v1.5 under the digest ssh
    negotiated (the sign request's flag bits)."""
    digest = hashes.SHA512() if sha512 else hashes.SHA256()
    algo = "rsa-sha2-512" if sha512 else "rsa-sha2-256"
    sig = private.sign(challenge, padding.PKCS1v15(), digest)
    return signature(algo, sig)


class AgentServer(socketserver.ThreadingUnixStreamServer):
    """The agent: one identity, two answers, nothing else."""

    daemon_threads = True

    def __init__(self, socket_path: str, private, comment: str) -> None:
        self.private = private
        self.comment = comment
        self.blob, self.algo = public_parts(private)
        super().__init__(socket_path, AgentHandler)

    def dispatch(self, payload: bytes) -> bytes:
        """One request payload to its response payload."""
        try:
            return self.respond(payload)
        except ValueError, IndexError, TypeError:
            return struct.pack("B", FAILURE)

    def respond(self, payload: bytes) -> bytes:
        kind = payload[0]
        if kind == REQUEST_IDENTITIES:
            return self.identities()
        if kind == SIGN_REQUEST:
            return self.sign_response(payload)
        # A session-bind extension probe or anything unknown: ssh
        # treats a FAILURE answer as "the agent cannot" and moves on.
        return struct.pack("B", FAILURE)

    def identities(self) -> bytes:
        """The one identity this agent serves, with its comment."""
        body = wire_string(self.blob) + wire_string(self.comment.encode())
        return struct.pack(">BI", IDENTITIES_ANSWER, 1) + body

    def sign_response(self, payload: bytes) -> bytes:
        """Sign the challenge, but only for the served identity."""
        reader = Reader(payload)
        reader.offset = 1
        blob = reader.string()
        challenge = reader.string()
        flags = reader.uint32()
        if blob != self.blob:
            return struct.pack("B", FAILURE)
        sig = sign(self.private, challenge, bool(flags & RSA_SHA2_512))
        return struct.pack("B", SIGN_RESPONSE) + wire_string(sig)


class AgentHandler(socketserver.BaseRequestHandler):
    """One agent connection: read frames, answer frames, until EOF."""

    def handle(self) -> None:
        while True:
            payload = self.receive()
            if payload is None:
                return
            self.request.sendall(frame(self.server.dispatch(payload)))

    def receive(self) -> bytes | None:
        """One request payload, or None when the connection ends —
        EOF, a short read, or a length field past the ceiling."""
        header = self.read_exact(4)
        if header is None:
            return None
        (length,) = struct.unpack(">I", header)
        if not 1 <= length <= MAX_MESSAGE:
            return None
        return self.read_exact(length)

    def read_exact(self, count: int) -> bytes | None:
        """Exactly ``count`` bytes, or None on EOF/short read."""
        data = b""
        while len(data) < count:
            chunk = self.request.recv(count - len(data))
            if not chunk:
                return None
            data += chunk
        return data


@contextmanager
def serve(private, comment: str):
    """The served agent: a ``.socket_path`` holder for the duration.

    The server runs on its own thread (ssh is a blocking child), in a
    mode-0700 temporary directory that goes away with the socket.
    """
    directory = tempfile.mkdtemp(prefix="msks-agent-")
    socket_path = str(Path(directory) / "agent.sock")
    server = AgentServer(socket_path, private, comment)
    os.chmod(socket_path, 0o600)
    # The public half as a file, so ssh can NAME the identity (-i
    # identity.pub) under IdentitiesOnly: ssh matches the agent's
    # listed key against this pubkey and signs through the socket —
    # the private half stays agent-resident. Public material is all
    # this file holds.
    server.identity_path = str(Path(directory) / "identity.pub")
    Path(server.identity_path).write_text(
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
        + "\n"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(directory, ignore_errors=True)


def socket_mode(path: str) -> int:
    """The socket's permission bits (the 0600 forced at bind)."""
    return os.stat(path).st_mode & 0o777
