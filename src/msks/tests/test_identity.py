"""The minted workspace identity (#111): mint, seed script, compose."""

from cryptography.hazmat.primitives import serialization
from msks.identity import (
    KEY_TYPES,
    MIME_BOUNDARY,
    compose_user_data,
    mint,
    operator_content_type,
    seed_script,
    trailing_newline,
    unique_boundary,
)

PUBLIC = "ecdsa-sha2-nistp256 AAAAE2VjZHNh user@host"


def minted_public(private_pem: str) -> str:
    """The public line a private half derives to, for round-trip
    checks that the two halves mint() returns are one keypair."""
    key = serialization.load_ssh_private_key(private_pem.encode(), password=b"")
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )


def test_mint_default_is_ecdsa_p256() -> None:
    """ECDSA P-256 (#115's FIPS-approvable default) with halves that
    are actually one keypair."""
    private_pem, public = mint("ecdsa")
    assert public.startswith("ecdsa-sha2-nistp256 ")
    assert private_pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert minted_public(private_pem).split()[:2] == public.split()[:2]


def test_mint_each_supported_type() -> None:
    """Every KEY_TYPES entry mints to its own OpenSSH name."""
    for key_type, ssh_name in KEY_TYPES.items():
        private_pem, public = mint(key_type)
        assert public.startswith(f"{ssh_name} ")
        assert minted_public(private_pem).split()[:2] == public.split()[:2]


def test_mint_rejects_unknown_type() -> None:
    try:
        mint("bogus")
    except ValueError as exc:
        assert "unknown ssh key type" in str(exc)
    else:
        raise AssertionError("mint accepted an unknown type")


def test_seed_script_plants_both_users_idempotently() -> None:
    """The script targets root and the msks user with the
    mkdir/chmod/append shape, and a key already present is not
    duplicated."""
    script = seed_script(PUBLIC)
    assert script.startswith("#!/bin/sh\n")
    assert f"key='{PUBLIC}'" in script
    assert script.count("grep -qxF") == 2
    assert "/root/.ssh/authorized_keys" in script
    assert "/home/msks/.ssh/authorized_keys" in script
    assert "chown msks:msks" in script
    # Idempotent shape: the append only runs when grep misses.
    assert script.count("|| printf") == 2
    assert script.count(">> /root/.ssh/authorized_keys") == 1
    assert script.count(">> /home/msks/.ssh/authorized_keys") == 1


def test_compose_without_key_is_verbatim() -> None:
    """No minted identity: the operator payload travels unchanged
    (the #41 contract)."""
    payload = "#!/bin/sh\necho hi\n"
    assert compose_user_data(payload, None) == payload


def test_compose_without_payload_is_the_script() -> None:
    """A minted key and no operator payload: the seed is the script
    alone, one plain document."""
    assert compose_user_data(None, PUBLIC) == seed_script(PUBLIC)


def test_compose_merges_script_and_script_payload() -> None:
    """Both halves present: MIME multipart, script first, the
    operator's shell payload second with its sniffed type."""
    payload = "#!/bin/sh\necho operator\n"
    composed = compose_user_data(payload, PUBLIC)
    assert composed.startswith(
        f'Content-Type: multipart/mixed; boundary="{MIME_BOUNDARY}"'
    )
    assert 'Content-Type: text/x-shellscript; charset="utf-8"' in composed
    assert 'Content-Type: text/cloud-config; charset="utf-8"' not in composed
    assert seed_script(PUBLIC) in composed
    assert payload in composed
    assert composed.endswith(f"--{MIME_BOUNDARY}--\n")


def test_compose_merges_cloud_config_payload() -> None:
    """A #cloud-config operator payload rides as text/cloud-config."""
    payload = "#cloud-config\npackages: []\n"
    composed = compose_user_data(payload, PUBLIC)
    assert 'Content-Type: text/cloud-config; charset="utf-8"' in composed


def test_compose_appends_missing_trailing_newline() -> None:
    """A payload without a final newline gets one: the closing
    boundary must start on its own line."""
    payload = "#!/bin/sh\necho operator"
    composed = compose_user_data(payload, PUBLIC)
    assert f"\n--{MIME_BOUNDARY}--" in composed


def test_unique_boundary_falls_back_when_embedded() -> None:
    """A payload embedding the boundary string forces a random one;
    ordinary payloads keep the documented boundary."""
    hostile = f"echo {MIME_BOUNDARY}\n"
    assert MIME_BOUNDARY not in unique_boundary(hostile, seed_script(PUBLIC))
    assert MIME_BOUNDARY in unique_boundary("echo hi\n", seed_script(PUBLIC))


def test_operator_content_type_by_first_line() -> None:
    assert operator_content_type("#cloud-config\n") == "text/cloud-config"
    assert operator_content_type("#!/bin/sh\n") == "text/x-shellscript"
    # Anything without a recognized first line is a shell script —
    # the #41 forms are exactly these.
    assert operator_content_type("echo bare") == "text/x-shellscript"
    # Whole-line match: cloud-config-archive is its own cloud-init
    # handler, not a prefix of cloud-config.
    assert (
        operator_content_type("#cloud-config-archive\n") == "text/cloud-config-archive"
    )
    assert operator_content_type("#cloud-config-with-suffix\n") == (
        "text/x-shellscript"
    )


def test_trailing_newline() -> None:
    assert trailing_newline("x\n") == "x\n"
    assert trailing_newline("x") == "x\n"
