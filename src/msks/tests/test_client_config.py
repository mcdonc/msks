"""The msks client YAML config file: modes, precedence, aliases
(#314)."""

import os
import stat
from pathlib import Path

import httpx
import pytest
import yaml
from msks.client import cli, config
from msks.client.env import env_token, env_url, ssl_context
from msks.server.tls import generate_ca

# The connect/state variables the file can feed, plus the
# terminal launcher's — cleared before any test that lets the
# config (or its materialization) near the environment, so a
# materialized value never outlives the test that caused it.
CLIENT_VARS = (
    "MSKSC_URL",
    "MSKSC_SOCKET",
    "MSKSC_TOKEN",
    "MSKSC_CAFILE",
    "MSKSC_EXPECTED_IMAGE",
    "MSKSC_CACHE_DIR",
    "MSKSC_STATE_DIR",
    "MSKSC_IDENTITY_FILE",
    "MSKSC_TERMINAL_OPEN_CMD",
    "MSKSC_SSH_OPTIONS",
    "MSKSC_SCREENSHOT_COMMAND",
    "MSKSC_SCREENSHOT_PORT",
    # The empty value is the "no system file" form (#1224): every
    # test that clears the client variables reads the user file
    # alone, and the layer tests set the variable themselves.
    "MSKSC_SYSTEM_CONFIG",
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


def test_ssh_options_takes_both_forms(tmp_path: Path) -> None:
    """The remembered passthrough (#385): string form shell-split,
    list form verbatim — flag tokens like ``-A`` stay whole."""
    doc = config.load_config(
        write_config(tmp_path, "ssh_options: -A -o ServerAliveInterval=30\n")
    )
    assert doc["ssh_options"] == ["-A", "-o", "ServerAliveInterval=30"]
    doc = config.load_config(
        write_config(
            tmp_path,
            "ssh_options:\n  - -A\n  - -o\n  - ServerAliveInterval=30\n",
        )
    )
    assert doc["ssh_options"] == ["-A", "-o", "ServerAliveInterval=30"]


def test_ssh_options_unset_and_refused_forms(tmp_path: Path) -> None:
    # Null, blank, and the empty list are the unset form: no
    # options ride. The -- separator is refused: msks appends the
    # options to its own, so a remembered separator would hand ssh
    # msks's transport as the remote command.
    for body in ("ssh_options:\n", "ssh_options: ''\n", "ssh_options: []\n"):
        assert config.load_config(write_config(tmp_path, body)) == {}
    for body, message in (
        ('ssh_options: -o "ForwardAgent=yes\n', "ssh_options"),
        ("ssh_options:\n  - -A\n  - 3\n", "non-empty strings"),
        ("ssh_options:\n  - -A\n  - ''\n", "non-empty strings"),
        ("ssh_options: 3\n", "string or a list of strings"),
        ("ssh_options: -- -A\n", "separator"),
        ("ssh_options:\n  - --\n", "separator"),
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
        "identity_file: /global-key\n"
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
    path = layered_tree(tmp_path)
    path = write_config(
        tmp_path,
        Path(path).read_text() + "active_daemon: lab\n",
    )
    conf = config.resolve(None, path)
    assert conf.url == "https://env:8660"
    assert conf.token == "envtok"
    assert conf.cache_dir == "/env-cache"
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
        "MSKSC_SOCKET": "",
        "MSKSC_TOKEN": "filetok",
        "MSKSC_CAFILE": "/global-ca.pem",
        "MSKSC_EXPECTED_IMAGE": "img:global",
        "MSKSC_CACHE_DIR": "/global-cache",
        "MSKSC_IDENTITY_FILE": "/global-key",
    }


def test_defaults_hold_when_nothing_provides_a_value(monkeypatch) -> None:
    clean_env(monkeypatch)
    conf = config.resolve(None, "none")
    assert conf.url == ""
    assert conf.socket == ""
    assert conf.token is None
    assert conf.cafile == ""
    assert conf.expected_image == ""
    assert conf.cache_dir is None
    assert conf.identity_file is None
    # An unconfigured launcher is None (#562): the TUI's shell
    # action runs in the tree's own terminal; a host that wants
    # windows names a launcher in the file or the environment.
    assert conf.terminal_open_cmd is None
    assert conf.env_layer == {}


def test_raw_url_flag_beats_the_environment(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://env:8660")
    conf = config.resolve("https://raw:8660/", layered_tree(tmp_path))
    assert conf.url == "https://raw:8660"
    assert conf.env_layer["MSKSC_URL"] == "https://raw:8660"


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
    monkeypatch.setenv("MSKSC_URL", "https://daemon:8660")
    with pytest.raises(SystemExit, match="token_file"):
        env_token()


def test_an_unparsable_cafile_is_the_named_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """A CA file that holds no certificate — the torn write the
    macOS setup tool can leave behind (#732, #733) — refuses with
    one named line instead of the ssl traceback the TUI's startup
    raised before."""
    bad = tmp_path / "ca.pem"
    bad.write_text("-----BEGIN CERTIFICATE-----x\n")
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_CAFILE", str(bad))
    with pytest.raises(SystemExit, match="holds no parsable"):
        ssl_context()


def test_a_missing_cafile_is_the_named_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """A cafile naming a file that is not there takes the same
    named refusal — the recovery line names the daemon's CA either
    way."""
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_CAFILE", str(tmp_path / "nope.pem"))
    with pytest.raises(SystemExit, match="is missing or holds"):
        ssl_context()


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


def test_an_empty_url_export_is_the_unset_form(monkeypatch) -> None:
    """Empty is the unset form for the URL too — the reader and the
    resolver agree, never a "" base URL: an empty URL export leaves
    the socket layer free to answer (#555)."""
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "")
    monkeypatch.setenv("MSKSC_SOCKET", "/run/msks/msksd.sock")
    assert env_url() == "http+unix://%2Frun%2Fmsks%2Fmsksd.sock"
    conf = config.resolve(None, "none")
    assert conf.url == ""
    assert conf.socket == "/run/msks/msksd.sock"


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


# --- state_dir ---


def test_state_dir_resolves_the_global_tier(tmp_path, monkeypatch) -> None:
    """The session-record root (#858) resolves the way every global
    key does: the variable over the file, the file over none — and
    only the file's winner is materialized, so every ``msks ssh``
    child reads it from the environment either way."""
    clean_env(monkeypatch)
    path = write_config(tmp_path, "state_dir: /state/root\n")
    conf = config.resolve(None, path)
    assert conf.state_dir == "/state/root"
    assert conf.env_layer["MSKSC_STATE_DIR"] == "/state/root"
    monkeypatch.setenv("MSKSC_STATE_DIR", "/env/root")
    conf = config.resolve(None, path)
    assert conf.state_dir == "/env/root"
    assert "MSKSC_STATE_DIR" not in conf.env_layer


def test_no_state_dir_answers_none(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    assert config.resolve(None, "none").state_dir is None


# --- ssh_options ---


def test_ssh_options_prefer_the_variable(tmp_path, monkeypatch) -> None:
    """The remembered passthrough resolves the way every file key
    does: the variable's string form over the file's value, the
    file's value over none (#385) — and only the winner the file
    provided is materialized."""
    clean_env(monkeypatch)
    path = write_config(tmp_path, "ssh_options: -A -o User=root\n")
    assert config.resolve(None, path).ssh_options == ["-A", "-o", "User=root"]
    monkeypatch.setenv("MSKSC_SSH_OPTIONS", "-T")
    conf = config.resolve(None, path)
    assert conf.ssh_options == ["-T"]
    assert "MSKSC_SSH_OPTIONS" not in conf.env_layer
    with pytest.raises(ValueError, match="MSKSC_SSH_OPTIONS"):
        monkeypatch.setenv("MSKSC_SSH_OPTIONS", '-o "ForwardAgent=yes')
        config.resolve(None, path)


def test_no_ssh_options_answer_none(tmp_path, monkeypatch) -> None:
    clean_env(monkeypatch)
    conf = config.resolve(None, write_config(tmp_path, "url: https://x\n"))
    assert conf.ssh_options == []
    assert "MSKSC_SSH_OPTIONS" not in conf.env_layer


def test_apply_materializes_the_file_derived_options(
    tmp_path, monkeypatch
) -> None:
    """The reader lives deep in the ssh and rsync call paths, so the
    file's winner reaches it the substrate every other file value
    rides: the environment, shell-joined — a join/split round trip
    that keeps a token's own quoting intact."""
    clean_env(monkeypatch)
    path = write_config(
        tmp_path,
        "ssh_options:\n  - -o\n  - 'ForwardAgent=\"/a b\"'\n  - -A\n",
    )
    conf = config.resolve(None, path)
    config.apply(conf)
    import shlex

    assert shlex.split(os.environ["MSKSC_SSH_OPTIONS"]) == [
        "-o",
        'ForwardAgent="/a b"',
        "-A",
    ]


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
    assert os.environ["MSKSC_IDENTITY_FILE"] == "/global-key"


def test_identity_file_is_global_only(tmp_path, monkeypatch) -> None:
    """identity_file names the operator's own private key (#336):
    a scalar global key whose variable carries the same shape (a
    path, ~ expanded), the environment overriding the file, the
    empty forms unset — and a per-alias identity_file is refused:
    an identity belongs to the operator, not to a daemon
    connection."""
    clean_env(monkeypatch)
    path = write_config(tmp_path, "identity_file: ~/.ssh/id_ed25519\n")
    # The file stores the path as written (~ expansion is the
    # reader's job, the token_file pattern — HOME moves, the config
    # does not).
    assert config.resolve(None, path).identity_file == "~/.ssh/id_ed25519"
    assert config.resolve(None, path).env_layer["MSKSC_IDENTITY_FILE"] == (
        "~/.ssh/id_ed25519"
    )
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", "/env-key")
    conf = config.resolve(None, path)
    assert conf.identity_file == "/env-key"
    assert "MSKSC_IDENTITY_FILE" not in conf.env_layer
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", "")
    unset = write_config(tmp_path, "identity-file:\n")
    assert config.resolve(None, unset).identity_file is None
    alias = write_config(
        tmp_path,
        "daemons:\n"
        "  lab:\n"
        "    url: https://lab:8660\n"
        "    identity_file: /lab-key\n",
    )
    with pytest.raises(ValueError, match="unknown key 'identity_file'"):
        config.resolve(None, alias)


def test_pubkey_file_is_global_only(tmp_path, monkeypatch) -> None:
    """pubkey_file names the operator's key's public half (#495):
    the same scalar-global shape identity_file carries — the file
    value filed into the layer the bootstrap materializes, the
    variable overriding the file, the empty forms unset, and a
    per-alias pubkey_file refused: an identity belongs to the
    operator, not to a daemon connection."""
    clean_env(monkeypatch)
    path = write_config(tmp_path, "pubkey_file: ~/.ssh/id_ed25519.pub\n")
    conf = config.resolve(None, path)
    assert conf.pubkey_file == "~/.ssh/id_ed25519.pub"
    assert conf.env_layer["MSKSC_PUBKEY_FILE"] == "~/.ssh/id_ed25519.pub"
    monkeypatch.setenv("MSKSC_PUBKEY_FILE", "/env.pub")
    conf = config.resolve(None, path)
    assert conf.pubkey_file == "/env.pub"
    assert "MSKSC_PUBKEY_FILE" not in conf.env_layer
    monkeypatch.setenv("MSKSC_PUBKEY_FILE", "")
    unset = write_config(tmp_path, "pubkey-file:\n")
    assert config.resolve(None, unset).pubkey_file is None
    alias = write_config(
        tmp_path,
        "daemons:\n"
        "  lab:\n"
        "    url: https://lab:8660\n"
        "    pubkey_file: /lab.pub\n",
    )
    with pytest.raises(ValueError, match="unknown key 'pubkey_file'"):
        config.resolve(None, alias)


def test_the_template_documents_pubkey_file() -> None:
    """The first-run template shows the key (#495) beside
    identity_file's example, commented like every other one."""
    assert "# pubkey_file:" in config.render_template()


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


# --- the socket layer (#555) ---------------------------------------------


def write_entry_config(tmp_path: Path, entry: str) -> str:
    return write_config(tmp_path, f"daemons:\n{entry}")


def test_an_entry_needs_one_of_url_or_socket(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs a url or a socket"):
        config.load_config(write_entry_config(tmp_path, "  bare: {}\n"))


def test_an_entry_refuses_both_url_and_socket(tmp_path: Path) -> None:
    body = "  both:\n    url: https://d\n    socket: /run/s\n"
    with pytest.raises(ValueError, match="both a url and a socket"):
        config.load_config(write_entry_config(tmp_path, body))


def test_a_socket_entry_resolves_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_env(monkeypatch)
    conf = config.resolve(
        "local", write_entry_config(tmp_path, "  local:\n    socket: /run/s\n")
    )
    assert conf.url == ""
    assert conf.socket == "/run/s"
    assert conf.token is None
    assert conf.env_layer == {
        "MSKSC_SOCKET": "/run/s",
        "MSKSC_URL": "",  # the exclusive blank: the loser's dial is off
    }


def test_an_active_socket_entry_materializes_the_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_env(monkeypatch)
    path = write_entry_config(tmp_path, "  local:\n    socket: /run/s\n")
    path = write_config(
        tmp_path,
        "active_daemon: local\ndaemons:\n  local:\n    socket: /run/s\n",
    )
    config.apply(config.resolve(None, path))
    assert os.environ["MSKSC_SOCKET"] == "/run/s"


def test_the_flag_takes_an_absolute_socket_path(tmp_path: Path) -> None:
    doc = config.load_config(
        write_entry_config(tmp_path, "  local:\n    socket: /run/s\n")
    )
    assert config.select_daemon("/run/other.sock", doc, "p") == (
        config.Selection(None, True, None, "/run/other.sock")
    )


def test_a_flagged_socket_entry_beats_the_environment_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://env:8660")
    conf = config.resolve(
        "local", write_entry_config(tmp_path, "  local:\n    socket: /run/s\n")
    )
    assert conf.url == ""
    assert conf.socket == "/run/s"


def test_the_environment_url_beats_the_environment_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://env:8660")
    monkeypatch.setenv("MSKSC_SOCKET", "/run/s")
    conf = config.resolve(None, "none")
    assert conf.url == "https://env:8660"
    assert conf.socket == ""


def test_a_socket_path_flag_resolves_to_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_env(monkeypatch)
    conf = config.resolve("/run/flagged.sock", "none")
    assert conf.url == ""
    assert conf.socket == "/run/flagged.sock"
    assert conf.env_layer == {
        "MSKSC_SOCKET": "/run/flagged.sock",
        "MSKSC_URL": "",
    }


def test_a_flagged_socket_entry_dials_the_socket_not_the_env_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag's pick is exclusive (#555 review): the ambient
    MSKSC_URL — the devenv shell presets one always — must not
    take the dial back to TCP."""
    from msks.client import env

    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://preset:8660")
    path = write_config(tmp_path, "daemons:\n  local:\n    socket: /run/s\n")
    config.apply(config.resolve("local", path))
    assert env.env_url() == env.unix_url("/run/s")


def test_an_active_socket_entry_does_not_reach_past_the_env_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """active_daemon is a file-tier pick (#314 precedence): an
    exported MSKSC_URL outranks it — the flag alone reaches past
    the environment (#555 review)."""
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_URL", "https://preset:8660")
    path = write_config(
        tmp_path,
        "active_daemon: local\ndaemons:\n  local:\n    socket: /run/s\n",
    )
    conf = config.resolve(None, path)
    assert conf.url == "https://preset:8660"
    assert conf.socket == ""


def test_the_environment_cafile_beats_the_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A scalar connect key's env form wins over the file's value
    and is never materialized (it already lives where the readers
    look)."""
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_CAFILE", "/env-ca.pem")
    path = write_config(tmp_path, "cafile: /file-ca.pem\n")
    conf = config.resolve(None, path)
    assert conf.cafile == "/env-ca.pem"
    assert "MSKSC_CAFILE" not in conf.env_layer


def test_the_unconfigured_refusal_names_every_candidate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The refusal lists every path the client checks: its own
    runtime root's socket and the system deployment's
    /run/msks/msksd.sock (#1080) — the system candidate stands
    alone when no runtime dir exists, so the message never has
    an empty looked-for list."""
    from msks.client.env import LOOKED_FOR, looked_for

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    assert looked_for() == LOOKED_FOR.format(
        candidates=("/run/user/4242/msks/msksd.sock, /run/msks/msksd.sock")
    )
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert looked_for() == LOOKED_FOR.format(candidates="/run/msks/msksd.sock")


def test_the_image_manifest_url_resolves_in_tier_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#565: the channel's manifest address — the file's value,
    the environment's, then the default (this repository's latest
    release)."""
    path = write_config(
        tmp_path, "image_manifest_url: https://fork.example/images.json\n"
    )
    conf = config.resolve(None, path)
    assert conf.image_manifest_url == "https://fork.example/images.json"
    monkeypatch.setenv(
        "MSKSC_IMAGE_MANIFEST_URL", "https://env.example/images.json"
    )
    conf = config.resolve(None, path)
    assert conf.image_manifest_url == "https://env.example/images.json"
    monkeypatch.delenv("MSKSC_IMAGE_MANIFEST_URL")
    bare = write_config(tmp_path, "url: https://d:8660\n")
    assert config.resolve(None, bare).image_manifest_url == (
        "https://github.com/mcdonc/msks/releases/latest/download/images.json"
    )


# --- the screenshot pipe's keys (#1194) ---


def test_screenshot_command_takes_both_forms(tmp_path: Path) -> None:
    """The capture command: string form shell-split, list form
    verbatim — the command's own argv either way."""
    doc = config.load_config(
        write_config(tmp_path, "screenshot_command: import -crop 1x1+0+0\n")
    )
    assert doc["screenshot_command"] == ["import", "-crop", "1x1+0+0"]
    doc = config.load_config(
        write_config(
            tmp_path,
            "screenshot_command:\n  - import\n  - -crop\n  - 1x1+0+0\n",
        )
    )
    assert doc["screenshot_command"] == ["import", "-crop", "1x1+0+0"]


def test_screenshot_command_unset_and_refused_forms(
    tmp_path: Path,
) -> None:
    # Null, blank, and the empty list are the unset form: the pipe
    # stays down. A `--` token is legitimate command argv, so
    # unlike ssh_options it is kept.
    for body in (
        "screenshot_command:\n",
        "screenshot_command: ''\n",
        "screenshot_command: []\n",
    ):
        assert config.load_config(write_config(tmp_path, body)) == {}
    doc = config.load_config(
        write_config(tmp_path, "screenshot_command: import -- all\n")
    )
    assert doc["screenshot_command"] == ["import", "--", "all"]
    for body, message in (
        ('screenshot_command: import "unclosed\n', "screenshot_command"),
        ("screenshot_command:\n  - import\n  - 3\n", "non-empty strings"),
        ("screenshot_command:\n  - import\n  - ''\n", "non-empty strings"),
        ("screenshot_command: 3\n", "string or a list of strings"),
    ):
        path = write_config(tmp_path, body)
        with pytest.raises(ValueError, match=message):
            config.load_config(path)


@pytest.mark.parametrize(
    ("body", "port"),
    [
        ("screenshot_port: 8662\n", 8662),
        ("screenshot_port: '9000'\n", 9000),
        ("screenshot_port: 1\n", 1),
    ],
)
def test_screenshot_port_takes_whole_numbers(
    tmp_path: Path, body: str, port: int
) -> None:
    assert config.load_config(write_config(tmp_path, body)) == {
        "screenshot_port": port
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("screenshot_port: 0\n", "out of range"),
        ("screenshot_port: 65536\n", "out of range"),
        ("screenshot_port: true\n", "got bool"),
        ("screenshot_port: x\n", "port number"),
        ("screenshot_port: 8.5\n", "port number"),
    ],
)
def test_screenshot_port_refuses_the_rest(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        config.load_config(write_config(tmp_path, body))


def test_screenshot_port_unset_forms_drop_out(tmp_path: Path) -> None:
    for body in ("screenshot_port:\n", "screenshot_port: ''\n"):
        assert config.load_config(write_config(tmp_path, body)) == {}


def test_a_daemon_entry_refuses_the_screenshot_keys(
    tmp_path: Path,
) -> None:
    """Both keys are global-only: the screen the pipe captures is
    the operator's desktop, not a daemon connection's."""
    path = write_config(
        tmp_path,
        "daemons:\n  lab:\n    url: https://lab:8660\n"
        "    screenshot_command: import\n",
    )
    with pytest.raises(ValueError, match="screenshot_command"):
        config.load_config(path)


def test_resolve_layers_the_screenshot_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The file's values resolve onto the config and materialize
    into the environment the holder's children inherit; the
    environment's own values win and are left as they stand."""
    clean_env(monkeypatch)
    path = write_config(
        tmp_path,
        "url: https://daemon\nscreenshot_command: import -crop 1x1\n"
        "screenshot_port: 9001\n",
    )
    conf = config.resolve(config=path)
    assert conf.screenshot_command == ["import", "-crop", "1x1"]
    assert conf.screenshot_port == 9001
    assert conf.env_layer["MSKSC_SCREENSHOT_COMMAND"] == "import -crop 1x1"
    assert conf.env_layer["MSKSC_SCREENSHOT_PORT"] == "9001"

    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "spectacle -b")
    monkeypatch.setenv("MSKSC_SCREENSHOT_PORT", "9002")
    conf = config.resolve(config=path)
    assert conf.screenshot_command == ["spectacle", "-b"]
    assert conf.screenshot_port == 9002
    assert "MSKSC_SCREENSHOT_COMMAND" not in conf.env_layer
    assert "MSKSC_SCREENSHOT_PORT" not in conf.env_layer


def test_resolve_refuses_an_invalid_port_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env(monkeypatch)
    path = write_config(tmp_path, "url: https://daemon\n")
    for raw in ("junk", "65536", "0"):
        monkeypatch.setenv("MSKSC_SCREENSHOT_PORT", raw)
        with pytest.raises(ValueError, match="MSKSC_SCREENSHOT_PORT"):
            config.resolve(config=path)


# --- the dictation keys (#1214) ------------------------------------------


@pytest.mark.parametrize(
    ("body", "doc"),
    [
        (
            "dictate_model: small\ndictate_device: scarlett\n"
            "dictate_prompt: msks, devenv\n",
            {
                "dictate_model": "small",
                "dictate_device": "scarlett",
                "dictate_prompt": "msks, devenv",
            },
        ),
        ("dictate-model: base.en\n", {"dictate_model": "base.en"}),
    ],
)
def test_dictation_keys_read_both_spellings(
    tmp_path: Path, body: str, doc: dict
) -> None:
    assert config.load_config(write_config(tmp_path, body)) == doc


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("dictate_model: 7\n", "must be a string"),
        ("dictate_prompt: [a, b]\n", "must be a string"),
        # The gate's numbers are not keys (#1214): the leaf's own.
        ("dictate_threshold: -60\n", "unknown config key 'dictate_threshold'"),
        ("dictate_margin: 14\n", "unknown config key 'dictate_margin'"),
        ("dictate_silence: 1\n", "unknown config key 'dictate_silence'"),
    ],
)
def test_dictation_keys_refuse_bad_values(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        config.load_config(write_config(tmp_path, body))


def test_dictation_keys_unset_forms_drop_out(tmp_path: Path) -> None:
    for key in ("dictate_model", "dictate_device"):
        for body in (f"{key}:\n", f"{key}: ''\n"):
            assert config.load_config(write_config(tmp_path, body)) == {}


def test_a_daemon_entry_refuses_the_dictation_keys(tmp_path: Path) -> None:
    """The keys are global-only: the microphone belongs to the
    operator's desktop, not to a daemon connection."""
    path = write_config(
        tmp_path,
        "daemons:\n  lab:\n    url: https://lab:8660\n"
        "    dictate_model: small\n",
    )
    with pytest.raises(ValueError, match="dictate_model"):
        config.load_config(path)


def test_resolve_carries_the_validated_file_keys(
    tmp_path: Path,
) -> None:
    """The resolution hands the GUI the file's validated keys
    (#1214): the dictation settings resolve from them under their
    own precedence. The environment wins nothing here — the
    dictation keys materialize nowhere, the GUI reads them off
    the doc itself."""
    conf = config.resolve(
        config=write_config(
            tmp_path, "url: https://daemon\ndictate_model: base.en\n"
        )
    )
    assert conf.doc == {"url": "https://daemon", "dictate_model": "base.en"}
    assert conf.env_layer == {}
    assert config.resolve(config="none").doc == {}


# --- the system file layer (#1224) ---------------------------------------

SYSTEM = """\
# the host's file
url: https://system:8660
cafile: /etc/msks/hv-1-ca.pem
daemons:
  host:
    url: https://host:8660
    token_file: /etc/msks/token
  lab:
    url: https://lab:8660
    expected_image: msks/sys-img
"""


def system_file(tmp_path: Path, text: str, monkeypatch) -> str:
    """The system file under the temporary tree, pointed at by the
    relocation variable (#1224)."""
    path = tmp_path / "system" / "msks.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", str(path))
    return str(path)


def user_tree(tmp_path: Path, monkeypatch) -> Path:
    """The user's config dir under the temporary tree."""
    user = tmp_path / "user"
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(user))
    return user


def test_system_config_path_reads_the_variable(monkeypatch) -> None:
    monkeypatch.delenv("MSKSC_SYSTEM_CONFIG", raising=False)
    assert config.system_config_path() == "/etc/msks/msks.yaml"
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", "/elsewhere/msks.yaml")
    assert config.system_config_path() == "/elsewhere/msks.yaml"
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", "")
    assert config.system_config_path() is None


def test_a_bare_msks_layers_the_system_file_under_the_users(
    tmp_path: Path, monkeypatch
) -> None:
    """The layer (#1224): a bare ``msks`` reads the system file as
    the floor and the user's file over it, key by key — the user's
    alias pick and image override stand, the system's url, CA,
    and token file fill in, and the paths record floor first with
    the user file the one a save writes."""
    clean_env(monkeypatch)
    system = system_file(tmp_path, SYSTEM, monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    user_file = user / "msks.yaml"
    user_file.write_text(
        "active_daemon: lab\n"
        "daemons:\n"
        "  lab:\n"
        "    expected_image: msks/my-img\n"
        "    cafile: ~/lab-ca.pem\n"
        "dictate_model: small\n"
    )
    conf = config.resolve()
    assert conf.config_paths == [system, str(user_file)]
    assert conf.config_path == str(user_file)  # the save target
    assert conf.config_arg is None  # the bare mode
    assert conf.daemon == "lab"
    assert conf.url == "https://lab:8660"  # the system's entry
    assert conf.expected_image == "msks/my-img"  # the user's override
    assert conf.cafile.endswith("/lab-ca.pem")  # the user's, expanded
    assert conf.doc["daemons"]["host"] == {
        "url": "https://host:8660",
        "token_file": "/etc/msks/token",
    }  # a system-only alias survives
    assert conf.doc["dictate_model"] == "small"
    # The global keys layer too: the system's url stands where the
    # user's file says nothing.
    assert conf.doc["url"] == "https://system:8660"


def test_daemons_merge_by_alias_key_by_key(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    system_file(tmp_path, SYSTEM, monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    (user / "msks.yaml").write_text(
        "daemons:\n"
        "  lab:\n"
        "    token_file: ~/lab.token\n"
        "  extra:\n"
        "    socket: /run/msks/extra.sock\n"
    )
    doc = config.resolve().doc
    assert doc["daemons"] == {
        "host": {"url": "https://host:8660", "token_file": "/etc/msks/token"},
        "lab": {
            "url": "https://lab:8660",  # the system's
            "expected_image": "msks/sys-img",  # the system's
            "token_file": "~/lab.token",  # the user's
        },
        "extra": {"socket": "/run/msks/extra.sock"},  # the user's alone
    }
    assert doc["url"] == "https://system:8660"


def test_the_user_file_replaces_a_scalar_the_system_file_carries(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    system_file(tmp_path, SYSTEM, monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    (user / "msks.yaml").write_text("url: https://user:8660\n")
    conf = config.resolve()
    assert conf.url == "https://user:8660"
    assert conf.doc["cafile"] == "/etc/msks/hv-1-ca.pem"  # the system's


def test_the_first_run_template_over_a_system_file_inherits_every_key(
    tmp_path: Path, monkeypatch
) -> None:
    """The template an empty document: over a system file it layers
    as "every key inherited" (#1224), never as a shadow."""
    clean_env(monkeypatch)
    system = system_file(tmp_path, SYSTEM, monkeypatch)
    user_tree(tmp_path, monkeypatch)
    conf = config.resolve()
    assert conf.config_paths == [system, conf.config_path]
    assert Path(conf.config_path).read_text().startswith("# msks client")
    assert conf.url == "https://system:8660"
    assert set(conf.doc["daemons"]) == {"host", "lab"}


def test_an_explicit_config_path_reads_no_system_file(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    system_file(tmp_path, SYSTEM, monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    explicit = user / "msks.yaml"
    explicit.write_text("expected_image: msks/mine\n")
    conf = config.resolve(config=str(explicit))
    assert conf.config_paths == [str(explicit)]
    assert conf.config_arg == str(explicit)
    assert conf.url == ""  # nothing layered under it
    assert conf.doc == {"expected_image": "msks/mine"}


def test_config_none_reads_neither_file(tmp_path: Path, monkeypatch) -> None:
    clean_env(monkeypatch)
    system_file(tmp_path, SYSTEM, monkeypatch)
    conf = config.resolve(config="none")
    assert conf.config_paths == []
    assert conf.config_path == "none"
    assert conf.config_arg == "none"
    assert conf.doc == {}


def test_an_empty_system_variable_means_no_system_file(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    (user / "msks.yaml").write_text("url: https://user:8660\n")
    conf = config.resolve()
    assert conf.config_paths == [str(user / "msks.yaml")]
    assert conf.url == "https://user:8660"


def test_a_system_file_that_does_not_stand_there_is_no_layer(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", str(tmp_path / "absent.yaml"))
    user_tree(tmp_path, monkeypatch)
    conf = config.resolve()
    assert conf.config_paths == [conf.config_path]


def test_each_file_refuses_with_its_own_path(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    (user / "msks.yaml").write_text("url: https://user\n")
    system = system_file(tmp_path, "url: [\n", monkeypatch)
    with pytest.raises(ValueError) as broken_system:
        config.resolve()
    assert str(system) in str(broken_system.value)
    system_file(tmp_path, SYSTEM, monkeypatch)
    (user / "msks.yaml").write_text("daemons: 7\n")
    with pytest.raises(ValueError) as broken_user:
        config.resolve()
    assert str(user / "msks.yaml") in str(broken_user.value)


def test_reload_doc_rereads_the_files_conf_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    system_file(tmp_path, "dictate_model: small\n", monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    (user / "msks.yaml").write_text("dictate_device: mic-a\n")
    conf = config.resolve()
    assert conf.doc == {"dictate_model": "small", "dictate_device": "mic-a"}
    (user / "msks.yaml").write_text("dictate_device: mic-b\n")
    assert config.reload_doc(conf)["dictate_device"] == "mic-b"


# --- the in-place save (#1224) ------------------------------------------


def test_save_keys_merges_into_the_user_file_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    """:func:`save_keys` writes the user's file and never the system
    file: the keys land in place, every other byte of the file —
    the comment, the alias block, the hyphen spelling — as the
    operator wrote it."""
    clean_env(monkeypatch)
    system = system_file(tmp_path, SYSTEM, monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    user_file = user / "msks.yaml"
    user_file.write_text(
        "# mine\n"
        "url: https://user:8660\n"
        "\n"
        "daemons:\n"
        "  lab:\n"
        "    url: https://my-lab:8660\n"
        "\n"
        "dictate-device: mic-a\n"
    )
    conf = config.resolve()
    config.save_keys(
        conf,
        {"dictate_model": "small", "dictate_device": "mic-b"},
    )
    assert user_file.read_text() == (
        "# mine\n"
        "url: https://user:8660\n"
        "\n"
        "daemons:\n"
        "  lab:\n"
        "    url: https://my-lab:8660\n"
        "\n"
        "dictate-device: mic-b\n"  # the file's own spelling
        "dictate_model: small\n"  # appended
    )
    assert Path(system).read_text() == SYSTEM  # untouched
    reloaded = config.reload_doc(conf)
    assert reloaded["dictate_model"] == "small"
    assert reloaded["dictate_device"] == "mic-b"
    assert reloaded["url"] == "https://user:8660"


def test_an_overlay_entry_takes_the_floor_aliass_address_one_way(
    tmp_path: Path, monkeypatch
) -> None:
    """The one-address rule runs over the merged entry (#1224): a
    user entry that adds a socket to a floor alias carrying a url
    is refused with the user file named, and a user entry that
    carries no address passes when the floor alias has one."""
    clean_env(monkeypatch)
    system_file(tmp_path, SYSTEM, monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    user_file = user / "msks.yaml"
    user_file.write_text("daemons:\n  lab:\n    token_file: ~/lab.token\n")
    assert config.resolve().doc["daemons"]["lab"]["url"] == "https://lab:8660"
    user_file.write_text("daemons:\n  lab:\n    socket: /run/lab.sock\n")
    with pytest.raises(
        ValueError, match="carries both a url and a socket"
    ) as both:
        config.resolve()
    assert str(user_file) in str(both.value)
    # A single file keeps the rule on its own: no address is refused.
    user_file.write_text("daemons:\n  lab:\n    token_file: ~/lab.token\n")
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", "")
    with pytest.raises(ValueError, match="needs a url or a socket"):
        config.resolve()


def test_save_keys_drops_a_blank_key(tmp_path: Path, monkeypatch) -> None:
    clean_env(monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    (user / "msks.yaml").write_text("dictate_model: small\nurl: https://u\n")
    conf = config.resolve()
    config.save_keys(conf, {"dictate_model": None})
    assert (user / "msks.yaml").read_text() == "url: https://u\n"


def test_save_keys_under_config_none_writes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    user_tree(tmp_path, monkeypatch)
    conf = config.resolve(config="none")
    config.save_keys(conf, {"dictate_model": "small"})
    assert not (tmp_path / "user").exists()


def test_save_keys_refuses_a_result_the_loader_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    """A file broken outside the keys: the merge lands its key and
    the self-check refuses the whole — nothing is written, the
    file stands as it was, and the refusal gives the path."""
    clean_env(monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    user_file = user / "msks.yaml"
    user_file.write_text("url: https://u\n")
    conf = config.resolve(config=str(user_file))
    user_file.write_text("url: [\n")  # broke under the window
    with pytest.raises(ValueError, match="invalid YAML") as refused:
        config.save_keys(conf, {"dictate_model": "small"})
    assert str(user_file) in str(refused.value)
    assert user_file.read_text() == "url: [\n"


def test_save_keys_names_an_unreadable_file(
    tmp_path: Path, monkeypatch
) -> None:
    clean_env(monkeypatch)
    user = user_tree(tmp_path, monkeypatch)
    user.mkdir()
    user_file = user / "msks.yaml"
    user_file.write_text("url: https://u\n")
    user_file.chmod(0o000)
    if os.getuid() == 0:  # root reads anything
        pytest.skip("root reads a mode-000 file")
    conf = config.ClientConfig(
        url="",
        socket="",
        token=None,
        cafile="",
        expected_image="",
        cache_dir=None,
        state_dir=None,
        identity_file=None,
        pubkey_file=None,
        terminal_open_cmd=None,
        image_manifest_url="",
        ssh_options=[],
        daemon=None,
        config_path=str(user_file),
        config_paths=[str(user_file)],
    )
    with pytest.raises(ValueError, match="cannot read config file") as refused:
        config.save_keys(conf, {"dictate_model": "small"})
    assert str(user_file) in str(refused.value)
