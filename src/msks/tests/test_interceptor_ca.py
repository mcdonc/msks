"""The daemon's interceptor CA and its leaves (#199, #485)."""

from datetime import timedelta

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from msks.interceptor import ca


def test_mint_ca_is_an_ed25519_ca() -> None:
    key, cert = ca.mint_ca()
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    assert cert.public_key().__class__ is key.public_key().__class__
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.value.ca is True
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.value.key_cert_sign
    assert "interceptor" in cert.subject.rfc4514_string()


def test_load_or_mint_persists_and_round_trips(tmp_path) -> None:
    first = ca.load_or_mint(tmp_path)
    key_path = tmp_path / ca.CA_KEY_FILE
    cert_path = tmp_path / ca.CA_CERT_FILE
    assert key_path.exists() and cert_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600
    again = ca.load_or_mint(tmp_path)
    assert again.cert == first.cert
    assert again.key.private_bytes_raw() == first.key.private_bytes_raw()
    assert again.chain_file == cert_path


def test_mint_leaf_is_sni_keyed_and_ca_signed(tmp_path) -> None:
    authority = ca.load_or_mint(tmp_path)
    key, cert = ca.mint_leaf(authority, "api.example.com")
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.value.get_values_for_type(x509.DNSName) == ["api.example.com"]
    # The leaf names its CA as issuer and verifies under it.
    assert cert.issuer == authority.cert.subject
    authority.cert.public_key().verify(
        cert.signature,
        cert.tbs_certificate_bytes,
    )
    # The leaf's own key differs from the CA's.
    assert key.public_key().public_bytes_raw() != (
        authority.key.public_key().public_bytes_raw()
    )
    # The validity window covers a connection's life with backdate
    # headroom for clock skew.
    now = ca.now_utc()
    assert cert.not_valid_before_utc <= now - timedelta(minutes=55)
    assert cert.not_valid_after_utc > now


def test_one_ca_serves_every_directory_of_the_daemon(tmp_path) -> None:
    """#485: the interceptor CA is the daemon's, not the workspace's
    — the same state directory answers the same authority, and the
    per-workspace separation the #194 spike proved is retired (the
    container adversary holds only certificates either way and can
    sign nothing; the split defended daemon-host key theft, a
    different threat domain). The probe service keeps its own
    directory and its own identity beside it."""
    interceptor = ca.load_or_mint(tmp_path)
    again = ca.load_or_mint(tmp_path)
    probe = ca.load_or_mint(tmp_path / "probe")
    assert again.key.private_bytes_raw() == (
        interceptor.key.private_bytes_raw()
    )
    assert probe.key.private_bytes_raw() != (
        interceptor.key.private_bytes_raw()
    )


def test_a_partial_ca_pair_mints_fresh(tmp_path) -> None:
    """A key without its cert — residue of an interrupted
    mint — cannot serve half a CA: the pair mints anew."""
    authority = ca.load_or_mint(tmp_path)
    (tmp_path / ca.CA_CERT_FILE).unlink()
    fresh = ca.load_or_mint(tmp_path)
    assert fresh.cert != authority.cert
    cn = fresh.cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0]
    assert cn.value == "msks interceptor CA"
