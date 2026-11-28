"""The connection picker's presenter (#1009, phase 5 of
#924): the presets the window picks its daemon from, and the
environment application a pick makes. No Qt in it — the
presets build over the client's own config layer with a
temporary file, a fake state dir, and a patched environment,
and every pick's writes read straight off the environment the
readers share.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from msks.client.gui import connection as connection_mod


@pytest.fixture(autouse=True)
def plain_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean connection environment: the shaping variables
    absent unless a test sets them, and the dev state dir
    pointing nowhere — the working tree's own dev daemon must
    not leak a preset into a test's menu."""
    for name in connection_mod.SHAPING_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "no-dev-state"))


def config_file(tmp_path: Path, text: str) -> str:
    """One config file with the text given."""
    path = tmp_path / "msks.yaml"
    path.write_text(text)
    return str(path)


ALIASES = """
daemons:
  farm:
    url: https://10.0.0.9:9660
    token_file: {token}
  laptop:
    socket: /run/msks/local.sock
active_daemon: farm
"""


def dev_state(tmp_path: Path, *, port: str = "9661") -> Path:
    """The dev daemon's state dir: the port, the token, and the
    CA the daemon writes on first serve."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "port").write_text(port)
    (state / "bootstrap-token").write_text("devtok\n")
    (state / "msks-ca.pem").write_text("CA\n")
    return state


def test_the_presets_list_the_file_the_dev_daemon_and_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The menu's rows (#1009): the file's aliases in the file's
    own order with the invocation's alias marked, the dev daemon
    when its state dir names a dial, and the environment when
    the shell exported an address."""
    token = tmp_path / "farm.tok"
    token.write_text("farmtok")
    config = config_file(tmp_path, ALIASES.format(token=token))
    monkeypatch.setenv("MSKSD_STATE_DIR", str(dev_state(tmp_path)))
    # The devenv shape (#1009): the shell's exports ARE the dev
    # daemon's state dir — one dial, one row. The environment
    # row dedupes into the dev row (its state-dir source the
    # fresher read), and the dev row marks itself the standing
    # connection.
    monkeypatch.setenv("MSKSC_URL", "https://127.0.0.1:9661")
    conf = SimpleNamespace(daemon=None, url="", socket="")
    rows = connection_mod.Connections(conf, config=config).presets()
    assert [(row.name, row.kind, row.active) for row in rows] == [
        ("farm", connection_mod.ALIAS, False),
        ("laptop", connection_mod.ALIAS, False),
        ("dev daemon", connection_mod.DEV, True),
    ]
    assert rows[0].address == "https://10.0.0.9:9660"
    assert rows[1].address == "/run/msks/local.sock"
    assert rows[2].address == "https://127.0.0.1:9661"
    # An alias the invocation itself picked marks that alias,
    # and the dev row stands down; the environment row stays
    # deduped either way.
    conf = SimpleNamespace(daemon="laptop", url="", socket="")
    rows = connection_mod.Connections(conf, config=config).presets()
    assert [row.active for row in rows] == [False, True, False]
    # A shell whose exports name another dial keeps both rows —
    # two dials, two names — the environment row marking itself
    # the standing connection.
    monkeypatch.setenv("MSKSC_URL", "https://other.example:9660")
    rows = connection_mod.Connections(
        SimpleNamespace(daemon=None, url="", socket=""), config=config
    ).presets()
    assert [(row.name, row.active) for row in rows] == [
        ("farm", False),
        ("laptop", False),
        ("dev daemon", False),
        ("environment", True),
    ]
    assert rows[3].address == "https://other.example:9660"


def test_the_environment_row_marks_itself_the_standing_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported address with no dev state and no alias pick
    (#1009): the environment row is the standing connection, the
    one an ambient export dialed."""
    monkeypatch.setenv("MSKSC_URL", "https://msks.example:9660")
    conf = SimpleNamespace(daemon=None, url="", socket="")
    rows = connection_mod.Connections(conf, config="none").presets()
    assert [(row.name, row.active) for row in rows] == [("environment", True)]
    # Nothing exported and nothing named: no environment row at
    # all — the local socket default dials and no preset claims
    # it.
    monkeypatch.delenv("MSKSC_URL")
    rows = connection_mod.Connections(None, config="none").presets()
    assert rows == []


def test_an_unusable_config_lists_no_aliases(tmp_path: Path) -> None:
    """A broken file at menu-build time (#1009): the aliases
    list nothing — the window already runs on what bootstrap
    resolved — and the other presets stand."""
    config = config_file(tmp_path, "daemons: [not a mapping]\n")
    rows = connection_mod.Connections(None, config=config).presets()
    assert [row.kind for row in rows] == []


def test_an_alias_pick_applies_the_config_layers_own_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alias pick (#1009): the config layer's own resolve
    and apply materialize the entry's values — the flagged
    entry outranking the ambient environment, the address
    written exclusively (the socket variable emptied), exactly
    what ``msks --daemon farm`` would dial."""
    token = tmp_path / "farm.tok"
    token.write_text("farmtok")
    config = config_file(tmp_path, ALIASES.format(token=token))
    monkeypatch.setenv("MSKSC_URL", "https://ambient:1")
    monkeypatch.setenv("MSKSC_SOCKET", "/run/ambient.sock")
    held = connection_mod.Connections(None, config=config)
    assert held.pick(held.presets()[0]) is True
    assert os.environ["MSKSC_URL"] == "https://10.0.0.9:9660"
    assert os.environ["MSKSC_SOCKET"] == ""
    assert os.environ["MSKSC_TOKEN"] == "farmtok"


def test_an_alias_the_file_can_no_longer_resolve_picks_nothing(
    tmp_path: Path,
) -> None:
    """The refused pick (#1009): the file changed under the menu
    — its token file left — and the config layer's own refusal
    answers False with the environment untouched, nothing to
    re-dial."""
    token = tmp_path / "farm.tok"
    token.write_text("farmtok")
    config = config_file(tmp_path, ALIASES.format(token=token))
    held = connection_mod.Connections(None, config=config)
    preset = held.presets()[0]
    token.unlink()
    assert held.pick(preset) is False
    assert "MSKSC_URL" not in os.environ


def test_a_dev_pick_writes_the_state_dirs_dial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dev pick (#1009): the url, token, and CA from the
    state dir as it stands at the pick — the values re-read, a
    rotated port honored — with the socket variable emptied
    beside them. A state dir that cannot name a dial answers
    False and leaves the environment untouched."""
    state = dev_state(tmp_path)
    monkeypatch.setenv("MSKSD_STATE_DIR", str(state))
    held = connection_mod.Connections(None, config="none")
    preset = held.presets()[0]
    assert held.pick(preset) is True
    assert os.environ["MSKSC_URL"] == "https://127.0.0.1:9661"
    assert os.environ["MSKSC_SOCKET"] == ""
    assert os.environ["MSKSC_TOKEN"] == "devtok"
    assert os.environ["MSKSC_CAFILE"] == str(state / "msks-ca.pem")
    # The pick re-reads the state: a rotated port dials the new
    # one.
    (state / "port").write_text("9662")
    assert held.pick(preset) is True
    assert os.environ["MSKSC_URL"] == "https://127.0.0.1:9662"
    # A state dir that lost its files picks nothing.
    (state / "port").unlink()
    assert held.pick(preset) is False
    assert os.environ["MSKSC_URL"] == "https://127.0.0.1:9662"


def test_an_environment_pick_restores_the_open_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment pick (#1009): the dial returns to the
    exports the window opened on — a value the shell supplied
    comes back, a variable a later pick added leaves the
    environment whole."""
    monkeypatch.setenv("MSKSC_URL", "https://shell.example:9660")
    monkeypatch.setenv("MSKSC_TOKEN", "shelltok")
    held = connection_mod.Connections(None, config="none")
    monkeypatch.setenv("MSKSC_URL", "https://elsewhere:9")
    monkeypatch.setenv("MSKSC_CAFILE", "/tmp/some-ca.pem")
    assert held.pick(held.presets()[0]) is True
    assert os.environ["MSKSC_URL"] == "https://shell.example:9660"
    assert os.environ["MSKSC_TOKEN"] == "shelltok"
    assert "MSKSC_CAFILE" not in os.environ
    assert "MSKSC_SOCKET" not in os.environ


def test_the_dev_values_read_the_state_dirs_files(
    tmp_path: Path,
) -> None:
    """The state dir's own reads (#1009): a missing dir, an
    empty port, or a missing token answers None — nothing
    honest to dial — and the CA joins only when its file
    stands."""
    gone = connection_mod.dev_values(tmp_path / "nope")
    assert gone is None
    state = tmp_path / "state"
    state.mkdir()
    (state / "port").write_text("\n")
    (state / "bootstrap-token").write_text("tok")
    assert connection_mod.dev_values(state) is None
    (state / "port").write_text("9")
    values = connection_mod.dev_values(state)
    assert values == ("https://127.0.0.1:9", "tok", "")
    (state / "msks-ca.pem").write_text("CA")
    assert connection_mod.dev_values(state) == (
        "https://127.0.0.1:9",
        "tok",
        str(state / "msks-ca.pem"),
    )


def test_the_dev_state_dir_honors_the_variable_and_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state dir's resolution (#1009): ``MSKSD_STATE_DIR``
    names it outright, and without it the working directory's
    ``.devenv/state/msksd`` stands — the devenv tree's own
    default."""
    monkeypatch.setenv("MSKSD_STATE_DIR", "/opt/state")
    assert connection_mod.dev_state_dir() == Path("/opt/state")
    monkeypatch.delenv("MSKSD_STATE_DIR")
    assert connection_mod.dev_state_dir() == connection_mod.DEV_STATE_DEFAULT


def test_a_conf_that_named_an_address_stands_on_it(
    tmp_path: Path,
) -> None:
    """The invocation's own resolution wins the standing
    address (#1009): a conf that carried a url (or a socket)
    names the standing dial before any export reads, and a
    non-dict daemon entry in a hand-built document lists
    nothing for itself."""
    from msks.client.gui import connection as conn

    conf = SimpleNamespace(
        daemon="farm", url="https://10.0.0.9:9660", socket=""
    )
    startup = {"MSKSC_URL": "https://ambient:1", "MSKSC_SOCKET": ""}
    assert conn.standing_address(conf, startup) == "https://10.0.0.9:9660"
    conf = SimpleNamespace(daemon=None, url="", socket="/run/local.sock")
    assert conn.standing_address(conf, startup) == "/run/local.sock"
    assert conn.alias_entries(
        {"daemons": {"ok": {"url": "https://u"}, "bogus": 3}}
    ) == [("ok", "https://u")]


def test_a_dev_pick_without_a_ca_dials_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state dir with no CA file yet (#1009): the dial applies
    with url and token alone, the CA variable left for whatever
    stood — the daemon writes the CA on first serve, and a
    window opened before that still picks the connection."""
    state = tmp_path / "bare"
    state.mkdir()
    (state / "port").write_text("9663")
    (state / "bootstrap-token").write_text("tok\n")
    monkeypatch.setenv("MSKSD_STATE_DIR", str(state))
    held = connection_mod.Connections(None, config="none")
    assert held.pick(held.presets()[0]) is True
    assert os.environ["MSKSC_URL"] == "https://127.0.0.1:9663"
    assert "MSKSC_CAFILE" not in os.environ


def test_a_pick_drives_the_marking_after_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pick's own marking (#1009): the row the operator
    picked stands checked on every later menu — a launch-time
    resolution the window still carries cannot keep naming a
    dial the operator just left, the toggle's whole point."""
    monkeypatch.setenv("MSKSD_STATE_DIR", str(dev_state(tmp_path)))
    monkeypatch.setenv("MSKSC_URL", "https://other.example:9660")
    conf = SimpleNamespace(daemon=None, url="", socket="")
    held = connection_mod.Connections(conf, config="none")
    # Pick the dev daemon: its row marks itself, the
    # environment row clears.
    dev = held.presets()[0]
    assert dev.kind == connection_mod.DEV
    assert held.pick(dev) is True
    assert [(row.name, row.active) for row in held.presets()] == [
        ("dev daemon", True),
        ("environment", False),
    ]
    # Pick the environment back: the toggle moves with it, the
    # launch-time inference (which named the environment at the
    # open) nothing the operator's act cannot outrank.
    env = held.presets()[1]
    assert env.kind == connection_mod.ENVIRONMENT
    assert held.pick(env) is True
    assert [(row.name, row.active) for row in held.presets()] == [
        ("dev daemon", False),
        ("environment", True),
    ]


def test_a_picked_row_that_leaves_marks_its_own_dial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pick's row gone (#1009): the state dir a re-host
    re-read no longer naming a dial, the row carrying the
    pick's own address stands in — the dial the pick landed on
    is the dial that stands."""
    state = dev_state(tmp_path, port="9670")
    monkeypatch.setenv("MSKSD_STATE_DIR", str(state))
    # The shell exported the same dial the state dir names, so
    # the environment row dedupes into the dev row at the open.
    monkeypatch.setenv("MSKSC_URL", "https://127.0.0.1:9670")
    held = connection_mod.Connections(None, config="none")
    dev = held.presets()[0]
    assert held.pick(dev) is True
    # The state dir rotates away; the environment row — the
    # shell's own export, still standing — carries the pick's
    # address.
    (state / "port").unlink()
    rows = held.presets()
    assert [(row.name, row.active) for row in rows] == [("environment", True)]


def test_a_pick_starts_from_the_open_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's own compose rule (#1009): every pick applies
    over the open's snapshot, so an earlier pick's token or CA
    never rides a later pick's URL — the dev token and CA a dev
    pick wrote leave with the next alias pick, whose entry names
    an address alone."""
    token = tmp_path / "farm.tok"
    token.write_text("farmtok")
    config = config_file(
        tmp_path,
        "daemons:\n  bare:\n    url: https://10.0.0.9:9660\n"
        f"  farm:\n    url: https://farm:9660\n    token_file: {token}\n",
    )
    state = dev_state(tmp_path, port="9669")
    monkeypatch.setenv("MSKSD_STATE_DIR", str(state))
    held = connection_mod.Connections(None, config=config)
    rows = {row.name: row for row in held.presets()}
    assert held.pick(rows["dev daemon"]) is True
    assert os.environ["MSKSC_TOKEN"] == "devtok"
    assert os.environ["MSKSC_CAFILE"] == str(state / "msks-ca.pem")
    # The bare alias names an address alone: the dev pick's
    # token and CA leave with the pick's own starting point.
    assert held.pick(rows["bare"]) is True
    assert os.environ["MSKSC_URL"] == "https://10.0.0.9:9660"
    assert "MSKSC_TOKEN" not in os.environ
    assert "MSKSC_CAFILE" not in os.environ


def test_a_refused_pick_leaves_the_standing_dial_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal's restore (#1009): the standing tree's calls
    read the environment per exchange, so a pick that cannot
    apply restores the dial as it stood — not the open's
    snapshot, a redirect the operator never asked for."""
    token = tmp_path / "farm.tok"
    token.write_text("farmtok")
    config = config_file(tmp_path, ALIASES.format(token=token))
    state = dev_state(tmp_path, port="9671")
    monkeypatch.setenv("MSKSD_STATE_DIR", str(state))
    held = connection_mod.Connections(None, config=config)
    rows = {row.name: row for row in held.presets()}
    assert held.pick(rows["dev daemon"]) is True
    standing = dict(
        (name, os.environ.get(name, ""))
        for name in connection_mod.SHAPING_VARS
    )
    token.unlink()
    assert held.pick(rows["farm"]) is False
    after = dict(
        (name, os.environ.get(name, ""))
        for name in connection_mod.SHAPING_VARS
    )
    assert after == standing
    assert held.pick(rows["dev daemon"]) is True  # the dial still stands


def test_a_changed_ca_builds_a_fresh_tls_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TLS cache keys on the CA (#1009): the process's one
    context is built from the CA that stood when it was first
    used, and a pick re-dials a daemon whose CA posture can
    differ — a changed ``MSKSC_CAFILE`` builds the next context
    fresh (pinned or TOFU whichever the new dial names), an
    unchanged one keeps the cached context, and a pick whose CA
    did not move reuses it."""
    import msks.client.env as env_mod

    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "no-dev"))
    monkeypatch.setenv("MSKSC_URL", "https://shell.example:9660")
    ca = tmp_path / "ca.pem"
    ca.write_text("CA\n")
    monkeypatch.setattr(env_mod, "_SHARED_SSL", [None])
    monkeypatch.setattr(env_mod, "_SHARED_SSL_KEY", [None])
    builds: list[object] = []
    monkeypatch.setattr(
        env_mod, "ssl_context", lambda: builds.append(object()) or builds[-1]
    )
    held = connection_mod.Connections(None, config="none")
    held.pick(held.presets()[0])  # the environment: no CA stood
    first = env_mod.shared_ssl()
    again = env_mod.shared_ssl()
    assert first is again  # an unchanged CA keeps the context
    assert builds == [first]  # one build, not one per call
    # A pick that pins a CA builds the next context fresh.
    state = tmp_path / "state"
    state.mkdir()
    (state / "port").write_text("9680")
    (state / "bootstrap-token").write_text("tok\n")
    (state / "msks-ca.pem").write_text("CA2\n")
    monkeypatch.setenv("MSKSD_STATE_DIR", str(state))
    dev = next(row for row in held.presets() if row.kind == connection_mod.DEV)
    assert held.pick(dev) is True
    assert os.environ["MSKSC_CAFILE"] == str(state / "msks-ca.pem")
    fresh = env_mod.shared_ssl()
    assert fresh is not first
    assert builds == [first, fresh]


def test_the_conf_names_the_config_file_the_picker_reads(
    tmp_path: Path,
) -> None:
    """The ``--config`` plumb (#1009): the invocation's resolved
    config carries the file it read, and the picker built on
    that conf alone lists that file's aliases — the custom file
    a launch bootstrapped from, not the default tree beside
    it."""
    from msks.client.config import resolve

    token = tmp_path / "t.tok"
    token.write_text("tok")
    config = config_file(
        tmp_path,
        "daemons:\n  custom:\n    url: https://custom:9660\n"
        f"    token_file: {token}\n",
    )
    conf = resolve(config=config)
    assert conf.config_path == config
    rows = connection_mod.Connections(conf).presets()
    assert [(row.name, row.address) for row in rows] == [
        ("custom", "https://custom:9660")
    ]
    # A pick resolves through the same file.
    assert connection_mod.Connections(conf).pick(rows[0]) is True
    assert os.environ["MSKSC_URL"] == "https://custom:9660"
    assert os.environ["MSKSC_TOKEN"] == "tok"


def test_the_picker_reads_the_layer_over_a_bare_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch's mode, not its file (#1224): a window launched
    bare over a system file lists the aliases of both files and
    marks the host's pick — the picker re-resolves the way the
    launch did, through ``config_arg``, not through the user file
    alone."""
    from msks.client.config import resolve

    token = tmp_path / "host.tok"
    token.write_text("hosttok")
    system = tmp_path / "system.yaml"
    system.write_text(
        "active_daemon: host\n"
        "daemons:\n"
        "  host:\n"
        f"    url: https://host:9660\n    token_file: {token}\n"
    )
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", str(system))
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "msks.yaml").write_text(
        "daemons:\n  mine:\n    socket: /run/msks/mine.sock\n"
    )
    conf = resolve()
    rows = connection_mod.Connections(conf).presets()
    assert [(row.name, row.address) for row in rows] == [
        ("host", "https://host:9660"),
        ("mine", "/run/msks/mine.sock"),
    ]
    assert rows[0].active is True  # the host's pick stands marked
    assert connection_mod.Connections(conf).config is None  # the bare mode


def test_the_launchs_materialized_winners_leave_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI parity the snapshot owes (#1009): a window
    launched ``--daemon farm`` — or with ``active_daemon:``
    naming one — carries bootstrap's materialized winners in
    its environment, and the snapshot must subtract them (the
    conf's own ``env_layer`` is exactly those pairs): the
    shell's exports alone are the starting point, so a weaker
    alias pick composes with nothing the launch alias carried,
    and the environment row names a shell export, not the
    launch's materialized dial."""
    token = tmp_path / "farm.tok"
    token.write_text("farmtok")
    config = config_file(
        tmp_path,
        "daemons:\n  bare:\n    url: https://10.0.0.9:9660\n"
        f"  farm:\n    url: https://farm:9660\n    token_file: {token}\n"
        "    cafile: /tmp/farm-ca.pem\n",
    )
    # The launch shape: bootstrap resolved and materialized the
    # farm pick into the environment, exactly as a real
    # ``msks --daemon farm gui`` leaves it.
    from msks.client.config import apply as apply_config
    from msks.client.config import resolve

    conf = resolve(daemon="farm", config=config)
    apply_config(conf)
    monkeypatch.setenv("MSKSD_STATE_DIR", str(tmp_path / "no-dev"))
    held = connection_mod.Connections(conf)
    # The materialized URL left the snapshot: no environment row
    # rides beside the aliases (the launch's alias marked).
    rows = {row.name: row for row in held.presets()}
    assert set(rows) == {"farm", "bare"}
    assert rows["farm"].active
    # The weaker alias pick composes with nothing farm carried —
    # the CLI's own dial from the same clean shell.
    assert held.pick(rows["bare"]) is True
    assert os.environ["MSKSC_URL"] == "https://10.0.0.9:9660"
    assert "MSKSC_TOKEN" not in os.environ
    assert "MSKSC_CAFILE" not in os.environ
    # The environment pick restores the shell's exports: nothing
    # materialized rides back.
    monkeypatch.setenv("MSKSC_URL", "https://shell.example:9660")
    held = connection_mod.Connections(conf)
    env = next(
        row for row in held.presets() if row.kind == connection_mod.ENVIRONMENT
    )
    assert env.address == "https://shell.example:9660"
    assert held.pick(env) is True
    assert os.environ["MSKSC_URL"] == "https://shell.example:9660"
    assert "MSKSC_TOKEN" not in os.environ
