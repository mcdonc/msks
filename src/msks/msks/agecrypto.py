"""The age crypto leaf (#513, #504): ssh-recipient encryption, in house.

Placeholder values (#198) are moving from the SecretSpec agefile —
ciphertext a daemon-held identity decrypts — to per-recipient
ciphertext encrypted to the workspace identity's ssh public half
(#504): the daemon stores ciphertext only, the client decrypts
with the private half it already owns (#486/#497 — the daemon
never holds it), and plaintext exists in the daemon only inside
the unlock window. This module is the whole construction, shared
by daemon and client the way ``msks.identity`` is:

- the age v1 file format — textual header, header MAC, STREAM
  payload (age-encryption.org/v1);
- the two ssh recipient types the reference tool defines:
  ``ssh-ed25519`` (the tweaked X25519 construction, its HKDF
  bound to the ssh wire-format public key) and ``ssh-rsa``
  (RSA-OAEP with the age label);
- key loading — OpenSSH-format private key files, the passphrase
  supplied through a callback the caller owns, because the client
  owns the prompt (#515) and nothing else ever prompts.

The construction interoperates with the reference implementation
(filippo.io/age, as of v1.3.2): the pinned fixtures under
``src/msks/tests/agefixtures/`` prove we decrypt its files, and
the live round-trip test proves it decrypts ours wherever the
``age`` binary exists. The bech32 encoding and the age identity
mint moved here from ``msks.secretstore`` (#513): the leaf owns
the age primitives, and #516 retires the rest of that module's
vault.
"""

import base64
import binascii
import hashlib
import hmac
import secrets as pysecrets
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import (
    ed25519,
    rsa,
    x25519,
)
from cryptography.hazmat.primitives.asymmetric.padding import (
    MGF1,
    OAEP,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: The version line every age v1 header opens with.
V1_HEADER = "age-encryption.org/v1"

#: The HKDF info strings: the ssh recipient labels and the two
#: file-format derivations (the header MAC key and the payload
#: key). These exact bytes are the interop surface — a change
#: parts compatibility with every age file ever written.
ED25519_LABEL = b"age-encryption.org/v1/ssh-ed25519"
RSA_LABEL = b"age-encryption.org/v1/ssh-rsa"
HEADER_INFO = b"header"
PAYLOAD_INFO = b"payload"

#: The file key age wraps in stanzas and the STREAM chunk size —
#: both fixed by the format.
FILE_KEY_BYTES = 16
CHUNK_BYTES = 64 * 1024

#: The reference parser's caps, adopted as-is: a header over
#: 2 MiB, more than 1024 stanzas, or a stanza over 128 arguments
#: is malformed — and unchecked, each is a memory lever a hostile
#: blob could pull in the client (#515).
MAX_HEADER_BYTES = 2 * 1024 * 1024
MAX_STANZAS = 1024
MAX_STANZA_ARGS = 128

#: The ssh key types this module can encrypt to (#513's capability
#: predicate): the two the reference tool defines. create accepts
#: any shape-valid type (#132); a workspace keyed otherwise cannot
#: hold secrets, and #514's mint refuses with this set as the
#: reason.
AGE_CAPABLE_TYPES = frozenset({"ssh-ed25519", "ssh-rsa"})


class AgeCryptoError(Exception):
    """A malformed key, ciphertext, or header — the named error for
    everything this module refuses to parse or verify. Distinct
    from :class:`NotForThisKey`, which is a decrypt miss, not a
    defect."""


class NotForThisKey(Exception):
    """A stanza this identity does not open: the wrong fingerprint
    or a body that fails under this key. The decrypt loop catches
    it and moves to the next stanza — possession of the key is
    proven by a successful unwrap, never claimed."""


# --- bech32 and the age identity mint (moved from secretstore) ----


#: The bech32 character set (BIP-173): age identities and
#: recipients encode with it.
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_polymod(values: list[int]) -> int:
    """The bech32 checksum accumulator over *values* (5-bit)."""
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def bech32_checksum(hrp: str, data: list[int]) -> list[int]:
    """The six 5-bit checksum words for *data* under *hrp* (the
    bech32 const-1 variant age uses)."""
    expanded = [ord(c) >> 5 for c in hrp] + [0]
    expanded += [ord(c) & 31 for c in hrp]
    polymod = bech32_polymod(expanded + data + [0] * 6) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def convertbits(data: bytes, frombits: int, tobits: int) -> list[int]:
    """Re-group *data*'s bits: 8-bit bytes into 5-bit words
    (zero-padded at the tail, as age's fixed-size keys need)."""
    acc = 0
    bits = 0
    words = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            words.append((acc >> bits) & maxv)
    if bits:
        words.append((acc << (tobits - bits)) & maxv)
    return words


def encode_bech32(hrp: str, data: bytes) -> str:
    """*data* as a bech32 string under the lowercase *hrp*."""
    words = convertbits(data, 8, 5)
    payload = words + bech32_checksum(hrp, words)
    return hrp + "1" + "".join(BECH32_CHARSET[w] for w in payload)


def mint_age_identity() -> str:
    """A fresh age-keygen-shaped identity file body: the X25519
    private key bech32-encoded as ``AGE-SECRET-KEY-1…`` with its
    recipient on a comment line (the shape ``age-keygen`` writes,
    so the file reads as a native age identity everywhere — the
    recipient line lets an operator re-key or back up without
    decrypting anything).
    """
    private = x25519.X25519PrivateKey.generate()
    scalar = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    recipient = encode_bech32(
        "age",
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
    )
    identity = encode_bech32("age-secret-key-", scalar).upper()
    return f"# public key: {recipient}\n{identity}\n"


# --- base64 and ssh wire helpers ----------------------------------


def b64e(data: bytes) -> str:
    """The header's base64: standard alphabet, no padding (the
    age header never carries ``=``)."""
    return base64.b64encode(data).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    """Strict decode of the header's unpadded base64: padding is
    restored by length, and anything outside the alphabet is a
    named error (the reference rejects non-canonical encodings,
    and so does a careful reader)."""
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AgeCryptoError(f"invalid base64: {exc}") from None


def wire_string(data: bytes) -> bytes:
    """One ssh wire-format string: 4-byte big-endian length +
    body."""
    return len(data).to_bytes(4, "big") + data


def wire_strings(blob: bytes) -> list[bytes]:
    """Every length-prefixed string in an ssh wire-format blob,
    whole-blob only (trailing bytes are an error, the same
    strictness ssh's own parser applies)."""
    out: list[bytes] = []
    pos = 0
    while pos < len(blob):
        if pos + 4 > len(blob):
            raise AgeCryptoError("truncated ssh wire blob")
        size = int.from_bytes(blob[pos : pos + 4], "big")
        pos += 4
        if size > len(blob) - pos:
            raise AgeCryptoError("truncated ssh wire blob")
        out.append(blob[pos : pos + size])
        pos += size
    return out


def mpint(value: int) -> bytes:
    """One ssh wire-format mpint: minimal big-endian bytes, a
    leading zero when the high bit would read as a sign."""
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return wire_string(raw)


def public_key_parts(line: str) -> tuple[str, bytes]:
    """An authorized_keys line split into ``(algo, wire_blob)``:
    the blob is the decoded base64 body, whose first embedded
    string must agree with the algorithm the line names (the same
    shape check ``msks.identity`` applies at create)."""
    fields = line.split()
    if len(fields) < 2:
        raise AgeCryptoError("a public key line needs an algorithm and body")
    algo, encoded = fields[0], fields[1]
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AgeCryptoError(f"public key body is not base64: {exc}") from None
    strings = wire_strings(blob)
    # An empty body cannot reach here (the line's split drops it),
    # so the first string is the embedded algorithm name.
    if strings[0] != algo.encode():
        raise AgeCryptoError(
            "public key algorithm does not match its key body"
        )
    return algo, blob


def fingerprint(blob: bytes) -> str:
    """The recipient hint every ssh stanza carries: four bytes of
    SHA-256 over the wire-format public key, base64. It names the
    key a stanza was wrapped to — a hint only, so a collision
    costs one failed unwrap, never a wrong decrypt."""
    return b64e(hashlib.sha256(blob).digest()[:4])


def secret_key_hash(line: str) -> str:
    """The staleness token a blob row records at mint (#513):
    SHA-256 hex of the workspace's ssh public key line. A
    workspace whose line changes mints a different hash, so the
    blob written against the old line reads back stale. Defined
    with the key vocabulary so the blob-writing leaf (#514) and
    the staleness reader (the model) share one spelling."""
    return hashlib.sha256(line.encode()).hexdigest()


def age_capable(line: str) -> bool:
    """Whether *line* names a key type this module can encrypt to
    — the #514 mint's refusal predicate. Shape-invalid lines are
    simply incapable (create's own checks, not this leaf's, own
    that error)."""
    fields = line.split()
    return len(fields) >= 2 and fields[0] in AGE_CAPABLE_TYPES


# --- curve25519 field arithmetic ----------------------------------

#: Curve25519's prime and Ed25519's d (the two constants the
#: Edwards→Montgomery conversion needs).
P25519 = 2**255 - 19
D25519 = -121665 * pow(121666, P25519 - 2, P25519) % P25519


def montgomery_u(point: bytes) -> bytes:
    """An Ed25519 public point's X25519 (Montgomery) u-coordinate:
    ``u = (1 + y) / (1 - y)`` over the curve's field, computed the
    way the reference's edwards25519 does — canonical y only, and
    the point must actually be on the curve (an off-curve y is an
    attacker-chosen input, not a key)."""
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= P25519:
        raise AgeCryptoError("non-canonical Ed25519 point")
    yy = y * y % P25519
    # d is a non-square mod p (the curve's completeness), and -1
    # is a square (p = 1 mod 4), so -1/d — the only y^2 that
    # would zero the denominator — has no root: the inverse below
    # always exists.
    denom = (D25519 * yy + 1) % P25519
    x2 = (yy - 1) * pow(denom, P25519 - 2, P25519) % P25519
    if pow(x2, (P25519 - 1) // 2, P25519) > 1:
        raise AgeCryptoError("the point is not on the curve")
    if (1 - y) % P25519 == 0:
        return b"\x00" * 32
    return (
        (1 + y) * pow((1 - y) % P25519, P25519 - 2, P25519) % P25519
    ).to_bytes(32, "little")


def ed25519_scalar(seed: bytes) -> bytes:
    """An Ed25519 private seed's X25519 scalar: the first 32 bytes
    of SHA-512 of the seed, clamped inside the X25519 operation
    itself (the standard conversion the reference uses — the
    scalar is fed unclamped and the multiplication clamps)."""
    return hashlib.sha512(seed).digest()[:32]


def x25519_shared(scalar: bytes, point: bytes) -> bytes:
    """One X25519 scalar multiplication. A low-order input point —
    whose result would be all zeros — is a named error: the
    underlying library rejects it on computation (the reference's
    own rule), and a handshake that produced it carries no
    secrecy."""
    try:
        return x25519.X25519PrivateKey.from_private_bytes(scalar).exchange(
            x25519.X25519PublicKey.from_public_bytes(point)
        )
    except ValueError:
        raise AgeCryptoError(
            "degenerate X25519 result (low-order point)"
        ) from None


# --- the recipient half -------------------------------------------


@dataclass(frozen=True)
class Recipient:
    """One ssh public key prepared to wrap a file key: the wire
    blob (the fingerprint's input), and the wrapped key's target —
    the Montgomery u-coordinate for ed25519, the RSA public key
    for rsa."""

    kind: str
    blob: bytes
    fp: str
    montgomery: bytes | None = None
    rsa_pub: rsa.RSAPublicKey | None = None


def recipient(line: str) -> Recipient:
    """An authorized_keys line as a wrap-ready recipient. Only the
    two reference-defined types parse; any other type is a named
    error — ``age_capable`` is the silent question this answers
    loudly."""
    algo, blob = public_key_parts(line)
    strings = wire_strings(blob)
    if algo == "ssh-ed25519":
        return ed25519_recipient(blob, strings)
    if algo == "ssh-rsa":
        return rsa_recipient(blob, strings)
    raise AgeCryptoError(f"age defines no recipient for a {algo} key")


def ed25519_recipient(blob: bytes, strings: list[bytes]) -> Recipient:
    """The ed25519 recipient: the wire blob's 32-byte point to its
    Montgomery u-coordinate."""
    if len(strings) != 2 or len(strings[1]) != 32:
        raise AgeCryptoError("a malformed ssh-ed25519 public key")
    return Recipient(
        kind="ssh-ed25519",
        blob=blob,
        fp=fingerprint(blob),
        montgomery=montgomery_u(strings[1]),
    )


def rsa_recipient(blob: bytes, strings: list[bytes]) -> Recipient:
    """The RSA recipient, 2048 bits minimum — the reference
    refuses to wrap under it, and so does the load."""
    if len(strings) != 3:
        raise AgeCryptoError("a malformed ssh-rsa public key")
    numbers = rsa.RSAPublicNumbers(
        int.from_bytes(strings[1], "big"),
        int.from_bytes(strings[2], "big"),
    )
    if numbers.n.bit_length() < 2048:
        raise AgeCryptoError(
            f"an RSA key under 2048 bits ({numbers.n.bit_length()})"
            " cannot wrap an age file key"
        )
    return Recipient(
        kind="ssh-rsa",
        blob=blob,
        fp=fingerprint(blob),
        rsa_pub=numbers.public_key(),
    )


def wrap_key(target: Recipient, file_key: bytes) -> Stanza:
    """The stanza that wraps *file_key* for *target*."""
    if target.kind == "ssh-ed25519":
        return wrap_ed25519(target, file_key)
    return wrap_rsa(target, file_key)


def wrap_ed25519(target: Recipient, file_key: bytes) -> Stanza:
    """The tweaked X25519 construction (reference v1.3.x): an
    ephemeral X25519 exchange with the recipient's Montgomery key,
    then a second multiplication by an HKDF tweak bound to the ssh
    wire blob — the tweak ties the wrap to the ssh key itself, not
    just its X25519 translation, so two ssh keys sharing a curve
    point stay distinct recipients."""
    ephemeral = pysecrets.token_bytes(32)
    epk = x25519_shared(ephemeral, (9).to_bytes(32, "little"))
    shared = x25519_shared(ephemeral, target.montgomery)
    tweak = hkdf_key(b"", target.blob, ED25519_LABEL)
    shared = x25519_shared(tweak, shared)
    key = hkdf_key(shared, epk + target.montgomery, ED25519_LABEL)
    return Stanza(
        kind="ssh-ed25519",
        args=[target.fp, b64e(epk)],
        body=aead_seal(key, file_key),
    )


def wrap_rsa(target: Recipient, file_key: bytes) -> Stanza:
    """The RSA recipient: OAEP with SHA-256 and the age label —
    the one construction the reference pinned, so the label bytes
    are part of the format."""
    return Stanza(
        kind="ssh-rsa",
        args=[target.fp],
        body=target.rsa_pub.encrypt(
            file_key,
            OAEP(MGF1(SHA256()), SHA256(), label=RSA_LABEL),
        ),
    )


# --- the identity half ---------------------------------------------


@dataclass
class SshIdentity:
    """One loaded ssh private key, ready to unwrap stanzas wrapped
    to its public half."""

    kind: str
    blob: bytes
    fp: str
    montgomery: bytes | None = None
    x25519_key: x25519.X25519PrivateKey | None = None
    rsa_key: rsa.RSAPrivateKey | None = None

    def unwrap(self, stanza: Stanza) -> bytes:
        """The file key under one stanza, or :class:`NotForThisKey`
        when this key does not open it (the decrypt loop's unit)."""
        if stanza.kind != self.kind:
            raise NotForThisKey(f"a {stanza.kind} stanza")
        if stanza.kind == "ssh-ed25519":
            return self.unwrap_ed25519(stanza)
        return self.unwrap_rsa(stanza)

    def unwrap_ed25519(self, stanza: Stanza) -> bytes:
        """The mirror of :func:`wrap_ed25519`: the fingerprint
        gates (a mismatch is a miss, not a defect), the shared
        secret takes the same tweak, and a body that fails under
        the derived key is a miss — the hint may collide."""
        if len(stanza.args) != 2:
            raise AgeCryptoError("an ssh-ed25519 stanza needs two arguments")
        if stanza.args[0] != self.fp:
            raise NotForThisKey("another key's fingerprint")
        try:
            epk = b64d(stanza.args[1])
        except AgeCryptoError:
            raise AgeCryptoError(
                "an ssh-ed25519 stanza with a bad share"
            ) from None
        if len(epk) != 32:
            raise AgeCryptoError("an ssh-ed25519 stanza with a bad share")
        shared = x25519_shared(self.x25519_scalar(), epk)
        tweak = hkdf_key(b"", self.blob, ED25519_LABEL)
        shared = x25519_shared(tweak, shared)
        key = hkdf_key(shared, epk + self.montgomery, ED25519_LABEL)
        return aead_open(key, stanza.body)

    def x25519_scalar(self) -> bytes:
        """The identity's X25519 scalar — seed → SHA-512 → 32
        bytes (stored as the key object; derived once at load)."""
        return self.x25519_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    def unwrap_rsa(self, stanza: Stanza) -> bytes:
        """The mirror of :func:`wrap_rsa`: the fingerprint gate,
        then OAEP — a body that fails is a miss."""
        if len(stanza.args) != 1:
            raise AgeCryptoError("an ssh-rsa stanza needs one argument")
        if stanza.args[0] != self.fp:
            raise NotForThisKey("another key's fingerprint")
        try:
            return self.rsa_key.decrypt(
                stanza.body,
                OAEP(MGF1(SHA256()), SHA256(), label=RSA_LABEL),
            )
        except ValueError:
            raise NotForThisKey("a body this key does not open") from None


def load_any_key(key_pem: bytes, password: bytes | None):
    """An OpenSSH-container or classic-PEM private key, whatever
    the file is — ssh's own container first (every modern
    ``ssh-keygen`` output), the PEM forms (an old RSA key) second.
    A ``TypeError`` bubbles up: it is the encrypted-key signal the
    prompt flow below answers."""
    try:
        return serialization.load_ssh_private_key(key_pem, password=password)
    except ValueError:
        return serialization.load_pem_private_key(key_pem, password=password)


def load_identity(
    key_pem: bytes, prompt: Callable[[], str | bytes]
) -> SshIdentity:
    """An ssh private key file as an unwrap-ready identity.

    The passphrase never lives here: an encrypted key answers a
    ``TypeError`` and the callback owns everything user-facing —
    the client prompts (#515), a test answers a constant. A wrong
    passphrase is a named error (cryptography's own is a bare
    ``ValueError`` naming nothing). ed25519 and RSA of at least
    2048 bits load; every other type is named and refused.
    """
    try:
        key = load_any_key(key_pem, None)
    except TypeError:
        key = prompted_key(key_pem, prompt)
    except ValueError:
        raise AgeCryptoError("not a private key file") from None
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return ed25519_identity(key)
    if isinstance(key, rsa.RSAPrivateKey):
        return rsa_identity(key)
    raise AgeCryptoError(
        f"age defines no recipient for a {key.__class__.__name__} key"
    )


def prompted_key(key_pem: bytes, prompt: Callable[[], str | bytes]):
    """The encrypted-key path: the callback's answer (str or
    bytes) as the passphrase, a wrong one a named error."""
    answer = prompt()
    password = answer.encode() if isinstance(answer, str) else answer
    try:
        return load_any_key(key_pem, password)
    except TypeError, ValueError:
        raise AgeCryptoError("the private key's passphrase is wrong") from None


def ed25519_identity(key: ed25519.Ed25519PrivateKey) -> SshIdentity:
    """The identity half of the ed25519 recipient: the wire blob
    rebuilt from the key (fingerprint input), the X25519 scalar
    (seed → SHA-512), and its Montgomery public half — the
    reference derives all three the same way."""
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    point = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    scalar = ed25519_scalar(seed)
    x25519_key = x25519.X25519PrivateKey.from_private_bytes(scalar)
    blob = wire_string(b"ssh-ed25519") + wire_string(point)
    return SshIdentity(
        kind="ssh-ed25519",
        blob=blob,
        fp=fingerprint(blob),
        montgomery=x25519_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ),
        x25519_key=x25519_key,
    )


def rsa_identity(key: rsa.RSAPrivateKey) -> SshIdentity:
    """The identity half of the RSA recipient, 2048 bits minimum
    (the wrap refuses less; so does the load)."""
    if key.key_size < 2048:
        raise AgeCryptoError(
            f"an RSA key under 2048 bits ({key.key_size}) cannot wrap an"
            " age file key"
        )
    numbers = key.public_key().public_numbers()
    blob = wire_string(b"ssh-rsa") + mpint(numbers.e) + mpint(numbers.n)
    return SshIdentity(
        kind="ssh-rsa", blob=blob, fp=fingerprint(blob), rsa_key=key
    )


# --- the file format ----------------------------------------------


@dataclass
class Stanza:
    """One header stanza: the type line's arguments and the base64
    body (decoded)."""

    kind: str
    args: list[str]
    body: bytes


def hkdf_key(ikm: bytes, salt: bytes | None, info: bytes) -> bytes:
    """32 bytes of HKDF-SHA-256. A ``None`` salt is the format's
    "empty salt" — the hash-length zeros, not the empty string —
    which is what the header-MAC derivation means."""
    return HKDF(algorithm=SHA256(), length=32, salt=salt, info=info).derive(
        ikm
    )


def aead_seal(key: bytes, plaintext: bytes) -> bytes:
    """The stanza wrap: ChaCha20-Poly1305 under the zero nonce, no
    AAD — the fixed construction every stanza body uses."""
    return ChaCha20Poly1305(key).encrypt(b"\x00" * 12, plaintext, b"")


def aead_open(key: bytes, ciphertext: bytes) -> bytes:
    """The stanza unwrap; a failure is the caller's miss to
    name."""
    try:
        return ChaCha20Poly1305(key).decrypt(b"\x00" * 12, ciphertext, b"")
    except Exception:
        raise NotForThisKey("a body this key does not open") from None


def stanza_lines(stanza: Stanza) -> list[str]:
    """The stanza's header lines: the argument line, then the body
    base64 wrapped at 64 columns — with a final short line the
    format requires even when it is empty (the reference writer
    always ends a stanza with a line under 64 columns, so a body
    whose base64 is an exact multiple folds to one extra empty
    line)."""
    lines = ["-> " + " ".join([stanza.kind, *stanza.args])]
    encoded = b64e(stanza.body)
    lines += [encoded[i : i + 64] for i in range(0, len(encoded), 64)]
    if len(encoded) % 64 == 0:
        lines.append("")
    return lines


def build_header(file_key: bytes, stanzas: list[Stanza]) -> bytes:
    """The whole textual header: version line, stanzas, and the
    MAC line. The MAC covers everything through the ``---`` mark
    (the space after it is excluded) under an HKDF key derived
    from the file key — so the header is tamper-evident before any
    recipient unwraps."""
    lines = [V1_HEADER]
    for stanza in stanzas:
        lines += stanza_lines(stanza)
    partial = "\n".join(lines) + "\n---"
    mac = hmac.new(
        hkdf_key(file_key, None, HEADER_INFO),
        partial.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return (partial + " " + b64e(mac) + "\n").encode("ascii")


def parse_header(data: bytes) -> tuple[list[Stanza], bytes, bytes, bytes]:
    """``(stanzas, mac, mac_input, payload)`` from an age file's
    bytes: the parsed stanzas, the MAC the last line claims, the
    bytes the MAC must cover, and everything after the header —
    the payload, whose first 16 bytes are its nonce. Sequential
    and strict: the version line is exact, stanza bodies are
    unpadded base64, and a stanza of an unknown type parses and is
    skipped (the format lets any stanza ride; identities ignore
    what they do not open)."""
    stanzas: list[Stanza] = []
    mac: bytes | None = None
    mac_input = b""
    payload_at = len(data)
    pos = 0
    current: Stanza | None = None
    first = True
    while pos < len(data):
        line, line_start, pos = header_line(data, pos)
        if line.startswith(b"---"):
            # No stanza yet covers the version line too — the
            # version line itself never reaches this arm (a wrong
            # one already raised inside header_step).
            if not stanzas:
                raise AgeCryptoError("the age header has no stanza")
            mac, mac_input = mac_line(line, data, line_start)
            payload_at = pos
            break
        first, current = header_step(line, first, current, stanzas)
    if mac is None:
        raise AgeCryptoError("the age header has no MAC line")
    return stanzas, mac, mac_input, data[payload_at:]


def header_step(
    line: bytes, first: bool, current: Stanza | None, stanzas: list[Stanza]
) -> tuple[bool, Stanza | None]:
    """One non-MAC header line onto the parse state: the version
    line (exact), a stanza's argument line, or a body line."""
    if first:
        if line != V1_HEADER.encode():
            raise AgeCryptoError("not an age v1 file")
        return False, current
    if line.startswith(b"-> "):
        return first, open_stanza(line, stanzas)
    accept_body_line(line, current)
    if len(line) < 64:
        # The short final line closes the stanza.
        return first, None
    return first, current


def header_line(data: bytes, pos: int) -> tuple[bytes, int, int]:
    """One header line and where the next begins; a header with no
    terminating newline ends nowhere the format accepts, and a
    header past the reference's size cap is malformed before any
    more of it parses."""
    nl = data.find(b"\n", pos)
    if nl < 0:
        raise AgeCryptoError("the age header has no MAC line")
    if nl + 1 > MAX_HEADER_BYTES:
        raise AgeCryptoError("the header exceeds the format's limit")
    return data[pos:nl], pos, nl + 1


def mac_line(line: bytes, data: bytes, line_start: int) -> tuple[bytes, bytes]:
    """The final line's claimed MAC and the bytes it must cover —
    everything through the ``---`` mark, the space after it
    excluded. Exactly one space and exactly 43 base64 characters,
    the shape 32 unpadded bytes take; anything else is malformed."""
    if len(line) != 4 + 43 or line[3:4] != b" ":
        raise AgeCryptoError("the MAC line is malformed")
    try:
        mac = b64d(line[4:].decode("ascii"))
    except AgeCryptoError, UnicodeDecodeError:
        raise AgeCryptoError("the MAC line is malformed") from None
    # 43 unpadded base64 characters decode to exactly 32 bytes, so
    # the length check above is the whole shape story.
    return mac, data[: line_start + 3]


def open_stanza(line: bytes, stanzas: list[Stanza]) -> Stanza:
    """The argument line's stanza, appended to the running set; an
    unknown type parses like any other (identities skip what they
    do not open), under the reference's caps — stanza count and
    argument count."""
    if len(stanzas) >= MAX_STANZAS:
        raise AgeCryptoError("the header carries too many stanzas")
    try:
        args = line[3:].decode("ascii").split()
    except UnicodeDecodeError:
        raise AgeCryptoError("a stanza line is not ASCII") from None
    if not args:
        raise AgeCryptoError("a stanza line with no type")
    if len(args) - 1 > MAX_STANZA_ARGS:
        raise AgeCryptoError("a stanza carries too many arguments")
    stanza = Stanza(kind=args[0], args=args[1:], body=b"")
    stanzas.append(stanza)
    return stanza


def accept_body_line(line: bytes, current: Stanza | None) -> None:
    """One body line onto its stanza: unpadded base64, 64 columns
    at most. A short line closes the stanza — only another stanza
    or the MAC may follow (the reference parser's rule; a body
    line after the close is malformed, not continued)."""
    if current is None:
        raise AgeCryptoError("a body line outside any stanza")
    if len(line) > 64:
        raise AgeCryptoError("a stanza body line over 64 columns")
    try:
        piece = b64d(line.decode("ascii"))
    except AgeCryptoError, UnicodeDecodeError:
        raise AgeCryptoError("a stanza body line is not base64") from None
    current.body += piece


def verify_header(file_key: bytes, mac_input: bytes, mac: bytes) -> None:
    """The MAC check over the parsed header; a mismatch names the
    file as modified, not merely undecryptable."""
    expect = hmac.new(
        hkdf_key(file_key, None, HEADER_INFO),
        mac_input,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expect, mac):
        raise AgeCryptoError("the header failed its MAC check")


def stream_encrypt(payload_key: bytes, plaintext: bytes) -> bytes:
    """The payload's STREAM: 64 KiB chunks under ChaCha20-Poly1305
    with an 11-byte big-endian counter and a final-chunk flag in
    the twelfth nonce byte. The last chunk carries the flag and
    may be short or full; an empty plaintext still produces its
    one empty final chunk — the shape the reference writer emits
    and its reader requires (a message always ends on a marked
    chunk)."""
    aead = ChaCha20Poly1305(payload_key)
    out: list[bytes] = []
    pos = 0
    counter = 0
    while True:
        block = plaintext[pos : pos + CHUNK_BYTES]
        pos += CHUNK_BYTES
        final = 1 if pos >= len(plaintext) else 0
        nonce = counter.to_bytes(11, "big") + bytes([final])
        out.append(aead.encrypt(nonce, block, b""))
        if final:
            return b"".join(out)
        counter += 1


def stream_decrypt(payload_key: bytes, payload: bytes) -> bytes:
    """The STREAM mirror: a payload always ends on a marked chunk,
    so a payload of no chunk at all is refused (the reference's
    rule). A truncated or tampered payload fails its tag under the
    chunk nonce it would have needed — including a final chunk
    somebody cut off — so every mutilation surfaces as the same
    named error."""
    if not payload:
        raise AgeCryptoError("the payload ended without a final chunk")
    aead = ChaCha20Poly1305(payload_key)
    out: list[bytes] = []
    pos = 0
    counter = 0
    while pos < len(payload):
        block = payload[pos : pos + CHUNK_BYTES + 16]
        pos += CHUNK_BYTES + 16
        nonce = counter.to_bytes(11, "big") + bytes(
            [1 if pos >= len(payload) else 0]
        )
        try:
            out.append(aead.decrypt(nonce, block, b""))
        except Exception:
            raise AgeCryptoError("the payload failed authentication") from None
        counter += 1
    return b"".join(out)


def encrypt(value: bytes, public_line: str) -> bytes:
    """A whole age file: *value* encrypted to one ssh public key
    line. One recipient per file is msks's shape (#513: one blob
    per recipient, one stanza per blob) — the format allows more,
    and this stays the minimal honest unit."""
    target = recipient(public_line)
    file_key = pysecrets.token_bytes(FILE_KEY_BYTES)
    header = build_header(file_key, [wrap_key(target, file_key)])
    nonce = pysecrets.token_bytes(16)
    payload_key = hkdf_key(file_key, nonce, PAYLOAD_INFO)
    return header + nonce + stream_encrypt(payload_key, value)


def decrypt(
    blob: bytes, key_pem: bytes, prompt: Callable[[], str | bytes]
) -> bytes:
    """The whole-file mirror: parse the header, unwrap the file
    key with the loaded identity (a stanza this key does not open
    is skipped — possession is proven by the unwrap, never
    claimed), verify the header MAC, and STREAM-decrypt the
    payload."""
    stanzas, mac, mac_input, payload = parse_header(blob)
    identity = load_identity(key_pem, prompt)
    file_key: bytes | None = None
    for stanza in stanzas:
        try:
            file_key = identity.unwrap(stanza)
            break
        except NotForThisKey:
            continue
    if file_key is None:
        raise AgeCryptoError("no stanza in the file matches this key")
    verify_header(file_key, mac_input, mac)
    if len(payload) < 16:
        raise AgeCryptoError("the payload carries no nonce")
    payload_key = hkdf_key(file_key, payload[:16], PAYLOAD_INFO)
    return stream_decrypt(payload_key, payload[16:])
