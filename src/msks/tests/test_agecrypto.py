"""The age crypto leaf's tests (#513, #504).

Two evidence classes pin interop with the reference tool
(filoSottile/age v1.3.x): the **fixtures** under ``agefixtures/``
— files the reference tool encrypted to the committed throwaway
keys, decrypted here by our code — and the **live round-trips**
(skipped where no ``age`` binary exists), which prove the other
direction: the reference tool decrypts files we wrote. The
fixture keys were minted by ``ssh-keygen`` for this test tree
alone; the encrypted one answers ``correct horse battery
staple``.
"""

import base64
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod
from msks.agecrypto import (
    AgeCryptoError,
    NotForThisKey,
    aead_open,
    aead_seal,
    age_capable,
    b64d,
    build_header,
    decrypt,
    encrypt,
    fingerprint,
    hkdf_key,
    load_identity,
    mint_age_identity,
    montgomery_u,
    mpint,
    parse_header,
    recipient,
    stanza_lines,
    stream_decrypt,
    stream_encrypt,
    verify_header,
    wire_string,
    wire_strings,
    wrap_key,
    x25519_shared,
)
from testkeys import mint as mint_key

from msks import agecrypto

FIXTURES = Path(__file__).parent / "agefixtures"
PLAIN = (FIXTURES / "plain.txt").read_bytes()
ED_PUB = (FIXTURES / "ed25519.pub").read_text().strip()
ED_PEM = (FIXTURES / "ed25519").read_bytes()
ED_ENC_PEM = (FIXTURES / "ed25519_enc").read_bytes()
RSA_PUB = (FIXTURES / "rsa.pub").read_text().strip()
RSA_PEM = (FIXTURES / "rsa").read_bytes()


def no_prompt():
    pytest.fail("an unencrypted key must not prompt")


def blob_of(line: str) -> bytes:
    """The authorized_keys line's wire blob (base64 body)."""
    return base64.b64decode(line.split()[1], validate=True)


# --- round-trips and pinned fixtures ------------------------------


def test_round_trip_ed25519() -> None:
    assert decrypt(encrypt(b"the value", ED_PUB), ED_PEM, no_prompt) == (
        b"the value"
    )


def test_round_trip_rsa() -> None:
    assert decrypt(encrypt(PLAIN, RSA_PUB), RSA_PEM, no_prompt) == PLAIN


def test_round_trip_empty_and_multichunk() -> None:
    assert decrypt(encrypt(b"", ED_PUB), ED_PEM, no_prompt) == b""
    big = b"x" * (agecrypto.CHUNK_BYTES + 1000) + b"tail"
    assert decrypt(encrypt(big, ED_PUB), ED_PEM, no_prompt) == big


def test_reference_fixture_ed25519() -> None:
    blob = (FIXTURES / "ed25519.age").read_bytes()
    assert decrypt(blob, ED_PEM, no_prompt) == PLAIN


def test_reference_fixture_ed25519_passphrase() -> None:
    """The passphrase rides the callback seam — the one prompt
    path (#515's client owns it for real)."""
    blob = (FIXTURES / "ed25519_enc.age").read_bytes()
    calls = []

    def prompt() -> str:
        calls.append(1)
        return "correct horse battery staple"

    assert decrypt(blob, ED_ENC_PEM, prompt) == PLAIN
    assert len(calls) == 1


def test_reference_fixture_rsa() -> None:
    blob = (FIXTURES / "rsa.age").read_bytes()
    assert decrypt(blob, RSA_PEM, no_prompt) == PLAIN


def test_stanza_fingerprint_matches_the_reference_hint() -> None:
    """The fp in a reference stanza is sha256 of the wire blob —
    our derivation must land on the same four bytes (the hint the
    unwrap gates on)."""
    blob = (FIXTURES / "ed25519.age").read_bytes()
    stanzas, _mac, _inp, _rest = parse_header(blob)
    assert stanzas[0].args[0] == recipient(ED_PUB).fp


# --- live interop (the reference tool decrypts ours) ---------------


AGE = shutil.which("age")


@pytest.mark.skipif(AGE is None, reason="the age binary is absent")
def test_live_tool_decrypts_our_ed25519_file(tmp_path: Path) -> None:
    out = tmp_path / "ours.age"
    out.write_bytes(encrypt(b"live check\n", ED_PUB))
    seen = subprocess.run(
        [AGE, "-d", "-i", str(FIXTURES / "ed25519"), str(out)],
        capture_output=True,
        check=True,
    )
    assert seen.stdout == b"live check\n"


@pytest.mark.skipif(AGE is None, reason="the age binary is absent")
def test_live_tool_decrypts_our_rsa_file(tmp_path: Path) -> None:
    out = tmp_path / "ours-rsa.age"
    out.write_bytes(encrypt(b"live rsa\n", RSA_PUB))
    seen = subprocess.run(
        [AGE, "-d", "-i", str(FIXTURES / "rsa"), str(out)],
        capture_output=True,
        check=True,
    )
    assert seen.stdout == b"live rsa\n"


@pytest.mark.skipif(AGE is None, reason="the age binary is absent")
def test_live_tool_encrypts_for_us(tmp_path: Path) -> None:
    out = tmp_path / "theirs.age"
    subprocess.run(
        [AGE, "-r", ED_PUB, "-o", str(out)],
        input=b"their check\n",
        check=True,
    )
    assert decrypt(out.read_bytes(), ED_PEM, no_prompt) == b"their check\n"


# --- the capability predicate (#514's refusal gate) ----------------


def test_age_capable_names_the_two_reference_types() -> None:
    assert age_capable(ED_PUB)
    assert age_capable(RSA_PUB)
    assert not age_capable("ecdsa-sha2-nistp256 AAAAB2NzaC1yc2E x")
    assert not age_capable("sk-ssh-ed25519@openssh.com AAAAB x")
    assert not age_capable("garbage")
    assert not age_capable("")


def test_recipient_refuses_other_types_by_name() -> None:
    pem, pub = mint_key("ecdsa")
    with pytest.raises(AgeCryptoError, match="ecdsa"):
        recipient(pub)


def rsa_line_for(key: rsa_mod.RSAPrivateKey) -> str:
    numbers = key.public_key().public_numbers()
    blob = wire_string(b"ssh-rsa") + mpint(numbers.e) + mpint(numbers.n)
    return "ssh-rsa " + base64.b64encode(blob).decode()


def test_recipient_refuses_small_rsa() -> None:
    key = rsa_mod.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(AgeCryptoError, match="under 2048 bits"):
        recipient(rsa_line_for(key))


def test_recipient_rejects_malformed_blobs() -> None:
    bad_point = (
        "ssh-ed25519 "
        + base64.b64encode(
            wire_string(b"ssh-ed25519") + wire_string(b"short")
        ).decode()
    )
    with pytest.raises(AgeCryptoError, match="malformed"):
        recipient(bad_point)
    mismatched = (
        "ssh-rsa "
        + base64.b64encode(
            wire_string(b"ssh-ed25519") + wire_string(b"x" * 32)
        ).decode()
    )
    with pytest.raises(AgeCryptoError, match="does not match"):
        recipient(mismatched)
    with pytest.raises(AgeCryptoError, match="base64"):
        recipient("ssh-ed25519 !!!not-base64!!!")
    short_rsa = "ssh-rsa " + base64.b64encode(wire_string(b"ssh-rsa")).decode()
    with pytest.raises(AgeCryptoError, match="malformed ssh-rsa"):
        recipient(short_rsa)


def test_rsa_stanza_with_a_broken_body_is_a_miss() -> None:
    """The fingerprint matches, the OAEP body does not — a miss
    (the hint may collide or lie; the padding check cannot)."""
    identity = load_identity(RSA_PEM, no_prompt)
    broken = agecrypto.Stanza(
        kind="ssh-rsa", args=[identity.fp], body=b"junk-junk-junk"
    )
    with pytest.raises(NotForThisKey):
        identity.unwrap(broken)


# --- curve25519 field arithmetic -----------------------------------


def test_montgomery_rejects_noncanonical_and_off_curve() -> None:
    with pytest.raises(AgeCryptoError, match="non-canonical"):
        montgomery_u(b"\xff" * 32)
    with pytest.raises(AgeCryptoError, match="not on the curve"):
        montgomery_u(b"\x02" + b"\x00" * 31)


def test_montgomery_maps_low_order_points_to_zero() -> None:
    """The low-order on-curve points map to u=0 — valid input,
    degenerate only at the shared secret (the gate below)."""
    assert montgomery_u(b"\x01" + b"\x00" * 31) == b"\x00" * 32
    assert montgomery_u(b"\xec" + b"\xff" * 30 + b"\x7f") == b"\x00" * 32


def test_x25519_shared_rejects_low_order_points() -> None:
    with pytest.raises(AgeCryptoError, match="degenerate"):
        x25519_shared(b"\x01" + b"\x00" * 31, b"\x00" * 32)


# --- identity loading ----------------------------------------------


def test_load_identity_wrong_passphrase_is_named() -> None:
    with pytest.raises(AgeCryptoError, match="passphrase is wrong"):
        load_identity(ED_ENC_PEM, lambda: "not it")


def test_load_identity_passphrase_accepts_bytes() -> None:
    identity = load_identity(
        ED_ENC_PEM, lambda: b"correct horse battery staple"
    )
    assert identity.kind == "ssh-ed25519"


def test_load_identity_refuses_unsupported_types() -> None:
    pem, pub = mint_key("ecdsa")
    with pytest.raises(AgeCryptoError, match="no recipient"):
        load_identity(pem.encode(), no_prompt)


def test_load_identity_refuses_garbage() -> None:
    with pytest.raises(AgeCryptoError, match="not a private key"):
        load_identity(b"not a key at all", no_prompt)


def test_load_identity_refuses_small_rsa() -> None:
    pem = rsa_mod.generate_private_key(
        public_exponent=65537, key_size=1024
    ).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(AgeCryptoError, match="under 2048 bits"):
        load_identity(pem, no_prompt)


def test_identity_fingerprint_matches_the_public_line() -> None:
    """The wire blob rebuilt from the private half equals the
    authorized_keys blob — the fp gate would miss stanzas
    otherwise."""
    assert load_identity(ED_PEM, no_prompt).fp == fingerprint(blob_of(ED_PUB))
    assert load_identity(RSA_PEM, no_prompt).fp == fingerprint(
        blob_of(RSA_PUB)
    )


# --- unwrap misses --------------------------------------------------


def test_wrong_ed25519_key_is_a_miss_not_an_error() -> None:
    other_pem, _pub = mint_key("ed25519")
    other_pem = other_pem.encode()
    with pytest.raises(AgeCryptoError, match="no stanza"):
        decrypt(encrypt(b"theirs", ED_PUB), other_pem, no_prompt)


def test_wrong_rsa_key_is_a_miss() -> None:
    other_pem = rsa_mod.generate_private_key(
        public_exponent=65537, key_size=2048
    ).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(AgeCryptoError, match="no stanza"):
        decrypt(encrypt(b"theirs", RSA_PUB), other_pem, no_prompt)


def test_tampered_stanza_body_is_a_miss() -> None:
    """The hint may collide or lie; the body's tag cannot — a
    corrupted body fails under the right key as a miss."""
    file_key = b"\x06" * 16
    stanza = wrap_key(recipient(ED_PUB), file_key)
    identity = load_identity(ED_PEM, no_prompt)
    tampered = replace(stanza, body=b"\x00" * len(stanza.body))
    with pytest.raises(NotForThisKey):
        identity.unwrap(tampered)


def test_foreign_stanza_type_is_skipped() -> None:
    """A header may carry stanzas for other recipient types; the
    identity skips them (the format's ignore rule)."""
    identity = load_identity(ED_PEM, no_prompt)
    foreign = agecrypto.Stanza(kind="scrypt", args=[], body=b"")
    with pytest.raises(NotForThisKey):
        identity.unwrap(foreign)


def test_malformed_stanza_arguments_are_defects() -> None:
    """A stanza of our type with a bad argument count or share is
    malformed, not a miss — the reference's split."""
    identity = load_identity(ED_PEM, no_prompt)
    two_args = agecrypto.Stanza(
        kind="ssh-ed25519", args=[identity.fp, "AAAA", "extra"], body=b"x"
    )
    with pytest.raises(AgeCryptoError, match="two arguments"):
        identity.unwrap(two_args)
    bad_share = agecrypto.Stanza(
        kind="ssh-ed25519", args=[identity.fp, "!!!"], body=b"x"
    )
    with pytest.raises(AgeCryptoError, match="bad share"):
        identity.unwrap(bad_share)
    short_share = agecrypto.Stanza(
        kind="ssh-ed25519", args=[identity.fp, "AAAA"], body=b"x"
    )
    with pytest.raises(AgeCryptoError, match="bad share"):
        identity.unwrap(short_share)
    rsa_two_args = agecrypto.Stanza(kind="ssh-rsa", args=["a", "b"], body=b"x")
    rsa_identity = load_identity(RSA_PEM, no_prompt)
    with pytest.raises(AgeCryptoError, match="one argument"):
        rsa_identity.unwrap(rsa_two_args)


# --- the header and its MAC ----------------------------------------


def wrapped_header(file_key: bytes) -> tuple[bytes, bytes]:
    """A header wrapping *file_key* for the fixture key, whole and
    with one arg byte flipped (the MAC's coverage test)."""
    stanza = wrap_key(recipient(ED_PUB), file_key)
    header = build_header(file_key, [stanza])
    mark = header.index(b" " + stanza.args[0].encode())
    flipped = bytearray(header)
    flipped[mark + 2] = ord("A") if flipped[mark + 2] != ord("A") else ord("B")
    return header, bytes(flipped)


def test_header_mac_covers_the_stanza_arguments() -> None:
    file_key = b"\x07" * 16
    header, flipped = wrapped_header(file_key)
    stanzas, mac, mac_input, rest = parse_header(header)
    assert rest == b""
    verify_header(file_key, mac_input, mac)
    _stanzas, _mac, flipped_input, flipped_mac = parse_header(
        flipped + b"payload"
    )
    with pytest.raises(AgeCryptoError, match="MAC"):
        verify_header(file_key, flipped_input, flipped_mac)


def test_tampered_mac_line_is_refused() -> None:
    blob = bytearray(encrypt(b"the value", ED_PUB))
    start = blob.index(b"\n--- ") + 5
    blob[start] = 65 if blob[start] != 65 else 66  # inside the MAC
    with pytest.raises(AgeCryptoError, match="MAC"):
        decrypt(bytes(blob), ED_PEM, no_prompt)


def test_version_line_is_exact() -> None:
    with pytest.raises(AgeCryptoError, match="not an age v1 file"):
        parse_header(b"age-encryption.org/v2\n-> ssh-rsa abcd\nz\n--- aaaa\n")


def test_header_without_stanza_or_mac_is_refused() -> None:
    with pytest.raises(AgeCryptoError):
        parse_header(b"age-encryption.org/v1\n")
    with pytest.raises(AgeCryptoError, match="no stanza"):
        parse_header(b"age-encryption.org/v1\n--- aaaa\npayload")


def test_body_lines_are_validated() -> None:
    with pytest.raises(AgeCryptoError, match="base64"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\n!!!\n--- aaaa\n"
        )
    with pytest.raises(AgeCryptoError, match="64 columns"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\n"
            + b"A" * 65
            + b"\n--- aaaa\n"
        )


def test_body_after_short_line_is_refused() -> None:
    """The short line closes the stanza; the MAC line's shape is
    well-formed here, so the named property is what raises."""
    with pytest.raises(AgeCryptoError, match="outside any stanza"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\nabc\nAAA\n"
            + b"--- "
            + b"A" * 43
            + b"\n"
        )


def test_mac_must_be_32_bytes() -> None:
    with pytest.raises(AgeCryptoError, match="MAC line is malformed"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\nAAAA\n--- aaaa\n"
        )


def test_exact_multiple_body_gains_an_empty_final_line() -> None:
    """A 48-byte stanza body folds to one 64-char line — the format
    then requires the empty short line (the reference parser's
    rule our writer mirrors; the ed25519 stanza body is 32 bytes,
    so this case takes a crafted one)."""
    stanza = agecrypto.Stanza(kind="ssh-ed25519", args=["fp"], body=b"A" * 48)
    assert stanza_lines(stanza)[-1] == ""
    blob = build_header(b"\x02" * 16, [stanza])
    stanzas, _mac, _inp, _rest = parse_header(blob)
    assert stanzas[0].body == b"A" * 48


# --- the payload ----------------------------------------------------


def test_payload_tamper_is_named() -> None:
    blob = bytearray(encrypt(b"the value", ED_PUB))
    blob[-1] ^= 0x01
    with pytest.raises(AgeCryptoError, match="payload failed"):
        decrypt(bytes(blob), ED_PEM, no_prompt)


def test_truncated_payload_at_chunk_boundary_fails() -> None:
    plain = b"y" * (agecrypto.CHUNK_BYTES + 100)
    key = hkdf_key(b"\x03" * 16, b"\x04" * 16, agecrypto.PAYLOAD_INFO)
    payload = stream_encrypt(key, plain)
    with pytest.raises(AgeCryptoError, match="payload failed"):
        stream_decrypt(key, payload[: agecrypto.CHUNK_BYTES + 16])


def test_payload_without_nonce_is_named() -> None:
    header = build_header(
        b"\x05" * 16, [wrap_key(recipient(ED_PUB), b"\x05" * 16)]
    )
    with pytest.raises(AgeCryptoError, match="nonce"):
        decrypt(header, ED_PEM, no_prompt)


# --- wire helpers and the moved bech32 ------------------------------


def test_wire_strings_refuses_truncation() -> None:
    with pytest.raises(AgeCryptoError, match="truncated"):
        wire_strings(b"\x00\x00\x00\x10abc")
    with pytest.raises(AgeCryptoError, match="truncated"):
        wire_strings(b"\x00")


def test_public_key_parts_needs_two_fields() -> None:
    with pytest.raises(AgeCryptoError, match="an algorithm and body"):
        agecrypto.public_key_parts("ssh-ed25519")


def test_mpint_zero_and_high_bit() -> None:
    assert mpint(0) == b"\x00\x00\x00\x01\x00"
    assert mpint(0x80) == b"\x00\x00\x00\x02\x00\x80"
    assert mpint(0x7F) == b"\x00\x00\x00\x01\x7f"


def test_b64d_refuses_bad_alphabet() -> None:
    with pytest.raises(AgeCryptoError, match="base64"):
        b64d("not*base64")


def test_mint_age_identity_keeps_the_keygen_shape() -> None:
    body = mint_age_identity()
    lines = body.splitlines()
    assert lines[0].startswith("# public key: age1")
    assert lines[1].startswith("AGE-SECRET-KEY-1")
    assert set(lines[1]) <= set(agecrypto.BECH32_CHARSET.upper() + "1-")


def test_aead_round_trip_and_miss() -> None:
    sealed = aead_seal(b"\x09" * 32, b"secret")
    assert aead_open(b"\x09" * 32, sealed) == b"secret"
    with pytest.raises(NotForThisKey):
        aead_open(b"\x0a" * 32, sealed)


def test_header_line_without_newline_is_refused() -> None:
    with pytest.raises(AgeCryptoError, match="no MAC line"):
        parse_header(b"age-encryption.org/v1")


def test_mac_line_with_bad_base64_is_malformed() -> None:
    with pytest.raises(AgeCryptoError, match="MAC line is malformed"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\nAAAA\n--- !!!\n"
        )


def test_stanza_line_without_a_type_is_refused() -> None:
    with pytest.raises(AgeCryptoError, match="no type"):
        parse_header(b"age-encryption.org/v1\n-> \nAAAA\n--- aaaa\n")


def test_body_line_before_any_stanza_is_refused() -> None:
    with pytest.raises(AgeCryptoError, match="outside any stanza"):
        parse_header(b"age-encryption.org/v1\nAAAA\n--- aaaa\n")


def test_classic_pem_rsa_key_loads() -> None:
    """The PEM fallback in the loader: a traditional RSA key file
    (the pre-OpenSSH-container form ssh still accepts)."""
    key = rsa_mod.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    line = rsa_line_for(key)
    assert decrypt(encrypt(b"old school\n", line), pem, no_prompt) == (
        b"old school\n"
    )


def test_reference_fixture_exact_chunk_boundary() -> None:
    """A 64 KiB payload — one full chunk, the exact-multiple edge
    the reference reader takes through its final-flag retry."""
    blob = (FIXTURES / "exact64k.age").read_bytes()
    plain = decrypt(blob, ED_PEM, no_prompt)
    assert plain == b"x" * agecrypto.CHUNK_BYTES


def test_reference_fixture_two_recipients() -> None:
    """One reference file wrapped to two keys: either identity
    unwraps it (the format's multi-recipient shape — msks writes
    one stanza per file, but must read what the tool writes)."""
    blob = (FIXTURES / "multi.age").read_bytes()
    assert decrypt(blob, ED_PEM, no_prompt) == PLAIN
    assert decrypt(blob, RSA_PEM, no_prompt) == PLAIN


def test_reference_fixture_empty_payload() -> None:
    """The reference writer's empty plaintext: one empty final
    chunk — the shape our writer now mirrors (the review's
    blocking find, pinned from the tool's side)."""
    blob = (FIXTURES / "empty.age").read_bytes()
    assert decrypt(blob, ED_PEM, no_prompt) == b""


@pytest.mark.skipif(AGE is None, reason="the age binary is absent")
def test_live_tool_decrypts_our_empty_file(tmp_path: Path) -> None:
    out = tmp_path / "ours-empty.age"
    out.write_bytes(encrypt(b"", ED_PUB))
    seen = subprocess.run(
        [AGE, "-d", "-i", str(FIXTURES / "ed25519"), str(out)],
        capture_output=True,
        check=True,
    )
    assert seen.stdout == b""


def test_stream_without_any_chunk_is_refused() -> None:
    key = hkdf_key(b"\x0b" * 16, b"\x0c" * 16, agecrypto.PAYLOAD_INFO)
    with pytest.raises(AgeCryptoError, match="without a final chunk"):
        stream_decrypt(key, b"")


def test_parser_caps_match_the_reference() -> None:
    too_many_args = (
        b"age-encryption.org/v1\n-> t "
        + b" ".join([b"a"] * 129)
        + b"\nAAAA\n--- "
        + b"A" * 43
        + b"\n"
    )
    with pytest.raises(AgeCryptoError, match="too many arguments"):
        parse_header(too_many_args)
    too_many_stanzas = (
        b"age-encryption.org/v1\n"
        + b"-> t a\nAAAA\n" * 1025
        + b"--- "
        + b"A" * 43
        + b"\n"
    )
    with pytest.raises(AgeCryptoError, match="too many stanzas"):
        parse_header(too_many_stanzas)
    oversized = (
        b"age-encryption.org/v1\n-> t "
        + b"a" * agecrypto.MAX_HEADER_BYTES
        + b"\nAAAA\n--- "
        + b"A" * 43
        + b"\n"
    )
    with pytest.raises(AgeCryptoError, match="limit"):
        parse_header(oversized)


def test_non_ascii_lines_are_named_errors() -> None:
    with pytest.raises(AgeCryptoError, match="not base64"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\n"
            + b"\xff\xfe\n"
            + b"--- "
            + b"A" * 43
            + b"\n"
        )
    with pytest.raises(AgeCryptoError, match="MAC line is malformed"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\nAAAA\n--- "
            + b"\xff" * 43
            + b"\n"
        )


def test_non_ascii_stanza_args_are_named() -> None:
    with pytest.raises(AgeCryptoError, match="not ASCII"):
        parse_header(
            b"age-encryption.org/v1\n-> t \xff\xfe\nAAAA\n--- "
            + b"A" * 43
            + b"\n"
        )


def test_mac_line_shape_is_exact() -> None:
    with pytest.raises(AgeCryptoError, match="MAC line is malformed"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\nAAAA\n---"
            + b"A" * 43
            + b"\n"
        )  # no space after the mark
    with pytest.raises(AgeCryptoError, match="MAC line is malformed"):
        parse_header(
            b"age-encryption.org/v1\n-> ssh-rsa abcd\nAAAA\n--- "
            + b"A" * 42
            + b"\n"
        )  # one short
