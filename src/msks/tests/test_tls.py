"""TLS material: generation, operator-provided mode, TOFU fingerprint."""

import datetime
import ipaddress
import socket
import ssl
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from msks.server import tls
from msks.server.tls import (
    fingerprint,
    generate_ca,
    generate_leaf,
    load_or_generate,
)


def test_generate_ca_and_leaf_roundtrip() -> None:
    ca_cert, ca_key = generate_ca()
    leaf_cert, leaf_key = generate_leaf(ca_cert, ca_key, "localhost")
    assert ca_cert.startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"BEGIN EC PRIVATE" in ca_key or b"PRIVATE KEY" in ca_key
    assert leaf_cert.startswith(b"-----BEGIN CERTIFICATE-----")
    assert fingerprint(leaf_cert) != fingerprint(ca_cert)


def test_generated_pair_verifies_under_strict_client(tmp_path: Path) -> None:
    """The minted CA + leaf pass Python 3.14's default client context.

    ``ssl.create_default_context()`` sets VERIFY_X509_STRICT there,
    which rejects a chain without Subject/Authority Key Identifiers —
    the exact failure the first bare-host dev loop hit (#141): curl
    accepted the pair, the msks client did not. The handshake below
    pins that the minted material can never regress behind the
    strictness clients actually run.
    """
    ca_cert, ca_key = generate_ca()
    leaf_cert, leaf_key = generate_leaf(ca_cert, ca_key, "127.0.0.1")
    ca = x509.load_pem_x509_certificate(ca_cert)
    leaf = x509.load_pem_x509_certificate(leaf_cert)
    caSKI = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    leafSKI = leaf.extensions.get_extension_for_class(
        x509.SubjectKeyIdentifier
    )
    leafAKI = leaf.extensions.get_extension_for_class(
        x509.AuthorityKeyIdentifier
    )
    assert leafAKI.value.key_identifier == caSKI.value.digest
    assert leafSKI.value.digest != caSKI.value.digest
    # The pair as files: load_cert_chain wants paths, and the leaf's
    # SAN names 127.0.0.1, so check_hostname on the client matches
    # the very address the dev daemon serves.
    cert_file = tmp_path / "leaf.pem"
    key_file = tmp_path / "leaf-key.pem"
    cert_file.write_bytes(leaf_cert)
    key_file.write_bytes(leaf_key)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    client_ctx = ssl.create_default_context(cafile=None)
    # Explicit rather than trusting the interpreter's default: the
    # assertion must hold under strictness everywhere it can be set.
    client_ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    client_ctx.load_verify_locations(cadata=ca_cert.decode())
    client_ctx.check_hostname = True
    client, server = socket.socketpair()
    try:
        tls_server = server_ctx.wrap_socket(
            server, server_side=True, do_handshake_on_connect=False
        )
        tls_client = client_ctx.wrap_socket(
            client, do_handshake_on_connect=False, server_hostname="127.0.0.1"
        )
        handshake: list[BaseException] = []

        def run_server() -> None:
            try:
                tls_server.do_handshake()
            except BaseException as exc:  # noqa: BLE001 - recorded, not raised
                handshake.append(exc)

        thread = threading.Thread(target=run_server)
        thread.start()
        try:
            tls_client.do_handshake()  # raises SSLError on a rejected chain
        finally:
            thread.join(timeout=5)
        # A deadlocked server thread would pass the empty-`handshake`
        # assert below vacuously — it must have finished.
        assert not thread.is_alive(), "server handshake thread did not finish"
        assert not handshake, f"server handshake failed: {handshake}"
        assert tls_client.getpeercert()["subject"]
    finally:
        client.close()
        server.close()


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


def test_corrupt_ca_regenerates(tmp_path: Path) -> None:
    # A hard power cut between create and fsync leaves zero-length
    # PEMs; the daemon must regenerate the CA instead of crash-looping
    # on MalformedFraming (found live by the appliance e2e).
    (tmp_path / "msks-ca.pem").write_bytes(b"")
    (tmp_path / "msks-ca-key.pem").write_bytes(b"")
    cert, key, fp = tls.load_or_generate(tmp_path, "127.0.0.1", None, None)
    assert fp and Path(cert).is_file() and Path(key).is_file()


def test_truncated_ca_key_regenerates(tmp_path: Path) -> None:
    cert_pem, key_pem = tls.generate_ca()
    (tmp_path / "msks-ca.pem").write_bytes(cert_pem)
    (tmp_path / "msks-ca-key.pem").write_bytes(key_pem[:37])  # torn write
    _, _, fp = tls.load_or_generate(tmp_path, "127.0.0.1", None, None)
    assert fp


def test_ca_usable_rejects_garbage(tmp_path: Path) -> None:
    assert tls._ca_usable(tmp_path / "a", tmp_path / "b") is False
    (tmp_path / "a").write_bytes(b"not a pem")
    (tmp_path / "b").write_bytes(b"not a pem")
    assert tls._ca_usable(tmp_path / "a", tmp_path / "b") is False


def mint_legacy_ca() -> tuple[bytes, bytes]:
    """A self-signed CA shaped exactly like the pre-strict-clean mint.

    BasicConstraints(ca=True, path_length=0) and nothing else — no
    SKI (the pre-#141 generate_ca wrote exactly this). Parses fine,
    which is what made it toxic: the leaf-remint path reads the CA's
    SKI to build the new leaf's AKI and crashed against it (#148).
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "msks CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=10))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def test_legacy_ca_is_replaced_wholesale(tmp_path: Path) -> None:
    """A no-SKI CA parses but cannot mint strict-clean leaves.

    The daemon used to crash-loop on the leaf remint against it
    (#148); now the whole pair is replaced and the next boot serves.
    """
    ca_pem, key_pem = mint_legacy_ca()
    (tmp_path / "msks-ca.pem").write_bytes(ca_pem)
    (tmp_path / "msks-ca-key.pem").write_bytes(key_pem)
    old_fp = fingerprint(ca_pem)
    cert, _key, fp = load_or_generate(tmp_path, "192.168.77.2", None, None)
    assert fp != old_fp
    new_ca = x509.load_pem_x509_certificate(
        (tmp_path / "msks-ca.pem").read_bytes()
    )
    new_ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    leaf = x509.load_pem_x509_certificate(Path(cert).read_bytes())
    leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    # Converged: a second resolve is a reuse, not another replacement.
    cert2, _key2, fp2 = load_or_generate(tmp_path, "192.168.77.2", None, None)
    assert fp2 == fp and Path(cert2) == Path(cert)


def test_ca_replacement_is_named_on_stderr(tmp_path, capsys) -> None:
    ca_pem, key_pem = mint_legacy_ca()  # ONE pair: cert and key correspond
    (tmp_path / "msks-ca.pem").write_bytes(ca_pem)
    (tmp_path / "msks-ca-key.pem").write_bytes(key_pem)
    load_or_generate(tmp_path, "h", None, None)
    err = capsys.readouterr().err
    assert "replacing an unusable CA" in err


def test_mismatched_ca_pair_is_replaced(tmp_path, capsys) -> None:
    """New cert beside an old key (a crash between the two writes).

    Both parse and the cert has its SKI; without the correspondence
    check the mint path signed a leaf with the wrong key and served
    a chain no client could verify — silently. Such a pair is
    unusable: replaced wholesale, named on stderr.
    """
    good_cert, _good_key = generate_ca()  # has SKI
    _other_cert, other_key = generate_ca()  # a DIFFERENT key
    (tmp_path / "msks-ca.pem").write_bytes(good_cert)
    (tmp_path / "msks-ca-key.pem").write_bytes(other_key)
    cert, _key, _fp = load_or_generate(tmp_path, "h", None, None)
    ca = x509.load_pem_x509_certificate(
        (tmp_path / "msks-ca.pem").read_bytes()
    )
    ca_key = serialization.load_pem_private_key(
        (tmp_path / "msks-ca-key.pem").read_bytes(), password=None
    )
    leaf = x509.load_pem_x509_certificate(Path(cert).read_bytes())
    assert (
        ca.public_key().public_numbers()
        == ca_key.public_key().public_numbers()
    )
    leaf.verify_directly_issued_by(ca)
    assert "mismatched" in capsys.readouterr().err


def test_ca_usable_contract() -> None:
    """The three sides of usable: strict-clean yes, no-SKI no, wrong key no."""
    import tempfile

    from msks.server.tls import _ca_usable

    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        good_cert, good_key = generate_ca()
        (state / "msks-ca.pem").write_bytes(good_cert)
        (state / "msks-ca-key.pem").write_bytes(good_key)
        assert _ca_usable(state / "msks-ca.pem", state / "msks-ca-key.pem")
        legacy_cert, legacy_key = mint_legacy_ca()
        (state / "msks-ca.pem").write_bytes(legacy_cert)
        (state / "msks-ca-key.pem").write_bytes(legacy_key)
        assert not _ca_usable(state / "msks-ca.pem", state / "msks-ca-key.pem")
        (state / "msks-ca-key.pem").write_bytes(other_key := good_key)
        # a strict-clean cert beside a foreign strict-clean key
        _, foreign_key = generate_ca()
        (state / "msks-ca.pem").write_bytes(good_cert)
        (state / "msks-ca-key.pem").write_bytes(foreign_key)
        assert not _ca_usable(state / "msks-ca.pem", state / "msks-ca-key.pem")
        assert other_key == good_key  # the earlier rebind did not matter


def test_first_mint_prints_no_replacement_note(tmp_path, capsys) -> None:
    load_or_generate(tmp_path, "h", None, None)
    assert "replacing" not in capsys.readouterr().err


def test_strict_clean_ca_keeps_leaf_only_remint(tmp_path: Path) -> None:
    """A usable CA survives a host change: leaf-only, same CA fingerprint."""
    _cert, _key, fp = load_or_generate(tmp_path, "127.0.0.1", None, None)
    ca_bytes_before = (tmp_path / "msks-ca.pem").read_bytes()
    cert, _key2, fp2 = load_or_generate(tmp_path, "192.168.77.2", None, None)
    assert fp2 == fp
    assert (tmp_path / "msks-ca.pem").read_bytes() == ca_bytes_before
    leaf = x509.load_pem_x509_certificate(Path(cert).read_bytes())
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert x509.IPAddress(ipaddress.ip_address("192.168.77.2")) in san.value
