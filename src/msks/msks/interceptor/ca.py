"""The daemon's interceptor CA and its connection leaves (#199, #485).

One CA for the whole daemon signs every leaf the interceptor
serves, one per TLS connection, keyed by the connection's SNI — so
a guest that trusts the seeded certificate (the #200 seed
composition) validates each intercepted flow. The CA was
per-workspace until #485: the split defended only against a theft
of the daemon host's key material (each workspace's interception
path confined to its own CA), while every guest holds only the
public certificate either way and can sign nothing — the container
threat the interception path faces is indifferent to the split,
and one mint replaced per-workspace state, per-workspace healing,
and per-workspace seeding variance.

Both halves are minted with msks code, not mitmproxy's
``CertStore``: the store mints RSA-only CAs, and its leaf path
signs with SHA-256 — a hard ``ValueError`` under the Ed25519 keys
the #111/#138 FIPS posture chooses (the #194 spike's finding).

The CA is Ed25519 and stays so for the leaves: nothing in the
daemon, the leaf format, or the wire depends on the type.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

#: The CA's files inside the directory the caller owns — the
#: daemon state root for the interceptor CA (#485), the probe
#: service's own directory for its service identity.
CA_KEY_FILE = "interceptor-ca.key"
CA_CERT_FILE = "interceptor-ca.crt"

#: The CA outlives every workspace boot cycle it will ever serve.
CA_DAYS = 3650

#: A leaf covers one connection's life; the wide window only
#: absorbs clock skew between daemon and guest.
LEAF_BACKDATE_S = 3600
LEAF_DAYS = 1


@dataclass(frozen=True)
class Authority:
    """A loaded CA: key, cert, and chain file path. One instance
    serves the whole daemon (#485); the probe service keeps its
    own beside it."""

    key: ed25519.Ed25519PrivateKey
    cert: x509.Certificate
    chain_file: Path


def now_utc() -> datetime:
    """The clock every mint in this module reads (a test seam)."""
    return datetime.now(UTC)


def sign(builder, key: ed25519.Ed25519PrivateKey) -> x509.Certificate:
    """Finish a builder under an Ed25519 key — EdDSA takes no
    separate digest parameter, so ``algorithm=None`` is the only
    spelling cryptography accepts."""
    return builder.sign(key, algorithm=None)


def mint_ca(
    label: str = "msks interceptor CA",
) -> tuple[ed25519.Ed25519PrivateKey, x509.Certificate]:
    """A fresh self-signed CA; the label names it in the subject."""
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
    now = now_utc()
    cert = sign(
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=CA_DAYS))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                data_encipherment=False,
                decipher_only=False,
                encipher_only=False,
                key_agreement=False,
                key_encipherment=False,
            ),
            critical=True,
        ),
        key,
    )
    return key, cert


def key_pem(key: ed25519.Ed25519PrivateKey) -> bytes:
    """The CA key's PEM bytes (unencrypted: the file's 0600 mode and
    the state dir's ownership are the protection)."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def load_or_mint(
    directory: Path, label: str = "msks interceptor CA"
) -> Authority:
    """The CA of the directory the caller owns: loaded when both
    halves exist, minted and written (key 0600) when they do not.
    The interceptor's is the daemon state root — one CA for every
    workspace (#485); the probe service's is its own directory,
    the same file shape for a different identity, its *label*
    distinguishing the two in diagnostics."""
    key_path = directory / CA_KEY_FILE
    cert_path = directory / CA_CERT_FILE
    if key_path.exists() and cert_path.exists():
        key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        # A pair that does not match is the residue of two writers
        # racing the mint (key and cert are two files, two writes) —
        # half of an interleaving. Serve nothing from it: minting
        # fresh replaces both halves atomically enough (the match
        # check here is what catches the next load if it interleaves
        # again).
        if cert.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ) == key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ):
            return Authority(key=key, cert=cert, chain_file=cert_path)
    key, cert = mint_ca(label)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key_pem(key))
    cert_path.write_bytes(cert_pem(cert))
    return Authority(key=key, cert=cert, chain_file=cert_path)


def mint_leaf(
    ca: Authority,
    sni: str,
    altnames: tuple[str, ...] | None = None,
    days: int = LEAF_DAYS,
) -> tuple[ed25519.Ed25519PrivateKey, x509.Certificate]:
    """One connection's leaf, signed by the CA and carrying
    the SNI as both subject and SAN (RFC 2818: the SAN is the
    identity a client checks). *altnames* widens the SAN set — the
    live test's origins serve several names from one leaf.
    *days* widens the validity window — the probe service's leaf
    (#424) outlives its connections the way a real service's
    certificate does."""
    names = altnames or (sni,)
    key = ed25519.Ed25519PrivateKey.generate()
    now = now_utc()
    cert = sign(
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sni)])
        )
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(seconds=LEAF_BACKDATE_S))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(name) for name in names]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca.key.public_key()
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        ),
        ca.key,
    )
    return key, cert
