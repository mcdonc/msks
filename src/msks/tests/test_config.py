"""The msksd YAML config file: modes, precedence, reload (#46)."""

import os
import re
import signal
from pathlib import Path

import msks.server.main as main_mod
import msks.settings as msks_settings
import pytest
import yaml
from msks.app import build_app
from msks.config import (
    CONFIG_ENV_VARS,
    SETTING_ENV_VARS,
    LayeredEnv,
    config_dir,
    default_config_path,
    file_env_overrides,
    generate_template,
    load_settings,
    parse_config_doc,
    render_template,
    resolve_config_path,
)
from msks.settings import Settings


def write_config(tmp_path, doc: str) -> str:
    path = tmp_path / "msksd.yaml"
    path.write_text(doc)
    return str(path)


def dump_config(tmp_path, doc) -> str:
    return write_config(tmp_path, yaml.safe_dump(doc))


# --- the one key↔variable rule ---


def test_keys_derive_from_variables_by_one_rule() -> None:
    """Every key is its variable minus ``MSKSD_``, lowercased.

    The table is built by this rule, so the file spelling and the
    variable spelling cannot drift — either is recoverable from the
    other without a lookup (klangkd's convention, #46).
    """
    assert set(CONFIG_ENV_VARS) == {
        var.removeprefix("MSKSD_").lower() for var in SETTING_ENV_VARS
    }
    assert len(CONFIG_ENV_VARS) == len(SETTING_ENV_VARS)  # no collision


def test_setting_vars_match_the_settings_source() -> None:
    """SETTING_ENV_VARS is exactly the set of ``MSKSD_*`` variables
    settings.py reads — a variable added there without a tuple entry
    fails here (its config key would die as unknown)."""
    text = Path(msks_settings.__file__).read_text()
    read_vars = set(re.findall(r'"(MSKSD_[A-Z0-9_]+)"', text))
    assert read_vars == set(SETTING_ENV_VARS)


def test_config_dir_var_is_not_a_config_key() -> None:
    """The bootstrap variable relocates the tree the file lives in,
    so it cannot come from the file itself."""
    assert "config_dir" not in CONFIG_ENV_VARS


# --- path resolution: the three --config modes ---


def test_default_path_honors_config_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_CONFIG_DIR", "/etc/msksd")
    assert default_config_path() == "/etc/msksd/msksd.yaml"


def test_default_path_xdg_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MSKSD_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert default_config_path() == "/xdg/msksd/msksd.yaml"


def test_default_path_home_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MSKSD_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert default_config_path().endswith("/.config/msksd/msksd.yaml")
    assert config_dir().endswith("/.config/msksd")


def test_bare_invocation_generates_the_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path / "cfg"))
    path = resolve_config_path(None)
    assert path == str(tmp_path / "cfg" / "msksd.yaml")
    body = (tmp_path / "cfg" / "msksd.yaml").read_text()
    assert "msksd configuration" in body
    # The template is valid YAML (every line commented -> empty doc).
    assert parse_config_doc(body, path) == {}


def test_existing_default_file_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path))
    path = write_config(tmp_path, "port: 9000\n")
    assert resolve_config_path(None) == path
    assert "9000" in Path(path).read_text()


def test_explicit_path_missing_is_an_error(tmp_path) -> None:
    with pytest.raises(ValueError, match="config file not found"):
        resolve_config_path(str(tmp_path / "nope.yaml"))


def test_explicit_none_disables_the_file() -> None:
    assert resolve_config_path("none") == "none"


def test_explicit_directory_gets_its_own_error(tmp_path) -> None:
    with pytest.raises(ValueError, match="config path is a directory"):
        resolve_config_path(str(tmp_path))


def test_generated_template_is_owner_only(tmp_path) -> None:
    """The template names credentials in its examples, so the file
    is 0600 like every other secret-bearing artifact."""
    path = tmp_path / "msksd.yaml"
    generate_template(str(path))
    assert path.stat().st_mode & 0o777 == 0o600


def test_generate_template_refuses_to_overwrite(tmp_path) -> None:
    path = str(tmp_path / "msksd.yaml")
    generate_template(path)
    with pytest.raises(FileExistsError):
        generate_template(path)


def test_default_generation_race_is_survived(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A concurrent msksd generating the file mid-check proceeds."""
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path))

    def raced(path: str) -> None:
        raise FileExistsError(path)

    monkeypatch.setattr("msks.config.generate_template", raced)
    assert resolve_config_path(None) == str(tmp_path / "msksd.yaml")


def test_template_mentions_docs_and_rule() -> None:
    body = render_template()
    assert "docs/config.md" in body
    assert "MSKSD_PORT -> port" in body
    for key in ("port:", "state_dir:", "k8s_namespace:", "egress_subnet:"):
        assert key in body


# --- parsing: key validation and scalar coercion ---


def test_file_overrides_translate_keys_to_env_vars(tmp_path) -> None:
    path = dump_config(tmp_path, {"port": 8660, "vmm_driver": "local"})
    assert file_env_overrides(path) == {
        "MSKSD_PORT": "8660",
        "MSKSD_VMM_DRIVER": "local",
    }


def test_native_scalars_keep_their_meaning(tmp_path) -> None:
    path = dump_config(
        tmp_path,
        {
            "port": 8661,
            "access_log": True,
            "event_poll_s": 0.5,
            "egress_enabled": False,
        },
    )
    layer = file_env_overrides(path)
    assert layer["MSKSD_PORT"] == "8661"
    assert layer["MSKSD_ACCESS_LOG"] == "true"
    assert layer["MSKSD_EVENT_POLL_S"] == "0.5"
    assert layer["MSKSD_EGRESS_ENABLED"] == "false"


def test_yaml11_bool_spellings_parse(tmp_path) -> None:
    """yes/on are booleans to PyYAML (YAML 1.1), so they keep their
    meaning; the single letters y/n are plain strings (documented)."""
    path = write_config(tmp_path, "access_log: yes\negress_enabled: off\n")
    layer = file_env_overrides(path)
    assert layer["MSKSD_ACCESS_LOG"] == "true"
    assert layer["MSKSD_EGRESS_ENABLED"] == "false"


def test_null_value_is_the_unset_form(tmp_path) -> None:
    path = write_config(tmp_path, "bootstrap_token:\n")
    assert file_env_overrides(path) == {}


def test_empty_file_is_env_only(tmp_path) -> None:
    assert file_env_overrides(write_config(tmp_path, "")) == {}


def test_unknown_key_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "prot: 8660\n")
    with pytest.raises(ValueError, match="unknown config key 'prot'"):
        file_env_overrides(path)


def test_section_shaped_file_rejected(tmp_path) -> None:
    """The file is flat: a section-shaped file names an unknown key."""
    path = write_config(tmp_path, "server:\n  port: 8660\n")
    with pytest.raises(ValueError, match="unknown config key 'server'"):
        file_env_overrides(path)


def test_non_scalar_value_rejected(tmp_path) -> None:
    path = dump_config(tmp_path, {"host": ["a", "b"]})
    with pytest.raises(
        ValueError, match="must be a number, boolean, or string"
    ):
        file_env_overrides(path)


def test_non_string_key_rejected(tmp_path) -> None:
    path = dump_config(tmp_path, {1: "x"})
    with pytest.raises(ValueError, match="config keys must be strings, got 1"):
        file_env_overrides(path)


def test_complex_yaml_key_rejected_cleanly(tmp_path) -> None:
    """A list-typed mapping key is legal YAML but not a config key:
    it must be refused as a ValueError, never a TypeError escaping
    the guards (fresh-eyes review, second pass)."""
    path = write_config(tmp_path, "? [a, b]\n: 1\n")
    with pytest.raises(ValueError, match="config keys must be scalars"):
        file_env_overrides(path)


def test_duplicate_key_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "port: 9001\nport: 9002\n")
    with pytest.raises(ValueError, match="duplicate config key 'port'"):
        file_env_overrides(path)


def test_merge_keys_refused_with_their_own_message(tmp_path) -> None:
    """The file is flat and every key is spelled out, so ``<<:
    *anchor`` is refused by name (fresh-eyes review) — not by
    PyYAML's opaque tag error, and not as the anchor carrier's
    unknown-key error."""
    path = write_config(
        tmp_path, "base: &b\n  port: 9001\nhost: 0.0.0.0\n<<: *b\n"
    )
    with pytest.raises(ValueError, match=r"merge keys .<<. are not supported"):
        file_env_overrides(path)


def test_nonfinite_float_rejected_from_file(tmp_path) -> None:
    path = write_config(tmp_path, "event_poll_s: .nan\n")
    with pytest.raises(ValueError, match="MSKSD_EVENT_POLL_S"):
        load_settings(path)


def test_nonfinite_float_rejected_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_EGRESS_DNS_TIMEOUT_S", "inf")
    with pytest.raises(ValueError, match="MSKSD_EGRESS_DNS_TIMEOUT_S"):
        Settings.from_env()


def test_non_mapping_document_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "- just\n- a list\n")
    with pytest.raises(ValueError, match="must be a mapping of keys"):
        file_env_overrides(path)


def test_invalid_yaml_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "port: [unclosed\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        file_env_overrides(path)


def test_missing_file_raises_oserror(tmp_path) -> None:
    with pytest.raises(OSError):
        file_env_overrides(str(tmp_path / "nope.yaml"))


# --- precedence: env > file > defaults ---


def test_file_overrides_defaults(tmp_path) -> None:
    path = dump_config(tmp_path, {"port": 9001})
    settings = load_settings(path)
    assert settings.server.port == 9001


def test_env_overrides_file(tmp_path) -> None:
    path = dump_config(tmp_path, {"port": 9001})
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MSKSD_PORT", "9002")
        settings = load_settings(path)
    assert settings.server.port == 9002


def test_empty_env_falls_through_to_file(tmp_path) -> None:
    path = dump_config(tmp_path, {"port": 9001})
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MSKSD_PORT", "")
        settings = load_settings(path)
    assert settings.server.port == 9001


def test_unset_keys_keep_defaults(tmp_path) -> None:
    path = dump_config(tmp_path, {"port": 9001})
    settings = load_settings(path)
    assert settings.server.host == "127.0.0.1"
    assert settings.vmm.driver == "local"


def test_state_dir_feeds_the_server_db_path(tmp_path) -> None:
    path = dump_config(tmp_path, {"state_dir": "/var/lib/msksd"})
    settings = load_settings(path)
    assert str(settings.vmm.state_dir) == "/var/lib/msksd"
    assert str(settings.server.db_path) == "/var/lib/msksd/msks.db"


def test_none_reads_env_and_defaults_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_PORT", "9003")
    settings = load_settings("none")
    assert settings.server.port == 9003
    assert settings.server.host == "127.0.0.1"


def test_validation_errors_name_the_env_var(tmp_path) -> None:
    path = dump_config(tmp_path, {"vmm_driver": "firecracker"})
    with pytest.raises(ValueError, match="MSKSD_VMM_DRIVER"):
        load_settings(path)


def test_layered_env_lookup_order(monkeypatch: pytest.MonkeyPatch) -> None:
    layered = LayeredEnv({"MSKSD_PORT": "1", "MSKSD_HOST": "h"})
    monkeypatch.setenv("MSKSD_PORT", "2")
    assert layered["MSKSD_PORT"] == "2"  # env wins
    assert layered["MSKSD_HOST"] == "h"  # file applies
    assert layered.get("MSKSD_ABSENT") is None
    monkeypatch.setenv("MSKSD_PORT", "")
    assert layered["MSKSD_PORT"] == "1"  # empty env is the unset form


def test_layered_env_iterates_and_measures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layered = LayeredEnv(
        {"MSKSD_PORT": "1", "MSKSD_HOST": "h", "MSKSD_BOOTSTRAP_TOKEN": ""}
    )
    monkeypatch.setenv("MSKSD_PORT", "2")
    monkeypatch.setenv("MSKSD_TLS_CERT", "/c.pem")
    monkeypatch.setenv("MSKSD_EVENT_POLL_S", "")  # empty: falls through
    # Iteration mirrors lookup: truthy env entries plus every file
    # key, with empty-string env entries never shadowing the file.
    expected = {name for name, value in os.environ.items() if value} | {
        "MSKSD_HOST",
        "MSKSD_PORT",
        "MSKSD_TLS_CERT",
        "MSKSD_BOOTSTRAP_TOKEN",
    }
    assert set(layered) == expected
    assert len(layered) == len(expected)
    assert layered["MSKSD_BOOTSTRAP_TOKEN"] == ""  # file's empty unset form


# (key, yaml value, attribute path, expected) — one row per config
# key, pinning the whole derived table end-to-end: the value written
# to the file must appear on the loaded settings field.
KEY_CASES = [
    ("vmm_driver", "k8s", "vmm.driver", "k8s"),
    ("cloud_hypervisor", "/ch", "vmm.cloud_hypervisor", "/ch"),
    ("state_dir", "/st", "vmm.state_dir", "/st"),
    ("socket_wait_timeout_s", 11.0, "vmm.socket_wait_timeout_s", 11.0),
    ("request_timeout_s", 6.0, "vmm.request_timeout_s", 6.0),
    ("shutdown_timeout_s", 21.0, "vmm.shutdown_timeout_s", 21.0),
    ("vsock_shell_port", 1024, "vmm.vsock_shell_port", 1024),
    ("vsock_wait_timeout_s", 16.0, "vmm.vsock_wait_timeout_s", 16.0),
    ("forward_wait_timeout_s", 5.5, "vmm.forward_wait_timeout_s", 5.5),
    ("console_stall_timeout_s", 31.0, "vmm.console_stall_timeout_s", 31.0),
    ("move_wait_timeout_s", 7.5, "vmm.move_wait_timeout_s", 7.5),
    ("default_image", "/img.tar", "vmm.default_image", "/img.tar"),
    ("qemu_img", "/qi", "vmm.qemu_img", "/qi"),
    ("mkfs_ext4", "/mkfs", "vmm.mkfs_ext4", "/mkfs"),
    ("mkisofs", "/mkisofs", "vmm.mkisofs", "/mkisofs"),
    ("host_name", "host-a", "vmm.host_name", "host-a"),
    ("root_mib", 4096, "vmm.root_mib", 4096),
    ("home_mib", 512, "vmm.home_mib", 512),
    ("storage_warn_pct", 80, "vmm.storage_warn_pct", 80),
    ("storage_floor_mib", 256, "vmm.storage_floor_mib", 256),
    ("resize2fs", "/r2fs", "vmm.resize2fs", "/r2fs"),
    ("e2fsck", "/fsck", "vmm.e2fsck", "/fsck"),
    ("host", "0.0.0.0", "server.host", "0.0.0.0"),
    ("port", 9000, "server.port", 9000),
    ("tls_cert", "/c.pem", "server.tls_cert", "/c.pem"),
    ("tls_key", "/k.pem", "server.tls_key", "/k.pem"),
    ("event_poll_s", 2.5, "server.event_poll_s", 2.5),
    ("bootstrap_token", "tok", "server.bootstrap_token", "tok"),
    ("access_log", True, "server.access_log", True),
    ("k8s_namespace", "ns1", "k8s.namespace", "ns1"),
    ("k8s_runner_image", "img:2", "k8s.runner_image", "img:2"),
    ("kubeconfig", "/kc", "k8s.kubeconfig", "/kc"),
    ("k8s_api_timeout_s", 9.0, "k8s.api_timeout_s", 9.0),
    ("k8s_storage_class", "fast", "k8s.storage_class", "fast"),
    ("k8s_workspace_storage_gib", 7, "k8s.workspace_storage_gib", 7),
    ("egress_enabled", True, "net.enabled", True),
    ("egress_subnet", "10.9.0.0/16", "net.pool", "10.9.0.0/16"),
    ("egress_uplink", "enp1s0", "net.uplink", "enp1s0"),
    ("egress_dns_upstream", "1.1.1.1", "net.dns_upstream", "1.1.1.1"),
    ("ip_tool", "/ipt", "net.ip_tool", "/ipt"),
    ("nft_tool", "/nftt", "net.nft_tool", "/nftt"),
    ("egress_lease_s", 120, "net.lease_s", 120),
    ("egress_dns_timeout_s", 4.5, "net.dns_timeout_s", 4.5),
    ("egress_mode", "interactive", "net.egress_mode", "interactive"),
    (
        "egress_consent_timeout_s",
        90.0,
        "net.consent_timeout_s",
        90.0,
    ),
    (
        "egress_consent_rate_limit",
        4,
        "net.consent_rate_limit",
        4,
    ),
    (
        "egress_consent_retention_days",
        7,
        "net.consent_retention_days",
        7,
    ),
    ("egress_consent_row_cap", 50, "net.consent_row_cap", 50),
    ("egress_queue_base", 2048, "net.queue_base", 2048),
    ("conntrack_tool", "/ct", "net.conntrack_tool", "/ct"),
    ("audit_hmac_key", "k1", "server.audit_hmac_key", "k1"),
    ("ssh_key_type", "ed25519", "vmm.ssh_key_type", "ed25519"),
]


def test_config_table_fully_covered_by_cases() -> None:
    """Drift guard: every table key has a case, and no case is spare."""
    assert set(CONFIG_ENV_VARS) == {key for key, *_ in KEY_CASES}


def test_every_key_reaches_its_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Every config key lands on its settings field when set in a file."""
    for var in ("MSKSD_PORT", "MSKSD_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    for key, value, attr, expected in KEY_CASES:
        path = dump_config(tmp_path, {key: value})
        settings = load_settings(path)
        got = settings
        for part in attr.split("."):
            got = getattr(got, part)
        # Stringified: the loaders coerce to typed values (int, bool,
        # Path, IPv4Network), and a parse failure raises before this.
        assert str(got) == str(expected), f"{key} did not reach {attr}"


# --- SIGHUP reload ---


def app_with_file(tmp_path, doc) -> object:
    return build_app(load_settings(dump_config(tmp_path, doc)))


def test_reload_swaps_live_settings(tmp_path) -> None:
    app = app_with_file(tmp_path, {"port": 9001})
    write_config(tmp_path, "port: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.port == 9004


def test_reload_refuses_invalid_config(tmp_path, capsys) -> None:
    app = app_with_file(tmp_path, {"port": 9001})
    write_config(tmp_path, "prot: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.port == 9001
    assert "reload refused" in capsys.readouterr().err


def test_reload_refuses_deleted_default_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """A deleted default-path file is refused, not regenerated (#46
    review): a reload is not a first run, and regenerating would
    silently revert every file-set value."""
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(cfg))
    cfg.mkdir()
    (cfg / "msksd.yaml").write_text("port: 9021\n")
    app = app_with_file(tmp_path, {"port": 9001})
    (cfg / "msksd.yaml").unlink()
    main_mod.reload_settings(app, None)
    assert app.state.settings.server.port == 9001
    assert not (cfg / "msksd.yaml").exists()  # not regenerated
    assert "config file not found" in capsys.readouterr().err


def test_reload_reads_present_default_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """SIGHUP with the default-path file present re-reads it (no
    generation involved on the reload path)."""
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(cfg))
    cfg.mkdir()
    (cfg / "msksd.yaml").write_text("port: 9021\n")
    app = app_with_file(tmp_path, {"port": 9001})
    (cfg / "msksd.yaml").write_text("port: 9022\n")
    main_mod.reload_settings(app, None)
    assert app.state.settings.server.port == 9022


def test_reload_keeps_generated_tls(tmp_path) -> None:
    app = app_with_file(tmp_path, {"port": 9001})
    app.state.settings.server.tls_cert = "/generated/c.pem"
    app.state.settings.server.tls_key = "/generated/k.pem"
    write_config(tmp_path, "port: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/generated/c.pem"
    assert app.state.settings.server.tls_key == "/generated/k.pem"


def test_reload_latches_the_state_dir(tmp_path) -> None:
    """A reload that moves state_dir changes nothing: the engine is
    open on the old path and the local driver resolves workspace
    artifacts from it live — moving it mid-run would orphan running
    workspaces (fresh-eyes review, third pass)."""
    app = app_with_file(tmp_path, {"port": 9001})
    startup_dir = app.state.settings.vmm.state_dir
    write_config(tmp_path, "port: 9004\nstate_dir: /moved\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.port == 9004  # the live swap held
    assert app.state.settings.vmm.state_dir == startup_dir
    assert app.state.settings.server.db_path.parent == startup_dir


def test_reload_keeps_operator_tls(tmp_path) -> None:
    app = app_with_file(
        tmp_path,
        {"port": 9001, "tls_cert": "/op/c.pem", "tls_key": "/op/k.pem"},
    )
    write_config(tmp_path, "port: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/op/c.pem"


def test_reload_with_half_configured_tls_keeps_startup_values(
    tmp_path,
) -> None:
    """A reload that names only one side of the pair keeps the other
    startup value — the listener runs on the pair it booted with."""
    app = app_with_file(tmp_path, {"port": 9001})
    app.state.settings.server.tls_cert = "/generated/c.pem"
    app.state.settings.server.tls_key = "/generated/k.pem"
    write_config(tmp_path, "tls_cert: /other/c.pem\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/other/c.pem"
    assert app.state.settings.server.tls_key == "/generated/k.pem"

    app = app_with_file(tmp_path, {"port": 9001})
    app.state.settings.server.tls_cert = "/generated/c.pem"
    app.state.settings.server.tls_key = "/generated/k.pem"
    write_config(tmp_path, "tls_key: /other/k.pem\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/generated/c.pem"
    assert app.state.settings.server.tls_key == "/other/k.pem"


def test_reload_survives_a_complex_yaml_key(tmp_path, capsys) -> None:
    """The refusing path keeps its contract even on input the YAML
    loader itself chokes on structurally (no TypeError escape)."""
    app = app_with_file(tmp_path, {"port": 9001})
    write_config(tmp_path, "? [a, b]\n: 1\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.port == 9001
    assert "reload refused" in capsys.readouterr().err


def test_install_sighup_reload_wires_the_handler(tmp_path) -> None:
    previous = signal.getsignal(signal.SIGHUP)
    try:
        app = app_with_file(tmp_path, {"port": 9001})
        main_mod.install_sighup_reload(app, str(tmp_path / "msksd.yaml"))
        write_config(tmp_path, "port: 9005\n")
        signal.raise_signal(signal.SIGHUP)
        assert app.state.settings.server.port == 9005
    finally:
        signal.signal(signal.SIGHUP, previous)


# --- main() wiring ---


def test_main_reads_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "state"))
    previous = signal.getsignal(signal.SIGHUP)
    try:
        seen = {}
        monkeypatch.setattr(
            main_mod,
            "serve",
            lambda app, no_tls: seen.update(
                port=app.state.settings.server.port
            ),
        )
        config = dump_config(tmp_path, {"port": 9010})
        assert main_mod.main(["--config", config]) == 0
        assert seen == {"port": 9010}
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_main_half_configured_tls_fails_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """A file naming only one side of the TLS pair exits 2 with the
    one-line pair error, not a traceback from inside serve."""
    served = []
    monkeypatch.setattr(
        main_mod, "serve", lambda app, no_tls: served.append(1)
    )
    config = dump_config(tmp_path, {"tls_cert": "/c.pem"})
    assert main_mod.main(["--config", config]) == 2
    assert served == []
    assert "must be set together" in capsys.readouterr().err


def test_main_tls_pre_flight_runs_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    """main's arm_tls pre-flight + serve's resolution print the CA
    fingerprint at most once (idempotence pinned, fresh-eyes review)."""
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "state"))
    previous = signal.getsignal(signal.SIGHUP)
    try:
        monkeypatch.setattr(main_mod, "run_forever", lambda app: None)
        config = dump_config(tmp_path, {"port": 9011})
        assert main_mod.main(["--config", config]) == 0
        err = capsys.readouterr().err
        assert err.count("CA fingerprint") == 1
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_main_missing_config_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    served = []
    monkeypatch.setattr(
        main_mod, "serve", lambda app, no_tls: served.append(1)
    )
    assert main_mod.main(["--config", str(tmp_path / "nope.yaml")]) == 2
    assert served == []
    assert "config file not found" in capsys.readouterr().err


def test_main_invalid_config_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    served = []
    monkeypatch.setattr(
        main_mod, "serve", lambda app, no_tls: served.append(1)
    )
    path = write_config(tmp_path, "vmm_driver: firecracker\n")
    assert main_mod.main(["--config", path]) == 2
    assert served == []
    assert "MSKSD_VMM_DRIVER" in capsys.readouterr().err


def test_settings_from_env_still_reads_plain_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_PORT", "9006")
    assert Settings.from_env().server.port == 9006
