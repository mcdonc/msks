"""The msks client YAML config file: modes, precedence, aliases
(#314)."""

import os
import stat
from pathlib import Path

import httpx
import pytest
import yaml
from msks.client import cli, config
from msks.client.rest import DEFAULT_URL, env_token, env_url, ssl_context
from msks.server.tls import generate_ca

# The six connect/state variables the file can feed, plus the
# terminal launcher's — cleared before any test that lets the
# config (or its materialization) near the environment, so a
# materialized value never outlives the test that caused it.
CLIENT_VARS = (
    "MSKSC_URL",
    "MSKSC_TOKEN",
    "MSKSC_CAFILE",
    "MSKSC_EXPECTED_IMAGE",
    "MSKSC_CACHE_DIR",
    "MSKSC_DATA_DIR",
    "MSKSC_TERMINAL_OPEN_CMD",
)


def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every client variable the file layers under.

    Set to the empty string rather than deleted: empty is the
    unset form for every reader, and a recorded ``setenv`` restores
    the prior state on teardown even after the test's own
    ``apply``/``main`` materializes a real value into the variable
    (a bare ``delenv`` records nothing when the variable is
    already absent, so a materialized value would outlive the
    test).
    """
    for name in CLIENT_VARS:
        monkeypatch.setenv(name, "")


def write_config(root: Path, doc) -> str:
    """One config file under *root*; a dict is dumped, a str kept."""
    path = root / "msks.yaml"
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc))
    return str(path)


def token_file(root: Path, token: str = "filetok") -> str:
    path = root / "daemon.token"
    path.write_text(f"{token}\n")
    return str(path)


# --- the path and its three --config modes ---


def test_config_dir_reads_the_bootstrap_variables(monkeypatch) -> None:
    monkeypatch.setenv("MSKSC_CONFIG_DIR", "/cfg/msks")
    assert config.config_dir() == "/cfg/msks"
    monkeypatch.delenv("MSKSC_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert config.config_dir() == os.path.join("/xdg", "msks")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", "/home/op")
    assert config.config_dir() == os.path.join("/home/op/.config/msks")


def test_default_config_path_joins_the_filename(monkeypatch) -> None:
    monkeypatch.setenv("MSKSC_CONFIG_DIR", "/cfg/msks")
    assert config.default_config_path() == os.path.join(
        "/cfg/msks", "msks.yaml"
    )


def test_bare_msks_generates_the_template_on_first_run(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path / "cfg"))
    path = config.resolve_config_path(None)
    assert path == config.default_config_path()
    body = Path(path).read_text()
    assert body.startswith("# msks client configuration")
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE((tmp_path / "cfg").stat().st_mode) == 0o700
    # A template parses to no keys: nothing overrides anything.
    assert config.parse_config_doc(body, path) == {}
    # Second run finds the file and never rewrites it.
    Path(path).write_text("url: https://kept\n")
    assert config.resolve_config_path(None) == path
    assert Path(path).read_text() == "url: https://kept\n"


def test_generate_template_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "msks.yaml"
    path.write_text("operator's file\n")
    with pytest.raises(FileExistsError):
        config.generate_template(str(path))
    assert path.read_text() == "operator's file\n"


def test_concurrent_generation_is_the_file_being_there(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path))
    path = config.default_config_path()

    def race(_path: str) -> None:
        raise FileExistsError

    monkeypatch.setattr(config, "generate_template", race)
    # No file on disk: the generation "loses the race" and the
    # winner's file is treated as there.
    assert config.ensure_default_config() == str(path)


def test_explicit_config_path_reads_exactly_that_file(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path, "url: https://explicit\n")
    assert config.resolve_config_path(path) == path
    with pytest.raises(ValueError, match="config file not found"):
        config.resolve_config_path(str(tmp_path / "nope.yaml"))
    with pytest.raises(ValueError, match="config path is a directory"):
        config.resolve_config_path(str(tmp_path))


def test_config_none_is_the_env_only_opt_out() -> None:
    assert config.resolve_config_path("none") == config.NO_CONFIG


# --- parsing and validation ---


def test_empty_and_null_forms_are_unset(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        'url:\ncafile: ""\nexpected_image: none-set\n'
        "terminal_open_cmd: ''\n",
    )
    assert config.load_config(path) == {"expected_image": "none-set"}
    # The bare null form takes the same unset path.
    null_form = write_config(tmp_path, "terminal_open_cmd:\n")
    assert config.load_config(null_form) == {}


def test_a_non_mapping_document_is_refused(tmp_path: Path) -> None:
    path = write_config(tmp_path, "- one\n- two\n")
    with pytest.raises(ValueError, match="must be a mapping of keys"):
        config.load_config(path)


def test_invalid_yaml_names_the_parse_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, "url: [unclosed\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        config.load_config(path)


def test_non_string_keys_are_refused(tmp_path: Path) -> None:
    path = write_config(tmp_path, "1: https://lab\n")
    with pytest.raises(ValueError, match="config keys must be strings"):
        config.load_config(path)


def test_kebab_and_snake_spell_the_same_keys(tmp_path, monkeypatch) -> None:
    """Hyphens fold to underscores everywhere a key is read — the
    globals, ``active_daemon``, and a daemon entry's keys — so a
    klangk-style kebab file resolves identically to a snake one
    (#314)."""
    clean_env(monkeypatch)
    tokens = token_file(tmp_path)
    kebab = write_config(
        tmp_path,
        "expected-image: img:9\n"
        f"token-file: {tokens}\n"
        "cache-dir: /kebab-cache\n"
        "terminal-open-cmd: konsole -e\n"
        "active-daemon: lab\n"
        "daemons:\n"
        "  lab:\n"
        "    url: https://lab:8660\n"
        f"    token-file: {tokens}\n"
        "    expected-image: img:lab\n",
    )
    conf = config.resolve(None, kebab)
    assert conf.url == "https://lab:8660"
    assert conf.token == "filetok"
    assert conf.expected_image == "img:lab"
    assert conf.cache_dir == "/kebab-cache"
    assert conf.terminal_open_cmd == ["konsole", "-e"]


def test_both_spellings_of_one_key_are_refused(tmp_path: Path) -> None:
    """One key, both spellings, one file: the second is a duplicate
    whatever order they appear in, at the top level and inside a
    daemon entry."""
    both = write_config(tmp_path, "token_file: /a\ntoken-file: /b\n")
    with pytest.raises(ValueError, match="hyphens and underscores"):
        config.load_config(both)
    entry = write_config(
        tmp_path,
        "daemons:\n  lab:\n    url: https://lab\n"
        "    token-file: /a\n    token_file: /b\n",
    )
    with pytest.raises(ValueError, match="hyphens and underscores"):
        config.load_config(entry)


def test_unknown_kebab_key_echoes_the_written_spelling(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path, "toke-file: /a\n")
    with pytest.raises(ValueError, match="unknown config key 'toke-file'"):
        config.load_config(path)


def test_non_string_entry_keys_are_refused(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, "daemons:\n  lab:\n    url: https://lab\n    1: x\n"
    )
    with pytest.raises(ValueError, match="keys must be strings"):
        config.load_config(path)


def test_duplicate_keys_are_refused(tmp_path: Path) -> None:
    path = write_config(tmp_path, "url: https://a\nurl: https://b\n")
    with pytest.raises(ValueError, match="duplicate config key 'url'"):
        config.load_config(path)


def test_unknown_key_names_the_valid_ones(tmp_path: Path) -> None:
    path = write_config(tmp_path, "servers: {}\n")
    with pytest.raises(ValueError, match="unknown config key 'servers'"):
        config.load_config(path)


def test_scalar_keys_must_be_strings(tmp_path: Path) -> None:
    path = write_config(tmp_path, "url: 8660\n")
    with pytest.raises(ValueError, match="'url' must be a string, got int"):
        config.load_config(path)


def test_active_daemon_must_be_a_name(tmp_path: Path) -> None:
    path = write_config(tmp_path, "active_daemon: ''\n")
    with pytest.raises(ValueError, match="active_daemon must be a daemon"):
        config.load_config(path)
    path = write_config(tmp_path, "active_daemon: 7\n")
    with pytest.raises(ValueError, match="active_daemon must be a daemon"):
        config.load_config(path)


def test_daemons_section_shape(tmp_path: Path) -> None:
    doc = config.load_config(
        write_config(
            tmp_path,
            "daemons:\n"
            "  lab:\n"
            "    url: https://lab:8660\n"
            "    token_file: /t/lab.token\n"
            "    cafile: /t/lab-ca.pem\n"
            "    expected_image: img:2\n",
        )
    )
    assert doc["daemons"] == {
        "lab": {
            "url": "https://lab:8660",
            "token_file": "/t/lab.token",
            "cafile": "/t/lab-ca.pem",
            "expected_image": "img:2",
        }
    }


def test_daemons_must_be_a_mapping_of_entries(tmp_path: Path) -> None:
    path = write_config(tmp_path, "daemons: lab\n")
    with pytest.raises(ValueError, match="daemons must be a mapping"):
        config.load_config(path)
    path = write_config(tmp_path, "daemons:\n  lab: https://lab\n")
    with pytest.raises(ValueError, match="daemon 'lab' must be a mapping"):
        config.load_config(path)


def test_daemon_entries_carry_only_their_keys(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, "daemons:\n  lab:\n    url: https://lab\n    port: 1\n"
    )
    with pytest.raises(ValueError, match="unknown key 'port' in daemon 'lab'"):
        config.load_config(path)


def test_a_daemon_entry_needs_a_url(tmp_path: Path) -> None:
    for body in (
        "daemons:\n  lab:\n    cafile: /ca\n",
        "daemons:\n  lab:\n    url:\n",
    ):
        path = write_config(tmp_path, body)
        with pytest.raises(ValueError, match="daemon 'lab' needs a url"):
            config.load_config(path)


def test_daemon_aliases_must_be_names(tmp_path: Path) -> None:
    path = write_config(tmp_path, "daemons:\n  1:\n    url: https://lab\n")
    with pytest.raises(ValueError, match="daemon aliases must be names"):
        config.load_config(path)


def test_terminal_open_cmd_takes_both_forms(tmp_path: Path) -> None:
    doc = config.load_config(
        write_config(tmp_path, 'terminal_open_cmd: konsole -e "msks ssh"\n')
    )
    assert doc["terminal_open_cmd"] == ["konsole", "-e", "msks ssh"]
    doc = config.load_config(
        write_config(
            tmp_path,
            "terminal_open_cmd:\n  - alacritty\n  - -T\n  - msks ssh\n",
        )
    )
    assert doc["terminal_open_cmd"] == ["alacritty", "-T", "msks ssh"]


def test_terminal_open_cmd_refuses_junk(tmp_path: Path) -> None:
    for body, message in (
        ('terminal_open_cmd: konsole -e "\n', "terminal_open_cmd"),
        ("terminal_open_cmd: []\n", "must name a command"),
        ("terminal_open_cmd:\n  - konsole\n  - 3\n", "non-empty strings"),
        ("terminal_open_cmd:\n  - konsole\n  - ''\n", "non-empty strings"),
        ("terminal_open_cmd: 3\n", "string or a list of strings"),
    ):
        path = write_config(tmp_path, body)
        with pytest.raises(ValueError, match=message):
            config.load_config(path)


# --- daemon selection ---


def test_selection_without_flag_or_active_daemon(tmp_path: Path) -> None:
    doc = config.load_config(
        write_config(tmp_path, "daemons:\n  lab:\n    url: https://lab\n")
    )
    assert config.select_daemon(None, doc, "p") == config.Selection(
        None, False, None
    )


def test_active_daemon_picks_the_default_alias(tmp_path: Path) -> None:
    doc = config.load_config(
        write_config(
            tmp_path,
            "daemons:\n  lab:\n    url: https://lab\nactive_daemon: lab\n",
        )
    )
    assert config.select_daemon(None, doc, "p").alias == "lab"
    doc = config.load_config(write_config(tmp_path, "active_daemon: ghost\n"))
    with pytest.raises(ValueError, match="active_daemon 'ghost' is not"):
        config.select_daemon(None, doc, "p")


def test_the_flag_takes_an_alias_or_a_raw_url(tmp_path: Path) -> None:
    doc = config.load_config(
        write_config(tmp_path, "daemons:\n  lab:\n    url: https://lab\n")
    )
    assert config.select_daemon("lab", doc, "p") == config.Selection(
        "lab", True, None
    )
    assert config.select_daemon("http://raw:1", doc, "p") == config.Selection(
        None, True, "http://raw:1"
    )
    with pytest.raises(ValueError, match="unknown daemon 'ghost'"):
        config.select_daemon("ghost", doc, "p")


def test_unknown_daemon_with_no_aliases_names_that(tmp_path) -> None:
    with pytest.raises(ValueError, match=r"defined: none"):
        config.select_daemon("ghost", {}, "p")


# --- the precedence layers ---


def layered_tree(root: Path) -> str:
    tokens = token_file(root)
    return write_config(
        root,
        "url: https://global:8660/\n"
        f"token_file: {tokens}\n"
        "cafile: /global-ca.pem\n"
        "expected_image: img:global\n"
        "cache_dir: /global-cache\n"
        "data_dir: /global-data\n"
        "daemons:\n"
        "  lab:\n"
        "    url: https://lab:8660/\n"
        f"    token_file: {tokens}\n"
        "    cafile: /lab-ca.pem\n"
        f"    expected_image: img:lab\n",
    )


def test_flag_alias_beats_the_environment(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://env:8660")
    monkeypatch.setenv("MSKSC_TOKEN", "envtok")
    conf = config.resolve("lab", layered_tree(tmp_path))
    # The flag's entry keys outrank the exports for this invocation;
    # its absent keys still fall through to the environment.
    assert conf.url == "https://lab:8660"
    assert conf.token == "filetok"
    assert conf.cafile == "/lab-ca.pem"
    assert conf.expected_image == "img:lab"
    assert conf.daemon == "lab"
    assert conf.env_layer["MSKSC_URL"] == "https://lab:8660"
    assert conf.env_layer["MSKSC_TOKEN"] == "filetok"


def test_environment_beats_the_ambient_alias_and_globals(
    tmp_path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://env:8660")
    monkeypatch.setenv("MSKSC_TOKEN", "envtok")
    monkeypatch.setenv("MSKSC_CACHE_DIR", "/env-cache")
    monkeypatch.setenv("MSKSC_DATA_DIR", "/env-data")
    path = layered_tree(tmp_path)
    path = write_config(
        tmp_path,
        Path(path).read_text() + "active_daemon: lab\n",
    )
    conf = config.resolve(None, path)
    assert conf.url == "https://env:8660"
    assert conf.token == "envtok"
    assert conf.cache_dir == "/env-cache"
    assert conf.data_dir == "/env-data"
    # The environment won nothing from the file: no materialization.
    assert "MSKSC_URL" not in conf.env_layer
    assert "MSKSC_TOKEN" not in conf.env_layer
    assert "MSKSC_CACHE_DIR" not in conf.env_layer


def test_ambient_alias_beats_the_global_keys(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    path = layered_tree(tmp_path)
    path = write_config(
        tmp_path,
        Path(path).read_text() + "active_daemon: lab\n",
    )
    conf = config.resolve(None, path)
    assert conf.url == "https://lab:8660"
    assert conf.cafile == "/lab-ca.pem"
    assert conf.expected_image == "img:lab"
    # Globals still answer for the keys the entry leaves out.
    assert conf.cache_dir == "/global-cache"
    assert conf.data_dir == "/global-data"


def test_global_keys_answer_when_nothing_else_does(
    tmp_path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    conf = config.resolve(None, layered_tree(tmp_path))
    assert conf.url == "https://global:8660"
    assert conf.cafile == "/global-ca.pem"
    assert conf.expected_image == "img:global"
    assert conf.env_layer == {
        "MSKSC_URL": "https://global:8660",
        "MSKSC_TOKEN": "filetok",
        "MSKSC_CAFILE": "/global-ca.pem",
        "MSKSC_EXPECTED_IMAGE": "img:global",
        "MSKSC_CACHE_DIR": "/global-cache",
        "MSKSC_DATA_DIR": "/global-data",
    }


def test_defaults_hold_when_nothing_provides_a_value(monkeypatch) -> None:
    clean_env(monkeypatch)
    conf = config.resolve(None, "none")
    assert conf.url == DEFAULT_URL
    assert conf.token is None
    assert conf.cafile == ""
    assert conf.expected_image == ""
    assert conf.cache_dir is None
    # The launcher's floor is xterm -e: the one terminal most
    # Linuxes carry, so the new-window path needs no configuration
    # (#314).
    assert list(config.DEFAULT_TERMINAL_CMD) == ["xterm", "-e"]
    assert conf.terminal_open_cmd == ["xterm", "-e"]
    assert conf.env_layer == {}


def test_raw_url_flag_beats_the_environment(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://env:8660")
    conf = config.resolve("https://raw:8660/", layered_tree(tmp_path))
    assert conf.url == "https://raw:8660"
    assert conf.env_layer["MSKSC_URL"] == "https://raw:8660"


def test_resolve_records_the_invocation_flags(monkeypatch) -> None:
    """The raw --daemon/--config values ride the resolution (#341):
    a spawned console child repeats them to reach the same daemon —
    the resolution's winners are not enough, because a flag's
    choice outranks the environment without landing in it."""
    clean_env(monkeypatch)
    bare = config.resolve(None, "none")
    assert bare.daemon_arg is None
    assert bare.config_arg == "none"
    flagged = config.resolve("https://raw:8660", "none")
    assert flagged.daemon_arg == "https://raw:8660"
    assert flagged.config_arg == "none"


# --- the token file ---


def test_the_token_comes_from_the_file_stripped(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    path = write_config(
        tmp_path,
        f"url: https://lab\ntoken_file: {token_file(tmp_path, ' ziptok ')}\n",
    )
    conf = config.resolve(None, path)
    assert conf.token == "ziptok"


def test_an_unreadable_token_file_names_both_places(
    tmp_path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    path = write_config(
        tmp_path, "url: https://lab\ntoken_file: /nope.token\n"
    )
    with pytest.raises(ValueError, match="token_file"):
        config.resolve(None, path)
    empty = tmp_path / "empty.token"
    empty.write_text("  \n")
    path = write_config(tmp_path, f"url: https://lab\ntoken_file: {empty}\n")
    with pytest.raises(ValueError, match="MSKSC_TOKEN"):
        config.resolve(None, path)


def test_config_read_and_write_failures_are_one_line(
    tmp_path: Path, monkeypatch
) -> None:
    """The bootstrap contract: an unreadable config file and an
    unwritable first-run template are one clean line each, not
    tracebacks."""
    unreadable = write_config(tmp_path, "url: https://lab\n")
    os.chmod(unreadable, 0o000)
    with pytest.raises(ValueError, match="cannot read config file"):
        config.load_config(unreadable)
    readonly = tmp_path / "ro-cfg"
    readonly.mkdir()
    readonly.chmod(0o555)
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(readonly))
    with pytest.raises(ValueError, match="cannot write the first-run"):
        config.resolve_config_path(None)


def test_a_missing_token_errors_before_network_activity(
    monkeypatch,
) -> None:
    clean_env(monkeypatch)
    with pytest.raises(SystemExit, match="token_file"):
        env_token()


def test_the_cafile_expands_a_leading_tilde(
    tmp_path: Path, monkeypatch
) -> None:
    """A home-relative cafile — the spelling the docs and the
    template's examples ship — verifies instead of dying on a
    literal ``~/`` path."""
    ca_cert, _ = generate_ca()
    (tmp_path / "ca.pem").write_bytes(ca_cert)
    clean_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MSKSC_CAFILE", "~/ca.pem")
    assert ssl_context().verify_mode.name == "CERT_REQUIRED"


def test_an_empty_url_export_falls_to_the_default(monkeypatch) -> None:
    """Empty is the unset form for the URL too — the reader and the
    resolver agree on the default, never a "" base URL."""
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "")
    assert env_url() == DEFAULT_URL
    assert config.resolve(None, "none").url == DEFAULT_URL


def test_help_screens_skip_the_config_bootstrap(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks <cmd> --help`` answers with help even when the config
    file is broken — the operator's most discoverable tool stays
    available exactly when it is needed."""
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path))
    write_config(tmp_path, "bogus_key: 1\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["ls", "--help"])
    assert exc.value.code == 0
    assert "List workspaces" in capsys.readouterr().out


# --- terminal_open_cmd ---


def test_terminal_command_prefers_the_variable(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    path = write_config(tmp_path, "terminal_open_cmd: konsole -e\n")
    assert config.resolve(None, path).terminal_open_cmd == ["konsole", "-e"]
    monkeypatch.setenv("MSKSC_TERMINAL_OPEN_CMD", "alacritty -T win -e")
    conf = config.resolve(None, path)
    assert conf.terminal_open_cmd == ["alacritty", "-T", "win", "-e"]
    with pytest.raises(ValueError, match="MSKSC_TERMINAL_OPEN_CMD"):
        monkeypatch.setenv("MSKSC_TERMINAL_OPEN_CMD", 'wezterm "')
        config.resolve(None, path)


# --- materialization and the CLI entry ---


def test_apply_writes_the_file_derived_winners(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    conf = config.resolve(None, layered_tree(tmp_path))
    config.apply(conf)
    assert os.environ["MSKSC_URL"] == "https://global:8660"
    assert os.environ["MSKSC_TOKEN"] == "filetok"
    assert os.environ["MSKSC_CAFILE"] == "/global-ca.pem"
    assert os.environ["MSKSC_EXPECTED_IMAGE"] == "img:global"
    assert os.environ["MSKSC_CACHE_DIR"] == "/global-cache"
    assert os.environ["MSKSC_DATA_DIR"] == "/global-data"


def test_bootstrap_maps_config_errors_to_one_line(
    tmp_path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    bad = write_config(tmp_path, "servers: {}\n")
    with pytest.raises(SystemExit, match="unknown config key 'servers'"):
        config.bootstrap(None, bad)
    with pytest.raises(SystemExit, match="msks: config file not found"):
        config.bootstrap(None, str(tmp_path / "nope.yaml"))


def listing_transport(seen: dict) -> httpx.MockTransport:
    """A transport that records the URL it served and answers a
    workspace listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    return httpx.MockTransport(handler)


def test_the_flag_reaches_the_rest_call(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path))
    write_config(
        tmp_path,
        "daemons:\n"
        "  lab:\n"
        "    url: https://lab.example:8660/\n"
        f"    token_file: {token_file(tmp_path)}\n",
    )
    seen: dict = {}
    code = cli.main(["--daemon", "lab", "ls"], listing_transport(seen))
    assert code == 0
    assert seen["url"].startswith("https://lab.example:8660/")
    assert seen["auth"] == "Bearer filetok"


def test_bare_invocation_generates_and_uses_the_template(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MSKSC_URL", "https://preset:8660")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    # The devenv-presets shape: exported variables beside a missing
    # file keep working unchanged, and the first run generates the
    # template beside them.
    seen: dict = {}
    code = cli.main(["ls"], listing_transport(seen))
    assert code == 0
    assert seen["url"].startswith("https://preset:8660/")
    assert (tmp_path / "msks.yaml").is_file()


def test_config_none_ignores_the_file_tree(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path))
    write_config(tmp_path, "url: https://file:8660\n")
    monkeypatch.setenv("MSKSC_URL", "https://env:8660")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    seen: dict = {}
    code = cli.main(["--config=none", "ls"], listing_transport(seen))
    assert code == 0
    assert seen["url"].startswith("https://env:8660/")


def test_an_unknown_flag_daemon_exits_with_one_line(
    tmp_path, monkeypatch, capsys
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path))
    write_config(tmp_path, "daemons:\n  lab:\n    url: https://lab\n")
    with pytest.raises(SystemExit, match="unknown daemon 'ghost'"):
        cli.main(["--daemon", "ghost", "ls"], listing_transport({}))
