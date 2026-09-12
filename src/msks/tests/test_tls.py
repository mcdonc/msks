"""TLS material: generation, operator-provided mode, TOFU fingerprint."""

from pathlib import Path

import pytest
from msks.server.tls import fingerprint, generate_ca, generate_leaf, load_or_generate


def test_generate_ca_and_leaf_roundtrip() -> None:
    ca_cert, ca_key = generate_ca()
    leaf_cert, leaf_key = generate_leaf(ca_cert, ca_key, "localhost")
    assert ca_cert.startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"BEGIN EC PRIVATE" in ca_key or b"PRIVATE KEY" in ca_key
    assert leaf_cert.startswith(b"-----BEGIN CERTIFICATE-----")
    assert fingerprint(leaf_cert) != fingerprint(ca_cert)


def test_generate_leaf_with_ip_host() -> None:
    ca_cert, ca_key = generate_ca()
    leaf_cert, _key = generate_leaf(ca_cert, ca_key, "127.0.0.1")
    assert fingerprint(leaf_cert)


def test_load_or_generate_operator_paths(tmp_path: Path) -> None:
    cert, key, fp = load_or_generate(tmp_path, "h", "/op/c.pem", "/op/k.pem")
    assert (cert, key, fp) == ("/op/c.pem", "/op/k.pem", None)


def test_load_or_generate_creates_and_reuses(tmp_path: Path) -> None:
    cert, key, fp = load_or_generate(tmp_path, "localhost", None, None)
    assert Path(cert).is_file()
    assert Path(key).is_file()
    assert len(fp) == 64
    first = Path(cert).read_bytes()
    cert2, _key2, fp2 = load_or_generate(tmp_path, "localhost", None, None)
    assert Path(cert2).read_bytes() == first
    assert fp2 == fp


def test_partial_operator_config_is_an_error(tmp_path: Path) -> None:
    # Only a cert path is given: a misconfiguration, not a silent
    # fallback to a different trust story.
    with pytest.raises(ValueError, match="together"):
        load_or_generate(tmp_path, "h", "/op/c.pem", None)


def test_leaf_regenerates_on_host_change(tmp_path: Path) -> None:
    cert, _key, _fp = load_or_generate(tmp_path, "alpha.local", None, None)
    first = Path(cert).read_bytes()
    cert2, _key2, _fp2 = load_or_generate(tmp_path, "beta.local", None, None)
    assert Path(cert2).read_bytes() != first


def test_leaf_regenerates_on_ca_change(tmp_path: Path) -> None:
    cert, _key, _fp = load_or_generate(tmp_path, "h", None, None)
    first = Path(cert).read_bytes()
    (tmp_path / "msks-ca.pem").unlink()
    (tmp_path / "msks-ca-key.pem").unlink()
    cert2, _key2, _fp2 = load_or_generate(tmp_path, "h", None, None)
    assert Path(cert2).read_bytes() != first
    assert _fp2 != _fp
