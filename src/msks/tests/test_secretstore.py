"""Secret store tests (#198, #514): the row vocabulary, the blob
helpers over the age crypto leaf, and the settings' refusals for
everything the blob store removed.
"""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from msks.agecrypto import (
    BECH32_CHARSET,
    convertbits,
    encode_bech32,
    mint_age_identity,
)
from msks.app import build_app
from msks.secretcache import NotEncryptableError
from msks.secretstore import (
    SecretStore,
    backend_ref,
    blob_recipients,
    eligible_recipients,
    new_sentinel,
    recipient_hashes,
    valid_name,
)
from msks.settings import Settings
from testkeys import mint as mint_key


def store_for(tmp_path) -> SecretStore:
    """A store (the cache owner) under *tmp_path*."""
    return build_app(
        Settings.from_env({"MSKSD_STATE_DIR": str(tmp_path)})
    ).state.secrets


def decode_bech32(text: str) -> tuple[str, list[int], list[int]]:
    """A bech32 string split into (hrp, data words, checksum
    words) — the verifier's half of the codec, for pinning the
    encoder against vectors the real age tooling minted."""
    lowered = text.lower()
    pos = lowered.rfind("1")
    values = [BECH32_CHARSET.index(c) for c in lowered[pos + 1 :]]
    return lowered[:pos], values[:-6], values[-6:]


def words_to_bytes(words: list[int]) -> bytes:
    """5-bit words regrouped into bytes, dropping the tail pad."""
    acc = 0
    bits = 0
    out = bytearray()
    for word in words:
        acc = (acc << 5) | word
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def test_new_sentinel_shape() -> None:
    """The sentinel is the versioned prefix + 43 base64url chars —
    uniform fixed length, so the wire matcher can recognize it
    without parsing. The daemon-wide row mints under its own
    prefix (#339), so the string alone names its reach."""
    sentinel = new_sentinel()
    assert sentinel.startswith("mskssec1_")
    assert len(sentinel) == len("mskssec1_") + 43
    assert new_sentinel() != sentinel
    daemon = new_sentinel(daemon_wide=True)
    assert daemon.startswith("mskssec2_")
    assert len(daemon) == len("mskssec2_") + 43


def test_backend_ref_sanitizes() -> None:
    """Dots and dashes become underscores, uppercased, MSKSWS-prefixed;
    a daemon-wide coverage carries its own ref family (#339)."""
    assert backend_ref(["my-ws"], "github_api") == "MSKSWS_MY_WS_GITHUB_API"
    assert backend_ref(["a.b"], "c.d") == "MSKSWS_A_B_C_D"
    assert backend_ref([], "github_api") == "MSKSDAEMON_GITHUB_API"


def test_backend_ref_joins_a_scoped_coverage_in_sorted_order() -> None:
    """A multi-workspace coverage is one ref: the sorted ids in
    order, then the label (#339) — the same spelling the same set
    mints anywhere, so the cache keys one entry per row."""
    assert backend_ref(["b-ws", "a-ws"], "token") == "MSKSWS_A_WS_B_WS_TOKEN"


def test_valid_name() -> None:
    """Labels follow the identifier rule the mint enforces."""
    assert valid_name("github_api")
    assert valid_name("_x")
    assert not valid_name("1abc")
    assert not valid_name("has-dash")


def test_convertbits_regroups_with_and_without_tail_pad() -> None:
    """8-bit bytes into 5-bit words: a length divisible by 5 bits'
    word size carries no pad word (5 bytes = exactly 8 words), a
    longer one pads the tail (32 bytes = 51 words + the pad)."""
    assert convertbits(b"12345", 8, 5) == convertbits(b"12345", 8, 5)
    assert len(convertbits(b"12345", 8, 5)) == 8
    assert len(convertbits(bytes(32), 8, 5)) == 52


def test_minted_identity_matches_the_age_tooling() -> None:
    """The minted identity is the age tooling's own shape: a
    scalar pinned against a real ``age-keygen`` output encodes to
    the same identity string, and the recipient on the comment
    line is the scalar's X25519 public half."""
    # Pinned from `age-keygen`: this scalar is that identity.
    scalar = bytes.fromhex(
        "9018ec3804901996535f3820bf8090d3c1ada24306ee6f8b479ad946df8fc8c4"
    )
    pinned = (
        "AGE-SECRET-KEY-1JQVWCWQYJQVEV56L8QSTLQYS60Q6MGJRQMHXLZ68NTV5DHU0"
        "ERZQWKV3K4"
    )
    assert encode_bech32("age-secret-key-", scalar).upper() == pinned
    body = mint_age_identity()
    comment, identity = body.splitlines()
    assert comment.startswith("# public key: age1")
    assert identity.startswith("AGE-SECRET-KEY-1")
    hrp, words, _checksum = decode_bech32(identity)
    assert hrp == "age-secret-key-"
    minted_scalar = words_to_bytes(words)
    assert len(minted_scalar) == 32
    private = x25519.X25519PrivateKey.from_private_bytes(minted_scalar)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert comment == f"# public key: {encode_bech32('age', public)}"


def test_blob_recipients_encrypts_one_file_per_key() -> None:
    """The blob mint (#514): one age file per recipient, each
    decryptable by that workspace identity's private half alone,
    each carrying the key line's staleness hash."""
    _a_pem, a_public = mint_key("ed25519")
    _b_pem, b_public = mint_key("rsa")
    blobs = blob_recipients(b"the value", {"ws-a": a_public, "ws-b": b_public})
    assert set(blobs) == {"ws-a", "ws-b"}
    a_hash, a_blob = blobs["ws-a"]
    assert a_hash == recipient_hashes({"ws-a": a_public})["ws-a"]
    _b_pem, _ = mint_key("rsa")
    _b_hash, b_blob = blobs["ws-b"]
    assert a_blob != b_blob  # fresh file keys per recipient
    from msks.agecrypto import load_identity

    identity = load_identity(_a_pem.encode(), lambda: b"")
    from msks.agecrypto import parse_header

    stanzas, _mac, _mac_input, payload = parse_header(a_blob)
    file_key = identity.unwrap(stanzas[0])
    from msks.agecrypto import PAYLOAD_INFO, hkdf_key, stream_decrypt

    plaintext = stream_decrypt(
        hkdf_key(file_key, payload[:16], PAYLOAD_INFO), payload[16:]
    )
    assert plaintext == b"the value"


def test_blob_recipients_refuses_an_incapable_key_by_name() -> None:
    """A key outside the leaf's capable set refuses the mint with
    the workspace and the constraint named (#514)."""
    _pem, ecdsa_public = mint_key("ecdsa")
    with pytest.raises(NotEncryptableError) as raised:
        blob_recipients(b"v", {"ws-x": ecdsa_public})
    assert raised.value.workspace_id == "ws-x"
    assert "ecdsa-sha2-nistp256" in str(raised.value)
    assert "ssh-ed25519 or ssh-rsa" in str(raised.value)


def test_eligible_recipients_filters() -> None:
    """The unlock's tolerant filter: incapable keys drop out, the
    rest keep their order and identity."""
    _e_pem, ed_public = mint_key("ed25519")
    _c_pem, ecdsa_public = mint_key("ecdsa")
    recipients = {"ws-ed": ed_public, "ws-ec": ecdsa_public}
    assert eligible_recipients(recipients) == {"ws-ed": ed_public}


def test_recipient_hashes_track_the_key_line() -> None:
    """The staleness hash is the key line's SHA-256 — a rotated
    key mints a different token (#513)."""
    _pem, public = mint_key("ed25519")
    _pem2, rotated = mint_key("ed25519")
    hashes = recipient_hashes({"ws": public})
    assert hashes == recipient_hashes({"ws": public})
    assert hashes != recipient_hashes({"ws": rotated})


def test_the_store_owns_the_unlock_cache(tmp_path) -> None:
    """The store object's one job (#514): owning the plaintext
    cache every surface reaches as ``secrets.plain``."""
    store = store_for(tmp_path)
    store.plain.store({"MSKSWS_WS_X_TOKEN": "value"})
    assert store.plain.read("MSKSWS_WS_X_TOKEN") == "value"
    store.cache_clear()
    with pytest.raises(Exception):
        store.plain.read("MSKSWS_WS_X_TOKEN")


def test_settings_refuse_every_removed_store_key() -> None:
    """#423 removed the provider family and #514 removed the
    agefile family with it: each one still set is named at load,
    so a stale config fails loudly instead of silently ignoring
    half a vault setup."""
    for name in (
        "MSKSD_SECRET_STORE_PROVIDER",
        "MSKSD_SECRET_STORE_REGION",
        "MSKSD_SECRET_STORE_PROFILE",
        "MSKSD_SECRET_STORE_PREFIX",
        "MSKSD_SECRET_STORE_PROJECT",
        "MSKSD_SECRET_STORE_ROOT",
        "MSKSD_SECRET_STORE_AGE_IDENTITY",
        "MSKSD_SECRET_STORE_CLI",
        "MSKSD_SECRET_STORE_TIMEOUT_S",
    ):
        with pytest.raises(ValueError, match=name):
            Settings.from_env({name: "whatever"})


def test_settings_unlock_timeout_is_zero_or_positive() -> None:
    """#514: the unlock window accepts zero (drop on the operator's
    disconnect) and any positive span, and refuses a negative."""
    assert (
        Settings.from_env(
            {"MSKSD_SECRET_STORE_UNLOCK_TIMEOUT_S": "0"}
        ).secret_store.unlock_timeout_s
        == 0.0
    )
    assert (
        Settings.from_env(
            {"MSKSD_SECRET_STORE_UNLOCK_TIMEOUT_S": "60"}
        ).secret_store.unlock_timeout_s
        == 60.0
    )
    assert Settings.from_env({}).secret_store.unlock_timeout_s == 86400.0
    with pytest.raises(
        ValueError, match="MSKSD_SECRET_STORE_UNLOCK_TIMEOUT_S"
    ):
        Settings.from_env({"MSKSD_SECRET_STORE_UNLOCK_TIMEOUT_S": "-1"})
