"""Secret store tests (#198, #423): manifest, the agefile vault,
and the real CLI.

The subprocess round-trips run the real ``secretspec`` binary from
the devenv shell against the ``age`` provider in a tmp root — the
same integration the daemon ships, with a daemon-minted identity —
so these tests assert what the CLI actually does (stderr shape,
stdin value handling, the newline ``get`` appends on 0.20), not
what a stub would echo back.
"""

import re
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from msks.app import build_app
from msks.secretstore import (
    AGE_IDENTITY_NAME,
    AGEFILE_NAME,
    BECH32_CHARSET,
    MANIFEST_NAME,
    SecretStore,
    SecretStoreError,
    backend_ref,
    convertbits,
    encode_bech32,
    ensure_age_identity,
    mint_age_identity,
    new_sentinel,
    provider_uri,
    render_manifest,
    valid_name,
)
from msks.settings import Settings


def store_for(tmp_path, env=None) -> SecretStore:
    """A store on the age provider under *tmp_path*."""
    variables = {
        "MSKSD_STATE_DIR": str(tmp_path),
        "MSKSD_SECRET_STORE_ROOT": str(tmp_path / "store"),
    }
    variables.update(env or {})
    app = build_app(Settings.from_env(variables))
    return app.state.secrets


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
    mints anywhere, so the store sees one entry per row."""
    assert backend_ref(["b-ws", "a-ws"], "token") == "MSKSWS_A_WS_B_WS_TOKEN"


def test_valid_name() -> None:
    """Labels follow SecretSpec's identifier rule."""
    assert valid_name("github_api")
    assert valid_name("_x")
    assert not valid_name("1abc")
    assert not valid_name("has-dash")


def test_provider_uri_is_the_agefile(tmp_path) -> None:
    """One provider, one URI: the agefile under the store root with
    the identity beside it. The derived identity path lands at
    <root>/age.key; an explicit setting wins; and the URI call is
    what mints the identity when it is absent — 0600, and never
    rewritten on a second call."""
    store = store_for(tmp_path)
    assert (
        provider_uri(store) == f"age://{tmp_path / 'store' / AGEFILE_NAME}"
        f"?identity={tmp_path / 'store' / AGE_IDENTITY_NAME}"
    )
    derived = tmp_path / "store" / AGE_IDENTITY_NAME
    assert derived.exists()
    assert stat.S_IMODE(derived.stat().st_mode) & 0o077 == 0
    body = derived.read_text()
    assert provider_uri(store)  # idempotent: the file stands
    assert derived.read_text() == body
    explicit = tmp_path / "elsewhere.key"
    store = store_for(
        tmp_path,
        {"MSKSD_SECRET_STORE_AGE_IDENTITY": str(explicit)},
    )
    assert (
        provider_uri(store) == f"age://{tmp_path / 'store' / AGEFILE_NAME}"
        f"?identity={explicit}"
    )
    assert explicit.exists()


def test_convertbits_regroups_with_and_without_tail_pad() -> None:
    """8-bit bytes into 5-bit words: a length divisible by 5 bits'
    word size carries no pad word (5 bytes = exactly 8 words), a
    longer one pads the tail (32 bytes = 51 words + the pad)."""
    assert convertbits(b"12345", 8, 5) == convertbits(b"12345", 8, 5)
    assert len(convertbits(b"12345", 8, 5)) == 8
    assert len(convertbits(bytes(32), 8, 5)) == 52


def test_minted_identity_matches_the_age_tooling(tmp_path) -> None:
    """The daemon-minted identity is the age tooling's own shape:
    a scalar pinned against a real ``age-keygen`` output encodes
    to the same identity string, and the recipient on the comment
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


def test_ensure_age_identity_mints_once(tmp_path, caplog) -> None:
    """Absent, the identity is minted (0600, parent 0700) and the
    mint names itself in the log — a repointed path or a moved
    vault stays diagnosable; present, it is left untouched — an
    operator's own age-keygen identity and a re-run against an
    existing vault both keep what stood."""
    import logging

    path = tmp_path / "vault" / "age.key"
    with caplog.at_level(logging.WARNING):
        ensure_age_identity(path)
    body = path.read_text()
    assert "minted a new age identity" in caplog.text
    assert str(path) in caplog.text
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0
    ensure_age_identity(path)
    assert path.read_text() == body
    hand_minted = tmp_path / "operator.key"
    hand_minted.write_text("AGE-SECRET-KEY-1OPERATOR\n")
    ensure_age_identity(hand_minted)
    assert hand_minted.read_text() == "AGE-SECRET-KEY-1OPERATOR\n"


def test_manifest_modes(tmp_path) -> None:
    """The root is 0700 and the manifest 0600 — the agefile and
    the age identity live beside them in the same root."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    root = tmp_path / "store"
    manifest = root / MANIFEST_NAME
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_render_manifest_declarations() -> None:
    """One inline declaration per ref, sorted, against the alias."""
    body = render_manifest(
        "age:/store/secrets.age?identity=/store/age.key",
        [("MSKSWS_B_X", "b/x"), ("MSKSWS_A_Y", "a/y")],
    )
    assert 'name = "msks"' in body
    assert 'store = "age:/store/secrets.age?identity=/store/age.key"' in body
    assert body.index("MSKSWS_A_Y") < body.index("MSKSWS_B_X")
    assert 'providers = ["store"]' in body


def test_manifest_sync_is_change_driven(tmp_path) -> None:
    """An unchanged body is not rewritten (mtime holds)."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    manifest = tmp_path / "store" / MANIFEST_NAME
    first_mtime = manifest.stat().st_mtime_ns
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    assert manifest.stat().st_mtime_ns == first_mtime
    store.sync_manifest([("MSKSWS_WS_X", "ws/x"), ("MSKSWS_WS_Y", "ws/y")])
    assert "MSKSWS_WS_Y" in manifest.read_text()


async def test_write_read_delete_roundtrip(tmp_path) -> None:
    """The daemon's three operations against the real CLI: the
    value lands in — and leaves — the encrypted agefile, read back
    through the same provider the daemon ships."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_WS_GITHUB", "ws/github")])
    await store.write("MSKSWS_WS_GITHUB", "ghp-real-token-123")
    agefile = tmp_path / "store" / AGEFILE_NAME
    assert agefile.exists()
    assert b"ghp-real-token-123" not in agefile.read_bytes()
    assert await store.read("MSKSWS_WS_GITHUB") == "ghp-real-token-123"
    await store.delete("MSKSWS_WS_GITHUB")
    with pytest.raises(SecretStoreError):
        await store.read("MSKSWS_WS_GITHUB")


async def test_read_survives_a_cold_cache(tmp_path) -> None:
    """A restarted daemon re-fetches by backend ref: a second store
    object (empty cache) reads what the first one wrote."""
    first = store_for(tmp_path)
    first.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    await first.write("MSKSWS_WS_X", "value-1")
    second = store_for(tmp_path)
    assert await second.read("MSKSWS_WS_X") == "value-1"


async def test_read_uses_the_cache(tmp_path) -> None:
    """After the first fetch the value comes from memory — the
    agefile can disappear and the swap path still answers."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    await store.write("MSKSWS_WS_X", "value-1")
    (tmp_path / "store" / AGEFILE_NAME).unlink()
    assert await store.read("MSKSWS_WS_X") == "value-1"


async def test_check_probes_and_cleans(tmp_path) -> None:
    """check() writes, reads back, and removes its probe — the store
    root holds no probe residue after a green run."""
    store = store_for(tmp_path)
    result = await store.check()
    assert result == {"provider": "age", "ok": True}
    assert not (tmp_path / "store" / "probe.toml").exists()
    assert (tmp_path / "store" / AGEFILE_NAME).exists()


async def test_missing_binary_is_a_named_error(tmp_path) -> None:
    """A wrong secret_store_cli fails with the CLI's stderr tail,
    not a bare FileNotFoundError."""
    store = store_for(
        tmp_path, {"MSKSD_SECRET_STORE_CLI": "/nonexistent/secretspec"}
    )
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError):
        await store.write("MSKSWS_WS_X", "v")


def test_settings_refuse_removed_provider_keys() -> None:
    """#423 removed the provider switch and its connection details:
    each one still set is named at load, so a stale config fails
    loudly instead of silently ignoring half a vault setup."""
    for name in (
        "MSKSD_SECRET_STORE_PROVIDER",
        "MSKSD_SECRET_STORE_REGION",
        "MSKSD_SECRET_STORE_PROFILE",
        "MSKSD_SECRET_STORE_PREFIX",
        "MSKSD_SECRET_STORE_PROJECT",
    ):
        with pytest.raises(ValueError, match=name):
            Settings.from_env({name: "whatever"})


def test_settings_root_derives_from_state_dir(tmp_path) -> None:
    settings = Settings.from_env({"MSKSD_STATE_DIR": str(tmp_path)})
    assert settings.secret_store.root == tmp_path / "secrets"
    assert settings.secret_store.age_identity is None
    assert settings.secret_store.cli == "secretspec"


def test_settings_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="MSKSD_SECRET_STORE_TIMEOUT_S"):
        Settings.from_env({"MSKSD_SECRET_STORE_TIMEOUT_S": "0"})


def test_cli_env_override(tmp_path, monkeypatch) -> None:
    """MSKSD_SECRET_STORE_CLI names the binary, house tool pattern."""
    settings = Settings.from_env(
        {"MSKSD_SECRET_STORE_CLI": "/opt/secretspec/bin/secretspec"}
    )
    assert settings.secret_store.cli == "/opt/secretspec/bin/secretspec"


async def test_operation_times_out(tmp_path) -> None:
    """A hung CLI answers the timeout, not a hung request."""
    slow = tmp_path / "slow-cli"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    store = store_for(
        tmp_path,
        {
            "MSKSD_SECRET_STORE_CLI": str(slow),
            "MSKSD_SECRET_STORE_TIMEOUT_S": "0.2",
        },
    )
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError, match="timed out"):
        await store.write("MSKSWS_WS_X", "v")


async def test_check_detects_a_lying_read(tmp_path, monkeypatch) -> None:
    """A provider that returns the wrong bytes is a failed check."""

    async def fake_run(args, stdin=None, manifest=None):
        if args[0] == "get":
            return b"not-the-probe\n"
        return b""

    store = store_for(tmp_path)
    monkeypatch.setattr(store, "run", fake_run)
    with pytest.raises(SecretStoreError, match="probe value mismatch"):
        await store.check()


async def test_a_failing_command_carries_stderr(tmp_path) -> None:
    """A non-zero exit from a runnable binary surfaces the stderr's
    last line (the empty-stderr form says "unknown error")."""
    failing = tmp_path / "failing-cli"
    failing.write_text(
        "#!/bin/sh\necho first line >&2\n echo the real error >&2\nexit 1\n"
    )
    failing.chmod(0o755)
    store = store_for(tmp_path, {"MSKSD_SECRET_STORE_CLI": str(failing)})
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError, match="the real error"):
        await store.write("MSKSWS_WS_X", "v")

    silent = tmp_path / "silent-cli"
    silent.write_text("#!/bin/sh\nexit 3\n")
    silent.chmod(0o755)
    store = store_for(tmp_path, {"MSKSD_SECRET_STORE_CLI": str(silent)})
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError, match="unknown error"):
        await store.read("MSKSWS_WS_X")


async def test_check_never_touches_a_real_placeholder(tmp_path) -> None:
    """The probe ref is lowercase, unreachable by construction: a
    workspace literally named `store` with a placeholder `probe`
    keeps its value across a health check."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_STORE_PROBE", "store/probe")])
    await store.write("MSKSWS_STORE_PROBE", "THE-REAL-SECRET")
    assert await store.check() == {"provider": "age", "ok": True}
    assert await store.read("MSKSWS_STORE_PROBE") == "THE-REAL-SECRET"


# --- the #335 legacy ref migration ----------------------------------


def app_for(tmp_path):
    """A built app with migrated schema; placeholders insertable."""
    app = build_app(
        Settings.from_env(
            {
                "MSKSD_STATE_DIR": str(tmp_path),
                "MSKSD_SECRET_STORE_ROOT": str(tmp_path / "store"),
            }
        )
    )
    app.state.model.migrate()
    return app


async def seed_legacy_placeholder(app, workspace_id, name, value=None):
    """One placeholder row as pre-#335 code minted it, with an
    optional stored value under the legacy ref."""
    ref = "MSKS_" + "_".join(
        re.sub(r"[^A-Za-z0-9_]", "_", part).upper()
        for part in (workspace_id, name)
    )
    await app.state.model.create_placeholder(
        workspaces=[workspace_id],
        name=name,
        sentinel=new_sentinel(),
        dests=["github.com"],
        backend_ref=ref,
    )
    if value is not None:
        app.state.secrets.sync_manifest([(ref, f"{workspace_id}/{name}")])
        await app.state.secrets.write(ref, value)
    return ref


async def test_legacy_refs_migrate_rows_values_and_manifest(tmp_path) -> None:
    """One startup pass moves row, stored value, and manifest onto
    the MSKSWS_ ref — the pre-#335 stored format, migrated (#335)."""
    app = app_for(tmp_path)
    await seed_legacy_placeholder(app, "ws-a", "github_api", "ghp-old")
    await seed_legacy_placeholder(app, "my-ws", "x", "v2")

    moved = await app.state.secrets.migrate_legacy_refs()

    assert moved == 2
    refs = {
        row["backend_ref"] for row in await app.state.model.list_placeholders()
    }
    assert refs == {"MSKSWS_WS_A_GITHUB_API", "MSKSWS_MY_WS_X"}
    # A restarted daemon (cold cache) reads the values at the new
    # refs; the legacy entries are gone from the provider.
    fresh = app_for(tmp_path).state.secrets
    assert await fresh.read("MSKSWS_WS_A_GITHUB_API") == "ghp-old"
    assert await fresh.read("MSKSWS_MY_WS_X") == "v2"
    for legacy in ("MSKS_WS_A_GITHUB_API", "MSKS_MY_WS_X"):
        with pytest.raises(SecretStoreError):
            await fresh.read(legacy)
    manifest = (tmp_path / "store" / MANIFEST_NAME).read_text()
    assert "MSKSWS_WS_A_GITHUB_API" in manifest
    assert "MSKS_" not in manifest


async def test_legacy_ref_migration_is_idempotent(tmp_path) -> None:
    """A pass interrupted and re-run leaves nothing half-done: the
    second pass sees no legacy rows and changes nothing."""
    app = app_for(tmp_path)
    await seed_legacy_placeholder(app, "ws-a", "github_api", "ghp-old")

    assert await app.state.secrets.migrate_legacy_refs() == 1
    body = (tmp_path / "store" / MANIFEST_NAME).read_text()

    assert await app.state.secrets.migrate_legacy_refs() == 0
    assert (tmp_path / "store" / MANIFEST_NAME).read_text() == body
    fresh = app_for(tmp_path).state.secrets
    assert await fresh.read("MSKSWS_WS_A_GITHUB_API") == "ghp-old"


async def test_unreadable_legacy_value_leaves_the_row_for_retry(
    tmp_path, caplog
) -> None:
    """A value the store cannot answer — a transient outage as
    much as a moved store, indistinguishable at the CLI — keeps
    the row on its legacy ref; renaming it would strand the value
    behind a ref nothing reads again (#335)."""
    app = app_for(tmp_path)
    await seed_legacy_placeholder(app, "ws-a", "github_api", value=None)

    with caplog.at_level("WARNING"):
        moved = await app.state.secrets.migrate_legacy_refs()

    assert moved == 0
    (row,) = await app.state.model.list_placeholders()
    assert row["backend_ref"] == "MSKS_WS_A_GITHUB_API"
    assert "no readable value" in caplog.text


async def test_stale_legacy_manifest_heals_without_legacy_rows(
    tmp_path,
) -> None:
    """A pass that renamed every row but died before its final
    re-sync leaves a manifest declaring the legacy ref; the next
    boot re-renders it even with no legacy rows left (#335)."""
    app = app_for(tmp_path)
    await app.state.model.create_placeholder(
        workspaces=["ws-a"],
        name="github_api",
        sentinel=new_sentinel(),
        dests=["github.com"],
        backend_ref="MSKSWS_WS_A_GITHUB_API",
    )
    stale = tmp_path / "store" / MANIFEST_NAME
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        render_manifest(
            "file:x", [("MSKS_WS_A_GITHUB_API", "ws-a/github_api")]
        )
    )

    assert await app.state.secrets.migrate_legacy_refs() == 0

    assert "MSKSWS_WS_A_GITHUB_API" in stale.read_text()
    assert "MSKS_WS_A_GITHUB_API" not in stale.read_text()


async def test_no_store_configured_migrates_nothing() -> None:
    """A settings object without a store root (the direct
    construction some test daemons use) has nothing to read or
    rewrite — the pass is a no-op, not a crash."""
    app = build_app(Settings())
    assert app.state.secrets.manifest_declares_legacy() is False
    assert await app.state.secrets.migrate_legacy_refs() == 0


async def test_failing_row_rename_leaves_the_row_legacy(tmp_path, monkeypatch):
    """A copy that fails mid-flight logs and leaves the row on its
    legacy ref — reads keep following the row's ref, and the next
    startup retries the move."""
    app = app_for(tmp_path)
    ref = await seed_legacy_placeholder(app, "ws-a", "github_api", "ghp-old")

    async def boom(placeholder_id, new_ref):
        raise RuntimeError("db gone")

    monkeypatch.setattr(app.state.model, "rename_placeholder_ref", boom)

    moved = await app.state.secrets.migrate_legacy_refs()

    assert moved == 0
    (row,) = await app.state.model.list_placeholders()
    assert row["backend_ref"] == ref


async def test_write_failure_retries_on_the_next_pass(
    tmp_path, monkeypatch
) -> None:
    """A pass whose copy step fails (here: the value is readable —
    simulated, the write is real and failing) leaves both the row
    and the old value intact; a later healthy pass completes the
    move — the resume story the ordering exists for (#335)."""
    app = app_for(tmp_path)
    await seed_legacy_placeholder(app, "ws-a", "github_api", "ghp-old")
    failing = tmp_path / "failing-cli"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    app.state.settings.secret_store.cli = str(failing)

    async def readable(ref):
        return "ghp-old"

    monkeypatch.setattr(app.state.secrets, "read", readable)
    assert await app.state.secrets.migrate_legacy_refs() == 0
    (row,) = await app.state.model.list_placeholders()
    assert row["backend_ref"] == "MSKS_WS_A_GITHUB_API"

    healthy = app_for(tmp_path)
    assert await healthy.state.secrets.migrate_legacy_refs() == 1
    (row,) = await healthy.state.model.list_placeholders()
    assert row["backend_ref"] == "MSKSWS_WS_A_GITHUB_API"
    fresh = app_for(tmp_path).state.secrets
    assert await fresh.read("MSKSWS_WS_A_GITHUB_API") == "ghp-old"
    stored = tmp_path / "store" / "msks" / "default"
    assert not (stored / "MSKS_WS_A_GITHUB_API").exists()


async def test_delete_failure_orphans_the_old_value_loudly(
    tmp_path, monkeypatch, caplog
) -> None:
    """The old value drops only after the row repoints, so a failed
    drop costs an inert orphan — named in the log — never the
    placeholder (#335)."""
    app = app_for(tmp_path)
    await seed_legacy_placeholder(app, "ws-a", "github_api", "ghp-old")

    async def refuse_delete(ref):
        raise SecretStoreError("delete", "refused")

    monkeypatch.setattr(app.state.secrets, "delete", refuse_delete)
    with caplog.at_level("WARNING"):
        moved = await app.state.secrets.migrate_legacy_refs()

    assert moved == 1
    (row,) = await app.state.model.list_placeholders()
    assert row["backend_ref"] == "MSKSWS_WS_A_GITHUB_API"
    fresh = app_for(tmp_path).state.secrets
    assert await fresh.read("MSKSWS_WS_A_GITHUB_API") == "ghp-old"
    # The orphan is a stored value behind a ref no manifest declares
    # (the re-sync dropped it); declaring it reads it back, proving
    # the copy left the original in the vault.
    fresh.sync_manifest([("MSKS_WS_A_GITHUB_API", "ws-a/github_api")])
    assert await fresh.read("MSKS_WS_A_GITHUB_API") == "ghp-old"
    assert "left behind" in caplog.text


def test_manifest_staleness_matches_keys_not_substrings(tmp_path) -> None:
    """A fully migrated store whose ref *contains* the legacy
    prefix (a workspace named ``msks``) is not stale: the check
    reads declaration keys, not substrings (#335)."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_MSKS_KEY", "msks/key")])

    assert store.manifest_declares_legacy() is False

    store.sync_manifest(
        [("MSKSWS_MSKS_KEY", "msks/key"), ("MSKS_OLD", "msks/old")]
    )

    assert store.manifest_declares_legacy() is True
