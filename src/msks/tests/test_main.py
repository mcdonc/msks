"""Entry point and server-config wiring."""

import signal

import msks.server.main as main_mod
import pytest
from msks.app import build_app
from msks.server.main import main, server_config, ssl_paths
from msks.settings import ServerSettings, Settings

from msks import __version__


def app_with(server: ServerSettings):
    return build_app(Settings(server=server))


def test_ssl_paths_none_when_unconfigured() -> None:
    app = app_with(ServerSettings())
    assert ssl_paths(app) == (None, None)


def test_ssl_paths_operator() -> None:
    app = app_with(ServerSettings(tls_cert="/c.pem", tls_key="/k.pem"))
    assert ssl_paths(app) == ("/c.pem", "/k.pem")


def test_server_config_plain_and_tls() -> None:
    plain = server_config(app_with(ServerSettings(host="127.0.0.1", port=8999)))
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
    seen = {}
    monkeypatch.setattr(
        main_mod, "serve", lambda app, no_tls: seen.update(no_tls=no_tls)
    )
    assert main([]) == 0
    assert seen == {"no_tls": False}
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


def test_serve_operator_certs_skip_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(main_mod, "run_forever", lambda app: None)
    app = app_with(
        ServerSettings(
            db_path=tmp_path / "x.db", tls_cert="/op/c.pem", tls_key="/op/k.pem"
        )
    )
    main_mod.serve(app, no_tls=False)
    assert app.state.settings.server.tls_cert == "/op/c.pem"
