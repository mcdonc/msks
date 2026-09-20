"""Secret store tests (#198): manifest, URIs, and the real CLI.

The subprocess round-trips run the real ``secretspec`` binary from
the devenv shell against the ``file`` provider in a tmp root — the
same integration the daemon ships — so these tests assert what the
CLI actually does (stderr shape, stdin value handling, the newline
``get`` appends on 0.20), not what a stub would echo back.
"""

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
    """Dots and dashes become underscores, uppercased, MSKS-prefixed."""
    assert backend_ref("my-ws", "github_api") == "MSKS_MY_WS_GITHUB_API"
    assert backend_ref("a.b", "c.d") == "MSKS_A_B_C_D"


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
        "file:/store", [("MSKS_B_X", "b/x"), ("MSKS_A_Y", "a/y")]
    )
    assert 'name = "msks"' in body
    assert 'store = "file:/store"' in body
    assert body.index("MSKS_A_Y") < body.index("MSKS_B_X")
    assert 'providers = ["store"]' in body


def test_manifest_modes(tmp_path) -> None:
    """The root is 0700 and the manifest 0600 — real secrets' bytes
    live beside them on the file provider."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    root = tmp_path / "store"
    manifest = root / MANIFEST_NAME
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_manifest_sync_is_change_driven(tmp_path) -> None:
    """An unchanged body is not rewritten (mtime holds)."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    manifest = tmp_path / "store" / MANIFEST_NAME
    first_mtime = manifest.stat().st_mtime_ns
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    assert manifest.stat().st_mtime_ns == first_mtime
    store.sync_manifest([("MSKS_WS_X", "ws/x"), ("MSKS_WS_Y", "ws/y")])
    assert "MSKS_WS_Y" in manifest.read_text()


async def test_write_read_delete_roundtrip(tmp_path) -> None:
    """The daemon's three operations against the real CLI."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKS_WS_GITHUB", "ws/github")])
    await store.write("MSKS_WS_GITHUB", "ghp-real-token-123")
    stored = tmp_path / "store" / "msks" / "default" / "MSKS_WS_GITHUB"
    assert stored.read_text() == "ghp-real-token-123"
    assert stat.S_IMODE(stored.stat().st_mode) & 0o077 == 0
    assert await store.read("MSKS_WS_GITHUB") == "ghp-real-token-123"
    await store.delete("MSKS_WS_GITHUB")
    assert not stored.exists()


async def test_read_survives_a_cold_cache(tmp_path) -> None:
    """A restarted daemon re-fetches by backend ref: a second store
    object (empty cache) reads what the first one wrote."""
    first = store_for(tmp_path)
    first.sync_manifest([("MSKS_WS_X", "ws/x")])
    await first.write("MSKS_WS_X", "value-1")
    second = store_for(tmp_path)
    assert await second.read("MSKS_WS_X") == "value-1"


async def test_read_uses_the_cache(tmp_path) -> None:
    """After the first fetch the value comes from memory — the file
    can disappear and the swap path still answers."""
    store = store_for(tmp_path)
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    await store.write("MSKS_WS_X", "value-1")
    (tmp_path / "store" / "msks" / "default" / "MSKS_WS_X").unlink()
    assert await store.read("MSKS_WS_X") == "value-1"


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
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError):
        await store.write("MSKS_WS_X", "v")


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
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError, match="timed out"):
        await store.write("MSKS_WS_X", "v")


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
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError, match="the real error"):
        await store.write("MSKS_WS_X", "v")

    silent = tmp_path / "silent-cli"
    silent.write_text("#!/bin/sh\nexit 3\n")
    silent.chmod(0o755)
    store = store_for(tmp_path, {"MSKSD_SECRET_STORE_CLI": str(silent)})
    store.sync_manifest([("MSKS_WS_X", "ws/x")])
    with pytest.raises(SecretStoreError, match="unknown error"):
        await store.read("MSKS_WS_X")
