"""The msksd YAML config file: modes, precedence, reload (#46)."""

import os
import signal
from pathlib import Path

import msks.server.main as main_mod
import pytest
import yaml
from msks.app import build_app
from msks.config import (
    CONFIG_ENV_VARS,
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
    path = write_config(tmp_path, "server:\n  port: 9000\n")
    assert resolve_config_path(None) == path
    assert "9000" in Path(path).read_text()


def test_explicit_path_missing_is_an_error(tmp_path) -> None:
    with pytest.raises(ValueError, match="config file not found"):
        resolve_config_path(str(tmp_path / "nope.yaml"))


def test_explicit_none_disables_the_file() -> None:
    assert resolve_config_path("none") == "none"


def test_generate_template_refuses_to_overwrite(tmp_path) -> None:
    path = str(tmp_path / "msksd.yaml")
    generate_template(path)
    with pytest.raises(FileExistsError):
        generate_template(path)


def test_template_mentions_docs_and_sections() -> None:
    body = render_template()
    assert "docs/config.md" in body
    for section in CONFIG_ENV_VARS:
        assert f"{section}:" in body


# --- parsing: key validation and scalar coercion ---


def test_file_overrides_translate_keys_to_env_vars(tmp_path) -> None:
    path = dump_config(
        tmp_path,
        {"server": {"port": 8660}, "vmm": {"driver": "local"}},
    )
    assert file_env_overrides(path) == {
        "MSKSD_PORT": "8660",
        "MSKSD_VMM_DRIVER": "local",
    }


def test_native_scalars_keep_their_meaning(tmp_path) -> None:
    path = dump_config(
        tmp_path,
        {
            "server": {"port": 8661, "access_log": True, "event_poll_s": 0.5},
            "net": {"enabled": False},
        },
    )
    layer = file_env_overrides(path)
    assert layer["MSKSD_PORT"] == "8661"
    assert layer["MSKSD_ACCESS_LOG"] == "true"
    assert layer["MSKSD_EVENT_POLL_S"] == "0.5"
    assert layer["MSKSD_EGRESS_ENABLED"] == "false"


def test_null_value_is_the_unset_form(tmp_path) -> None:
    path = write_config(tmp_path, "server:\n  bootstrap_token:\n")
    assert file_env_overrides(path) == {}


def test_empty_file_is_env_only(tmp_path) -> None:
    assert file_env_overrides(write_config(tmp_path, "")) == {}


def test_unknown_section_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "vm:\n  driver: local\n")
    with pytest.raises(ValueError, match="unknown config section 'vm'"):
        file_env_overrides(path)


def test_unknown_key_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "server:\n  prot: 8660\n")
    with pytest.raises(ValueError, match=r"unknown config key server.prot"):
        file_env_overrides(path)


def test_non_scalar_value_rejected(tmp_path) -> None:
    path = dump_config(tmp_path, {"server": {"host": ["a", "b"]}})
    with pytest.raises(ValueError, match="must be a number, boolean, or string"):
        file_env_overrides(path)


def test_scalar_section_value_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "vmm: local\n")
    with pytest.raises(ValueError, match="section 'vmm' must be a mapping"):
        file_env_overrides(path)


def test_non_mapping_document_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "- just\n- a list\n")
    with pytest.raises(ValueError, match="must be a mapping of sections"):
        file_env_overrides(path)


def test_invalid_yaml_rejected(tmp_path) -> None:
    path = write_config(tmp_path, "server: [unclosed\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        file_env_overrides(path)


def test_missing_file_raises_oserror(tmp_path) -> None:
    with pytest.raises(OSError):
        file_env_overrides(str(tmp_path / "nope.yaml"))


# --- precedence: env > file > defaults ---


def test_file_overrides_defaults(tmp_path) -> None:
    path = dump_config(tmp_path, {"server": {"port": 9001}})
    settings = load_settings(path)
    assert settings.server.port == 9001


def test_env_overrides_file(tmp_path) -> None:
    path = dump_config(tmp_path, {"server": {"port": 9001}})
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MSKSD_PORT", "9002")
        settings = load_settings(path)
    assert settings.server.port == 9002


def test_empty_env_falls_through_to_file(tmp_path) -> None:
    path = dump_config(tmp_path, {"server": {"port": 9001}})
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MSKSD_PORT", "")
        settings = load_settings(path)
    assert settings.server.port == 9001


def test_unset_keys_keep_defaults(tmp_path) -> None:
    path = dump_config(tmp_path, {"server": {"port": 9001}})
    settings = load_settings(path)
    assert settings.server.host == "127.0.0.1"
    assert settings.vmm.driver == "local"


def test_state_dir_feeds_the_server_db_path(tmp_path) -> None:
    path = dump_config(tmp_path, {"vmm": {"state_dir": "/var/lib/msksd"}})
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
    path = dump_config(tmp_path, {"vmm": {"driver": "firecracker"}})
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
    layered = LayeredEnv({"MSKSD_PORT": "1", "MSKSD_HOST": "h"})
    monkeypatch.setenv("MSKSD_PORT", "2")
    monkeypatch.setenv("MSKSD_TLS_CERT", "/c.pem")
    names = {"MSKSD_HOST", "MSKSD_PORT", "MSKSD_TLS_CERT"}
    assert set(layered) == set(os.environ) | names
    assert len(layered) == len(set(os.environ) | names)


def test_default_generation_race_is_survived(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A concurrent msksd generating the file mid-check proceeds."""
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path))

    def raced(path: str) -> None:
        raise FileExistsError(path)

    monkeypatch.setattr("msks.config.generate_template", raced)
    assert resolve_config_path(None) == str(tmp_path / "msksd.yaml")


# (section, key, yaml value, attribute path, expected) — one row per
# config key, pinning the whole CONFIG_ENV_VARS table end-to-end: the
# value written to the file must appear on the loaded settings field.
KEY_CASES = [
    ("vmm", "driver", "k8s", "vmm.driver", "k8s"),
    ("vmm", "cloud_hypervisor", "/ch", "vmm.cloud_hypervisor", "/ch"),
    ("vmm", "state_dir", "/st", "vmm.state_dir", "/st"),
    ("vmm", "socket_wait_timeout_s", 11.0, "vmm.socket_wait_timeout_s", 11.0),
    ("vmm", "request_timeout_s", 6.0, "vmm.request_timeout_s", 6.0),
    ("vmm", "shutdown_timeout_s", 21.0, "vmm.shutdown_timeout_s", 21.0),
    ("vmm", "vsock_shell_port", 1024, "vmm.vsock_shell_port", 1024),
    ("vmm", "vsock_wait_timeout_s", 16.0, "vmm.vsock_wait_timeout_s", 16.0),
    ("vmm", "default_image", "/img.tar", "vmm.default_image", "/img.tar"),
    ("vmm", "qemu_img", "/qi", "vmm.qemu_img", "/qi"),
    ("vmm", "mkfs_ext4", "/mkfs", "vmm.mkfs_ext4", "/mkfs"),
    ("vmm", "mkisofs", "/mkisofs", "vmm.mkisofs", "/mkisofs"),
    ("vmm", "host_name", "host-a", "vmm.host_name", "host-a"),
    ("vmm", "root_mib", 4096, "vmm.root_mib", 4096),
    ("vmm", "home_mib", 512, "vmm.home_mib", 512),
    ("server", "host", "0.0.0.0", "server.host", "0.0.0.0"),
    ("server", "port", 9000, "server.port", 9000),
    ("server", "tls_cert", "/c.pem", "server.tls_cert", "/c.pem"),
    ("server", "tls_key", "/k.pem", "server.tls_key", "/k.pem"),
    ("server", "event_poll_s", 2.5, "server.event_poll_s", 2.5),
    ("server", "bootstrap_token", "tok", "server.bootstrap_token", "tok"),
    ("server", "access_log", True, "server.access_log", True),
    ("k8s", "namespace", "ns1", "k8s.namespace", "ns1"),
    ("k8s", "runner_image", "img:2", "k8s.runner_image", "img:2"),
    ("k8s", "kubeconfig", "/kc", "k8s.kubeconfig", "/kc"),
    ("k8s", "api_timeout_s", 9.0, "k8s.api_timeout_s", 9.0),
    ("k8s", "storage_class", "fast", "k8s.storage_class", "fast"),
    ("k8s", "workspace_storage_gib", 7, "k8s.workspace_storage_gib", 7),
    ("net", "enabled", True, "net.enabled", True),
    ("net", "pool", "10.9.0.0/16", "net.pool", "10.9.0.0/16"),
    ("net", "uplink", "enp1s0", "net.uplink", "enp1s0"),
    ("net", "dns_upstream", "1.1.1.1", "net.dns_upstream", "1.1.1.1"),
    ("net", "ip_tool", "/ipt", "net.ip_tool", "/ipt"),
    ("net", "nft_tool", "/nftt", "net.nft_tool", "/nftt"),
    ("net", "lease_s", 120, "net.lease_s", 120),
    ("net", "dns_timeout_s", 4.5, "net.dns_timeout_s", 4.5),
]


def section_keys(section: str) -> set[str]:
    """The case-list keys for one section (drift-guard helper)."""
    return {key for s, key, _, _, _ in KEY_CASES if s == section}


def test_config_table_fully_covered_by_cases() -> None:
    """Drift guard: every table key has a case, and no case is spare."""
    assert set(CONFIG_ENV_VARS) == {section for section, *_ in KEY_CASES}
    for section, keys in CONFIG_ENV_VARS.items():
        assert set(keys) == section_keys(section), (
            f"cases for {section} drift from the table"
        )


def test_every_key_reaches_its_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Every config key lands on its settings field when set in a file."""
    for var in ("MSKSD_PORT", "MSKSD_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    for section, key, value, attr, expected in KEY_CASES:
        path = dump_config(tmp_path, {section: {key: value}})
        settings = load_settings(path)
        got = settings
        for part in attr.split("."):
            got = getattr(got, part)
        # Stringified: the loaders coerce to typed values (int, bool,
        # Path, IPv4Network), and a parse failure raises before this.
        assert str(got) == str(expected), f"{section}.{key} did not reach {attr}"


# --- SIGHUP reload ---


def app_with_file(tmp_path, doc) -> object:
    return build_app(load_settings(dump_config(tmp_path, doc)))


def test_reload_swaps_live_settings(tmp_path) -> None:
    app = app_with_file(tmp_path, {"server": {"port": 9001}})
    write_config(tmp_path, "server:\n  port: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.port == 9004


def test_reload_refuses_invalid_config(tmp_path, capsys) -> None:
    app = app_with_file(tmp_path, {"server": {"port": 9001}})
    write_config(tmp_path, "server:\n  prot: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.port == 9001
    assert "reload refused" in capsys.readouterr().err


def test_reload_keeps_generated_tls(tmp_path) -> None:
    app = app_with_file(tmp_path, {"server": {"port": 9001}})
    app.state.settings.server.tls_cert = "/generated/c.pem"
    app.state.settings.server.tls_key = "/generated/k.pem"
    write_config(tmp_path, "server:\n  port: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/generated/c.pem"
    assert app.state.settings.server.tls_key == "/generated/k.pem"


def test_reload_keeps_operator_tls(tmp_path) -> None:
    app = app_with_file(
        tmp_path,
        {"server": {"port": 9001, "tls_cert": "/op/c.pem", "tls_key": "/op/k.pem"}},
    )
    write_config(tmp_path, "server:\n  port: 9004\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/op/c.pem"


def test_reload_with_half_configured_tls_keeps_startup_values(tmp_path) -> None:
    """A reload that names only one side of the pair keeps the other
    startup value — the listener runs on the pair it booted with."""
    app = app_with_file(tmp_path, {"server": {"port": 9001}})
    app.state.settings.server.tls_cert = "/generated/c.pem"
    app.state.settings.server.tls_key = "/generated/k.pem"
    write_config(tmp_path, "server:\n  tls_cert: /other/c.pem\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/other/c.pem"
    assert app.state.settings.server.tls_key == "/generated/k.pem"

    app = app_with_file(tmp_path, {"server": {"port": 9001}})
    app.state.settings.server.tls_cert = "/generated/c.pem"
    app.state.settings.server.tls_key = "/generated/k.pem"
    write_config(tmp_path, "server:\n  tls_key: /other/k.pem\n")
    main_mod.reload_settings(app, str(tmp_path / "msksd.yaml"))
    assert app.state.settings.server.tls_cert == "/generated/c.pem"
    assert app.state.settings.server.tls_key == "/other/k.pem"


def test_install_sighup_reload_wires_the_handler(tmp_path) -> None:
    previous = signal.getsignal(signal.SIGHUP)
    try:
        app = app_with_file(tmp_path, {"server": {"port": 9001}})
        main_mod.install_sighup_reload(app, str(tmp_path / "msksd.yaml"))
        write_config(tmp_path, "server:\n  port: 9005\n")
        signal.raise_signal(signal.SIGHUP)
        assert app.state.settings.server.port == 9005
    finally:
        signal.signal(signal.SIGHUP, previous)


# --- main() wiring ---


def test_main_reads_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seen = {}
    monkeypatch.setattr(
        main_mod,
        "serve",
        lambda app, no_tls: seen.update(port=app.state.settings.server.port),
    )
    assert (
        main_mod.main(["--config", dump_config(tmp_path, {"server": {"port": 9010}})])
        == 0
    )
    assert seen == {"port": 9010}
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


def test_main_missing_config_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    served = []
    monkeypatch.setattr(main_mod, "serve", lambda app, no_tls: served.append(1))
    assert main_mod.main(["--config", str(tmp_path / "nope.yaml")]) == 2
    assert served == []
    assert "config file not found" in capsys.readouterr().err


def test_main_invalid_config_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    served = []
    monkeypatch.setattr(main_mod, "serve", lambda app, no_tls: served.append(1))
    path = write_config(tmp_path, "vmm:\n  driver: firecracker\n")
    assert main_mod.main(["--config", path]) == 2
    assert served == []
    assert "MSKSD_VMM_DRIVER" in capsys.readouterr().err


def test_settings_from_env_still_reads_plain_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_PORT", "9006")
    assert Settings.from_env().server.port == 9006
