"""TLS material: generation, operator-provided mode, TOFU fingerprint."""

from pathlib import Path

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


def test_partial_operator_config_falls_back(tmp_path: Path) -> None:
    # Only a cert path is given: treated as not operator-provided.
    cert, key, fp = load_or_generate(tmp_path, "h", "/op/c.pem", None)
    assert Path(cert).is_file()
    assert fp is not None
