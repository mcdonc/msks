"""The per-workspace interceptor CA and its leaves (#199)."""

from datetime import timedelta

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from msks.interceptor import ca


def test_mint_ca_is_an_ed25519_ca() -> None:
    key, cert = ca.mint_ca("ws-a")
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    assert cert.public_key().__class__ is key.public_key().__class__
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.value.ca is True
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.value.key_cert_sign
    assert "ws-a" in cert.subject.rfc4514_string()


def test_load_or_mint_persists_and_round_trips(tmp_path) -> None:
    first = ca.load_or_mint(tmp_path, "ws-a")
    key_path = tmp_path / ca.CA_KEY_FILE
    cert_path = tmp_path / ca.CA_CERT_FILE
    assert key_path.exists() and cert_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600
    again = ca.load_or_mint(tmp_path, "ws-a")
    assert again.cert == first.cert
    assert again.key.private_bytes_raw() == first.key.private_bytes_raw()
    assert again.chain_file == cert_path


def test_mint_leaf_is_sni_keyed_and_ca_signed(tmp_path) -> None:
    authority = ca.load_or_mint(tmp_path, "ws-a")
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


def test_leaves_of_two_workspaces_stay_separate(tmp_path) -> None:
    """CA separation (#194): a leaf from ws-a's CA does not verify
    under ws-b's — the guest-visible property the spike proved."""
    ca_a = ca.load_or_mint(tmp_path / "a", "ws-a")
    ca_b = ca.load_or_mint(tmp_path / "b", "ws-b")
    _, leaf = ca.mint_leaf(ca_a, "api.example.com")
    assert leaf.issuer == ca_a.cert.subject
    assert leaf.issuer != ca_b.cert.subject
    try:
        ca_b.cert.public_key().verify(
            leaf.signature, leaf.tbs_certificate_bytes
        )
    except Exception:  # noqa: BLE001 - any verification failure is the pass
        return
    raise AssertionError("ws-a's leaf verified under ws-b's CA")


def test_a_partial_ca_pair_mints_fresh(tmp_path) -> None:
    """A key without its cert — residue of an interrupted
    mint — cannot serve half a CA: the pair mints anew."""
    authority = ca.load_or_mint(tmp_path, "ws-a")
    (tmp_path / ca.CA_CERT_FILE).unlink()
    fresh = ca.load_or_mint(tmp_path, "ws-a")
    assert fresh.cert != authority.cert
    cn = fresh.cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0]
    assert cn.value == "msks ws-a interceptor CA"
