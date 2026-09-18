"""SSHSIG signing for the console challenge (#123), and the ssh-agent
client that keeps private material out of msks.

The guest helper's challenge demands an SSHSIG signature
(PROTOCOL.sshsig) over a fresh nonce in the ``msks-console``
namespace. Two ways to produce one:

- :func:`sign_payload` — msks holds the private half (a daemon-mint
  escrow fetched over the API, or the client data root's file) and
  signs in-process. This is the create-default path.
- :func:`sign_via_agent` — the key lives in an ssh-agent (the
  operator's own ``SSH_AUTH_SOCK``, hardware-backed keys included)
  and msks only speaks the agent protocol: list identities, match
  the workspace's public half, request the signature. The private
  half never crosses msks's boundary, and a touch-to-sign key makes
  each console session a physical user-presence check.

Both produce the same wire form: base64 of the SSHSIG file body,
which the guest re-armors and hands to ``ssh-keygen -Y verify``.
"""

import base64
import hashlib
import os
import socket
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)

from .agent import Reader, load_private, public_parts, wire_string

#: The SSHSIG payload hash: PROTOCOL.sshsig names sha512, and every
#: signature rides the SHA-512 family to match it.
HASH_ALGORITHM = "sha512"

#: The SSHSIG format version (PROTOCOL.sshsig).
SSHSIG_VERSION = 1

#: The console challenge's namespace: a console signature verifies
#: nowhere else (an ssh session signature, a git signature, a
#: signature for another workspace's challenge — none pass here).
#: The guest helper pins the same name.
NAMESPACE = "msks-console"

#: Agent protocol constants (draft-miller-ssh-agent), the client side.
REQUEST_IDENTITIES = 11
IDENTITIES_ANSWER = 12
SIGN_REQUEST = 13
SIGN_RESPONSE = 14
FAILURE = 5

#: The RSA signing flags: SHA-512 under rsa-sha2-512.
RSA_SHA2_512 = 4

#: One agent message is bounded; a bigger length field is a broken
#: peer and the connection closes.
MAX_MESSAGE = 1 << 16

#: ECDSA curve → wire algorithm name (the SSHSIG sign side uses the
#: plain key type names, not the -cert forms).
CURVE_NAMES = {
    ec.SECP256R1: "ecdsa-sha2-nistp256",
    ec.SECP384R1: "ecdsa-sha2-nistp384",
    ec.SECP521R1: "ecdsa-sha2-nistp521",
}

#: ECDSA curve → the digest width ssh-keygen signs with (each curve
#: its own, matching ssh_ecdsa_sign: p256 SHA-256, p384 SHA-384, p521
#: SHA-512).
CURVE_HASHES = {
    ec.SECP256R1: hashes.SHA256,
    ec.SECP384R1: hashes.SHA384,
    ec.SECP521R1: hashes.SHA512,
}


def ecdsa_hash(curve) -> hashes.HashAlgorithm:
    """The digest an ECDSA signature over ``curve`` rides."""
    maker = CURVE_HASHES.get(type(curve))
    if maker is None:
        raise ValueError("unsupported ECDSA curve")
    return maker()


def mpint(value: int) -> bytes:
    """One ssh wire ``mpint``: two's complement, high bit padded."""
    data = value.to_bytes((value.bit_length() + 8) // 8, "big")
    return wire_string(data)


def sshsig_blob(payload: bytes, namespace: str) -> bytes:
    """The bytes an SSHSIG signature covers (PROTOCOL.sshsig): the
    raw magic, the namespace, the reserved field, the hash
    algorithm, and the payload's digest — every field after the
    magic a wire string. ssh-keygen builds the identical blob when
    it verifies."""
    digest = hashlib.sha512(payload).digest()
    return (
        b"SSHSIG"
        + wire_string(namespace.encode())
        + wire_string(b"")
        + wire_string(HASH_ALGORITHM.encode())
        + wire_string(digest)
    )


def sshsig_body(
    public_blob: bytes, signature_wire: bytes, namespace: str
) -> bytes:
    """The SSHSIG file body: the raw magic, the format version, the
    signer's public key blob, the namespace, the reserved field, the
    hash algorithm, and the signature. Armored, this is what a
    verifier reads."""
    return (
        b"SSHSIG"
        + struct.pack(">I", SSHSIG_VERSION)
        + wire_string(public_blob)
        + wire_string(namespace.encode())
        + wire_string(b"")
        + wire_string(HASH_ALGORITHM.encode())
        + wire_string(signature_wire)
    )


def sign_payload(private_pem: str, payload: bytes, namespace: str) -> str:
    """Base64 SSHSIG body of ``payload``, signed by the msks-held
    private half — the create-default console path."""
    private = load_private(private_pem)
    public_blob, _algorithm = public_parts(private)
    return sign_key(private, public_blob, payload, namespace)


def sign_key(
    private, public_blob: bytes, payload: bytes, namespace: str
) -> str:
    """The SSHSIG body with every signer: the blob to cover, the
    signature over it, and the body that carries both."""
    blob = sshsig_blob(payload, namespace)
    algorithm, raw = sign_blob(private, blob)
    signature_wire = wire_string(algorithm.encode()) + wire_string(raw)
    return base64.b64encode(
        sshsig_body(public_blob, signature_wire, namespace)
    ).decode()


def sign_blob(private, blob: bytes) -> tuple[str, bytes]:
    """``(algorithm, raw signature)`` over the blob: ed25519 pure,
    ECDSA over its curve's own hash (r and s as mpints), RSA
    PKCS#1 v1.5 SHA-512 as rsa-sha2-512."""
    if isinstance(private, ed25519.Ed25519PrivateKey):
        return "ssh-ed25519", private.sign(blob)
    if isinstance(private, ec.EllipticCurvePrivateKey):
        # ecdsa_hash already refused a curve outside the wire names,
        # so the name table shares its verdict.
        raw = private.sign(blob, ec.ECDSA(ecdsa_hash(private.curve)))
        r, s = decode_dss_signature(raw)
        return CURVE_NAMES[type(private.curve)], mpint(r) + mpint(s)
    sig = private.sign(blob, padding.PKCS1v15(), hashes.SHA512())
    return "rsa-sha2-512", sig


class AgentClient:
    """The client half of the agent protocol: enough to list
    identities and request one signature over a unix socket."""

    def __init__(self, path: str) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(30.0)
        self.socket.connect(path)

    def close(self) -> None:
        self.socket.close()

    def exchange(self, payload: bytes) -> bytes | None:
        """One framed request, one framed answer payload; None on a
        failure answer, a broken frame, or a peer that died between
        connect and send."""
        try:
            self.socket.sendall(wire_string(payload))
        except OSError:
            return None
        header = self.read_exact(4)
        if header is None:
            return None
        (length,) = struct.unpack(">I", header)
        if not 1 <= length <= MAX_MESSAGE:
            return None
        return self.read_exact(length)

    def read_exact(self, count: int) -> bytes | None:
        data = b""
        while len(data) < count:
            try:
                chunk = self.socket.recv(count - len(data))
            except OSError:
                # Including a timeout (socket.timeout subclasses
                # OSError): a stalled peer reads as death.
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def identities(self) -> list[tuple[bytes, str]]:
        """The agent's ``(key blob, comment)`` list; empty when the
        agent refuses or speaks garbage."""
        answer = self.exchange(struct.pack("B", REQUEST_IDENTITIES))
        if answer is None or not answer or answer[0] != IDENTITIES_ANSWER:
            return []
        reader = Reader(answer)
        reader.offset = 1
        try:
            return self.parse_identities(reader, reader.uint32())
        except ValueError:
            # A truncated or malformed body: an agent that speaks
            # garbage lists no keys.
            return []

    def parse_identities(
        self, reader: Reader, count: int
    ) -> list[tuple[bytes, str]]:
        """The listed pairs from an IDENTITIES_ANSWER body."""
        listed = []
        for _ in range(count):
            listed.append(
                (reader.string(), reader.string().decode(errors="replace"))
            )
        return listed

    def sign(self, key_blob: bytes, data: bytes, flags: int) -> bytes | None:
        """One signature wire blob (algorithm + raw, ssh wire form),
        or None when the agent refuses."""
        payload = (
            struct.pack("B", SIGN_REQUEST)
            + wire_string(key_blob)
            + wire_string(data)
            + struct.pack(">I", flags)
        )
        answer = self.exchange(payload)
        if answer is None or not answer or answer[0] != SIGN_RESPONSE:
            return None
        reader = Reader(answer)
        reader.offset = 1
        return reader.string()


def sign_via_agent(
    socket_path: str, public_line: str, payload: bytes, namespace: str
) -> str:
    """Base64 SSHSIG body through the agent at ``socket_path``.

    The workspace's public line names the identity: the agent's
    listed blob matching it is the one that signs. The private half
    stays inside the agent — msks sees a signature, nothing else.
    """
    want = base64.b64decode(public_line.split()[1])
    client = AgentClient(socket_path)
    try:
        for blob, _comment in client.identities():
            if blob != want:
                continue
            flags = RSA_SHA2_512 if public_line.startswith("ssh-rsa ") else 0
            signature = client.sign(
                blob, sshsig_blob(payload, namespace), flags
            )
            if signature is None:
                break
            return base64.b64encode(
                sshsig_body(blob, signature, namespace)
            ).decode()
        raise SystemExit(
            "msks console: the agent at "
            f"{socket_path} produced no signature for the workspace's "
            "key — it is not loaded (ssh-add it), or its confirmation "
            "window passed without an answer (a hardware key's touch). "
            "Connect from the client that holds the private half."
        )
    finally:
        client.close()


def environment_agent() -> str | None:
    """The agent socket the operator's environment names, when it
    names one and it exists."""
    path = os.environ.get("SSH_AUTH_SOCK", "")
    if path and os.path.exists(path):
        return path
    return None
