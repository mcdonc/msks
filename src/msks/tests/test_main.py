"""Entry point and server-config wiring."""

import logging
import signal

import msks.server.main as main_mod
import pytest
from msks.app import build_app
from msks.server.main import main, server_config, ssl_paths
from msks.settings import ServerSettings, Settings, VmmSettings
from msks.spec.version import __version__


def app_with(server: ServerSettings):
    return build_app(Settings(server=server))


def test_ssl_paths_none_when_unconfigured() -> None:
    app = app_with(ServerSettings())
    assert ssl_paths(app) == (None, None)


def test_ssl_paths_operator() -> None:
    app = app_with(ServerSettings(tls_cert="/c.pem", tls_key="/k.pem"))
    assert ssl_paths(app) == ("/c.pem", "/k.pem")


def test_server_config_plain_and_tls() -> None:
    plain = server_config(
        app_with(ServerSettings(host="127.0.0.1", port=8999))
    )
    assert plain.host == "127.0.0.1" and plain.port == 8999
    secure = server_config(
        app_with(ServerSettings(tls_cert="/c.pem", tls_key="/k.pem"))
    )
    assert secure.ssl_certfile == "/c.pem"
    assert secure.ssl_keyfile == "/k.pem"


def test_main_version(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_the_tree_version_is_one_word() -> None:
    """#704: the tree's two version spellings (pyproject and the
    vocabulary leaf) are edited to the same word at release time —
    a drift makes every release build uninstallable through the
    setup tool, and the drift is load-bearing (the in-VM coherence
    guard compares one against the other)."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert version == __version__


def test_serve_resolves_tls(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(main_mod, "run_forever", lambda app: None)
    app = app_with(ServerSettings(db_path=tmp_path / "x.db"))
    main_mod.serve(app, no_tls=False)
    assert app.state.settings.server.tls_cert is not None
    assert app.state.settings.server.tls_key is not None


def test_serve_no_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "run_forever", lambda app: None)
    app = app_with(ServerSettings())
    main_mod.serve(app, no_tls=True)
    assert app.state.settings.server.tls_cert is None


def test_main_runs_serve(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "state"))
    previous = signal.getsignal(signal.SIGHUP)
    try:
        seen = {}
        monkeypatch.setattr(
            main_mod, "serve", lambda app, no_tls: seen.update(no_tls=no_tls)
        )
        assert main([]) == 0
        assert seen == {"no_tls": False}
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_serve_operator_certs_skip_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(main_mod, "run_forever", lambda app: None)
    app = app_with(
        ServerSettings(
            db_path=tmp_path / "x.db",
            tls_cert="/op/c.pem",
            tls_key="/op/k.pem",
        )
    )
    main_mod.serve(app, no_tls=False)
    assert app.state.settings.server.tls_cert == "/op/c.pem"


def test_main_reload_arms_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "state"))
    previous = signal.getsignal(signal.SIGHUP)
    try:
        seen = {}
        monkeypatch.setattr(
            main_mod, "serve", lambda app, no_tls: seen.update(no_tls=no_tls)
        )
        monkeypatch.setattr(
            main_mod, "arm_reload_watcher", lambda: seen.update(reload=True)
        )
        assert main(["--reload"]) == 0
        assert seen == {"no_tls": False, "reload": True}
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_main_without_reload_leaves_watcher_disarmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "state"))
    previous = signal.getsignal(signal.SIGHUP)
    try:
        seen = {}
        monkeypatch.setattr(
            main_mod, "serve", lambda app, no_tls: seen.update(no_tls=no_tls)
        )
        monkeypatch.setattr(
            main_mod, "arm_reload_watcher", lambda: seen.update(reload=True)
        )
        assert main([]) == 0
        assert seen == {"no_tls": False}
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_main_configures_msks_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "state"))
    previous = signal.getsignal(signal.SIGHUP)
    root = logging.getLogger()
    msks_logger = logging.getLogger("msks")
    old_handlers = root.handlers[:]
    old_root_level = root.level
    old_msks_level = msks_logger.level
    # Clear handlers so basicConfig actually installs one.
    root.handlers = []
    root.level = logging.WARNING
    msks_logger.level = logging.NOTSET
    try:
        monkeypatch.setattr(main_mod, "serve", lambda app, no_tls: None)
        assert main([]) == 0
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
        assert msks_logger.level <= logging.INFO
    finally:
        signal.signal(signal.SIGHUP, previous)
        root.handlers = old_handlers
        root.level = old_root_level
        msks_logger.level = old_msks_level


# --- the MSKSD_NESTED startup notice (#1140) ---


def test_nested_notice_off_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A setting left off is the default on every host: the daemon
    logs nothing about it."""
    reads: list[str] = []
    monkeypatch.setattr(
        main_mod, "host_nested_support", lambda: reads.append("read")
    )
    with caplog.at_level(logging.INFO, logger="msks.server.main"):
        main_mod.nested_notice(Settings())
    assert caplog.records == []
    assert reads == []


def test_nested_notice_warns_when_the_host_cannot_nest(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#1140: MSKSD_NESTED on a host whose KVM does not nest earns
    one warning naming the module option, since every workspace
    boots there without the feature and nothing else says so."""
    monkeypatch.setattr(main_mod, "host_nested_support", lambda: "off")
    with caplog.at_level(logging.INFO, logger="msks.server.main"):
        main_mod.nested_notice(Settings(vmm=VmmSettings(nested=True)))
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "nested=1" in caplog.records[0].getMessage()


def test_nested_notice_reports_the_on_and_unknown_answers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A nesting host gets the plain on line. An x86_64 host whose
    switch no read answered for gets the line that says so (the
    doctor's row and the daemon's line must agree about one
    host); a machine that publishes no switch gets the other
    unverified line. All at info."""
    cases = (
        ("on", True, "may run VMs"),
        ("unknown", True, "no kvm_intel or kvm_amd"),
        ("unknown", False, "publishes no KVM nested switch"),
    )
    for answer, x86, word in cases:
        caplog.clear()
        monkeypatch.setattr(
            main_mod, "host_nested_support", lambda a=answer: a
        )
        monkeypatch.setattr(main_mod, "linux_x86_64", lambda: x86)
        with caplog.at_level(logging.INFO, logger="msks.server.main"):
            main_mod.nested_notice(Settings(vmm=VmmSettings(nested=True)))
        assert [record.levelname for record in caplog.records] == ["INFO"]
        assert word in caplog.records[0].getMessage()


def test_main_logs_the_nested_notice_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """main() emits the notice once the settings load and before
    serve takes over: the setting came from the environment here,
    the same path a unit file's Environment= line takes."""
    monkeypatch.setenv("MSKSD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MSKSD_NESTED", "true")
    monkeypatch.setattr(main_mod, "host_nested_support", lambda: "on")
    seen = {}
    monkeypatch.setattr(
        main_mod, "serve", lambda app, no_tls: seen.update(no_tls=no_tls)
    )
    previous = signal.getsignal(signal.SIGHUP)
    try:
        with caplog.at_level(logging.INFO, logger="msks.server.main"):
            assert main([]) == 0
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert seen == {"no_tls": False}
    assert any(
        "nested virtualization on" in record.getMessage()
        for record in caplog.records
    )
