"""TLS material for the single HTTPS+WSS listener (#8).

Two modes:

- operator-provided certificate (``MSKSD_TLS_CERT``/``MSKSD_TLS_KEY``)
  used verbatim;
- otherwise a self-signed CA + leaf generated into the state dir on
  first run and reused afterwards. The CA fingerprint is logged at
  startup so first-connect clients can pin it (trust-on-first-use:
  the daemon's own TOFU, like SSH's).

Both the HTTPS listener and the WSS event channel ride this one
certificate — one surface, one trust story.
"""

import datetime
import hashlib
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CA_CERT = "msks-ca.pem"
CA_KEY = "msks-ca-key.pem"
LEAF_CERT = "msks-cert.pem"
LEAF_KEY = "msks-key.pem"
CERT_DAYS = 825
CA_DAYS = 3650


def fingerprint(cert_pem: bytes) -> str:
    """The SHA-256 fingerprint clients pin (TOFU)."""
    return hashlib.sha256(cert_pem).hexdigest()


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def generate_ca() -> tuple[bytes, bytes]:
    """A fresh self-signed CA (cert PEM, key PEM)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = _name("msks CA")
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def generate_leaf(
    ca_cert_pem: bytes, ca_key_pem: bytes, host: str
) -> tuple[bytes, bytes]:
    """A leaf certificate for ``host`` signed by the CA PEMs."""
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    names: list[x509.GeneralName] = [x509.DNSName(host)]
    with _suppress_value_error():
        names.append(x509.IPAddress(ipaddress.ip_address(host)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("msks"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(names),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


class _suppress_value_error:
    """Context manager absorbing IPAddress parse failures for hostnames."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is ValueError


def load_or_generate(
    state_dir: Path, host: str, tls_cert: str | None, tls_key: str | None
) -> tuple[str, str, str | None]:
    """Resolve ``(cert_path, key_path, ca_fingerprint)`` for the listener.

    Operator-provided paths win as-is (no fingerprint — the operator's
    CA is the trust story). Otherwise a CA + leaf are generated under
    ``state_dir`` once and reused, and the CA fingerprint is returned
    for startup logging (TOFU pinning).
    """
    if tls_cert and tls_key:
        return tls_cert, tls_key, None
    ca_cert = state_dir / CA_CERT
    ca_key = state_dir / CA_KEY
    leaf_cert = state_dir / LEAF_CERT
    leaf_key = state_dir / LEAF_KEY
    if not ca_cert.is_file():
        cert_pem, key_pem = generate_ca()
        _write(ca_cert, cert_pem, 0o644)
        _write(ca_key, key_pem, 0o600)
    if not leaf_cert.is_file():
        cert_pem, key_pem = generate_leaf(
            ca_cert.read_bytes(), ca_key.read_bytes(), host
        )
        _write(leaf_cert, cert_pem, 0o644)
        _write(leaf_key, key_pem, 0o600)
    return str(leaf_cert), str(leaf_key), fingerprint(ca_cert.read_bytes())
