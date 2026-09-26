"""Secret store tests (#198): manifest, URIs, and the real CLI.

The subprocess round-trips run the real ``secretspec`` binary from
the devenv shell against the ``file`` provider in a tmp root — the
same integration the daemon ships — so these tests assert what the
CLI actually does (stderr shape, stdin value handling, the newline
``get`` appends on 0.20), not what a stub would echo back.
"""

import re
import stat

import pytest
from msks.app import build_app
from msks.secretstore import (
    MANIFEST_NAME,
    SecretStore,
    SecretStoreError,
    backend_ref,
    new_sentinel,
    provider_uri,
    render_manifest,
    valid_name,
)
from msks.settings import Settings


def store_for(tmp_path, env=None) -> SecretStore:
    """A store on the file provider under *tmp_path*."""
    variables = {
        "MSKSD_STATE_DIR": str(tmp_path),
        "MSKSD_SECRET_STORE_ROOT": str(tmp_path / "store"),
    }
    variables.update(env or {})
    app = build_app(Settings.from_env(variables))
    return app.state.secrets


def test_new_sentinel_shape() -> None:
    """The sentinel is the versioned prefix + 43 base64url chars —
    uniform fixed length, so the wire matcher can recognize it
    without parsing."""
    sentinel = new_sentinel()
    assert sentinel.startswith("mskssec1_")
    assert len(sentinel) == len("mskssec1_") + 43
    assert new_sentinel() != sentinel


def test_backend_ref_sanitizes() -> None:
    """Dots and dashes become underscores, uppercased, MSKSWS-prefixed."""
    assert backend_ref("my-ws", "github_api") == "MSKSWS_MY_WS_GITHUB_API"
    assert backend_ref("a.b", "c.d") == "MSKSWS_A_B_C_D"


def test_valid_name() -> None:
    """Labels follow SecretSpec's identifier rule."""
    assert valid_name("github_api")
    assert valid_name("_x")
    assert not valid_name("1abc")
    assert not valid_name("has-dash")


def test_provider_uris(tmp_path) -> None:
    """Each provider maps to its documented URI form."""
    store = store_for(tmp_path)
    assert provider_uri(store) == f"file:{tmp_path / 'store'}"
    store = store_for(
        tmp_path,
        {
            "MSKSD_SECRET_STORE_PROVIDER": "age",
            "MSKSD_SECRET_STORE_AGE_IDENTITY": "/etc/age.key",
        },
    )
    assert (
        provider_uri(store)
        == f"age://{tmp_path / 'store' / 'secrets.age'}?identity=/etc/age.key"
    )
    store = store_for(
        tmp_path,
        {
            "MSKSD_SECRET_STORE_PROVIDER": "awssm",
            "MSKSD_SECRET_STORE_REGION": "eu-west-1",
        },
    )
    assert provider_uri(store) == "awssm://eu-west-1"
    store = store_for(
        tmp_path,
        {
            "MSKSD_SECRET_STORE_PROVIDER": "awssm",
            "MSKSD_SECRET_STORE_REGION": "eu-west-1",
            "MSKSD_SECRET_STORE_PROFILE": "prod",
            "MSKSD_SECRET_STORE_PREFIX": "team",
        },
    )
    assert provider_uri(store) == "awssm://prod@eu-west-1?prefix=team"
    store = store_for(
        tmp_path,
        {
            "MSKSD_SECRET_STORE_PROVIDER": "bws",
            "MSKSD_SECRET_STORE_PROJECT": "uuid-1",
        },
    )
    assert provider_uri(store) == "bws://uuid-1"


def test_render_manifest_declarations() -> None:
    """One inline declaration per ref, sorted, against the alias."""
    body = render_manifest(
        "file:/store", [("MSKSWS_B_X", "b/x"), ("MSKSWS_A_Y", "a/y")]
    )
    assert 'name = "msks"' in body
    assert 'store = "file:/store"' in body
    assert body.index("MSKSWS_A_Y") < body.index("MSKSWS_B_X")
    assert 'providers = ["store"]' in body


def test_manifest_modes(tmp_path) -> None:
    """The root is 0700 and the manifest 0600 — real secrets' bytes
    live beside them on the file provider."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    root = tmp_path / "store"
    manifest = root / MANIFEST_NAME
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


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
    """The daemon's three operations against the real CLI."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_WS_GITHUB", "ws/github")])
    await store.write("MSKSWS_WS_GITHUB", "ghp-real-token-123")
    stored = tmp_path / "store" / "msks" / "default" / "MSKSWS_WS_GITHUB"
    assert stored.read_text() == "ghp-real-token-123"
    assert stat.S_IMODE(stored.stat().st_mode) & 0o077 == 0
    assert await store.read("MSKSWS_WS_GITHUB") == "ghp-real-token-123"
    await store.delete("MSKSWS_WS_GITHUB")
    assert not stored.exists()


async def test_read_survives_a_cold_cache(tmp_path) -> None:
    """A restarted daemon re-fetches by backend ref: a second store
    object (empty cache) reads what the first one wrote."""
    first = store_for(tmp_path)
    first.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    await first.write("MSKSWS_WS_X", "value-1")
    second = store_for(tmp_path)
    assert await second.read("MSKSWS_WS_X") == "value-1"


async def test_read_uses_the_cache(tmp_path) -> None:
    """After the first fetch the value comes from memory — the file
    can disappear and the swap path still answers."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    await store.write("MSKSWS_WS_X", "value-1")
    (tmp_path / "store" / "msks" / "default" / "MSKSWS_WS_X").unlink()
    assert await store.read("MSKSWS_WS_X") == "value-1"


async def test_check_probes_and_cleans(tmp_path) -> None:
    """check() writes, reads back, and removes its probe — the store
    root holds no probe residue after a green run."""
    store = store_for(tmp_path)
    result = await store.check()
    assert result == {"provider": "file", "ok": True}
    assert not (tmp_path / "store" / "probe.toml").exists()
    stored = tmp_path / "store" / "msks" / "default"
    assert not stored.exists() or not any(stored.iterdir())


async def test_missing_binary_is_a_named_error(tmp_path) -> None:
    """A wrong secret_store_cli fails with the CLI's stderr tail,
    not a bare FileNotFoundError."""
    store = store_for(
        tmp_path, {"MSKSD_SECRET_STORE_CLI": "/nonexistent/secretspec"}
    )
    store.sync_manifest([("MSKSWS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError):
        await store.write("MSKSWS_WS_X", "v")


def test_settings_reject_unknown_provider() -> None:
    with pytest.raises(ValueError, match="MSKSD_SECRET_STORE_PROVIDER"):
        Settings.from_env({"MSKSD_SECRET_STORE_PROVIDER": "vault"})


def test_settings_per_provider_required_keys() -> None:
    """Each provider's required key is named when absent."""
    for provider, name in (
        ("age", "MSKSD_SECRET_STORE_AGE_IDENTITY"),
        ("awssm", "MSKSD_SECRET_STORE_REGION"),
        ("bws", "MSKSD_SECRET_STORE_PROJECT"),
    ):
        with pytest.raises(ValueError, match=name):
            Settings.from_env({"MSKSD_SECRET_STORE_PROVIDER": provider})


def test_settings_root_derives_from_state_dir(tmp_path) -> None:
    settings = Settings.from_env({"MSKSD_STATE_DIR": str(tmp_path)})
    assert settings.secret_store.root == tmp_path / "secrets"
    assert settings.secret_store.provider == "file"
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
    assert await store.check() == {"provider": "file", "ok": True}
    assert await store.read("MSKSWS_STORE_PROBE") == "THE-REAL-SECRET"
    stored = tmp_path / "store" / "msks" / "default" / "MSKSWS_STORE_PROBE"
    assert stored.read_text() == "THE-REAL-SECRET"


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
        workspace_id=workspace_id,
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
    stored = tmp_path / "store" / "msks" / "default"
    assert set(stored.iterdir()) == {
        stored / "MSKSWS_WS_A_GITHUB_API",
        stored / "MSKSWS_MY_WS_X",
    }
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


async def test_missing_legacy_value_still_renames_the_row(
    tmp_path, caplog
) -> None:
    """A row whose value cannot be read (a reconfigured provider,
    a moved store) still lands on the new ref — what remains is a
    missing value, not a wrong one."""
    app = app_for(tmp_path)
    await seed_legacy_placeholder(app, "ws-a", "github_api", value=None)

    with caplog.at_level("WARNING"):
        moved = await app.state.secrets.migrate_legacy_refs()

    assert moved == 1
    (row,) = await app.state.model.list_placeholders()
    assert row["backend_ref"] == "MSKSWS_WS_A_GITHUB_API"
    assert "no readable value" in caplog.text


async def test_stale_legacy_manifest_heals_without_legacy_rows(
    tmp_path,
) -> None:
    """A pass that renamed every row but died before its final
    re-sync leaves a manifest declaring the legacy ref; the next
    boot re-renders it even with no legacy rows left (#335)."""
    app = app_for(tmp_path)
    await app.state.model.create_placeholder(
        workspace_id="ws-a",
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


async def test_failing_row_copy_leaves_the_row_legacy(tmp_path, monkeypatch):
    """A copy that fails mid-flight logs and leaves the row on its
    legacy ref — reads keep following the row's ref, and the next
    startup retries the move."""
    app = app_for(tmp_path)
    ref = await seed_legacy_placeholder(app, "ws-a", "github_api", "ghp-old")

    async def boom(placeholder_id, new_ref):
        raise RuntimeError("db gone")

    monkeypatch.setattr(app.state.model, "rename_placeholder_ref", boom)

    moved = await app.state.secrets.migrate_legacy_refs()

    assert moved == 1
    (row,) = await app.state.model.list_placeholders()
    assert row["backend_ref"] == ref
