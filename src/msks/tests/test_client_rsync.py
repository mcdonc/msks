"""``msks rsync`` tests: the host fill, the remote-shell string, the
run loop.

The argv pieces are pinned separately (rsync's own argument shapes
were probed against the real binary: the empty host passes no host
word to the remote shell, ``user@host:`` appends ``-l user`` after
the ``-e`` words, the last ``-e`` wins, and the ``-e`` value is
word-split honoring double quotes), and — where a local sshd
exists — one integration test runs stock rsync against it through
the agent, proving the transport path rsync's ssh children take.
"""

import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from msks.client import agent, cli, rsync, ssh
from test_client_ssh import (
    KEY,
    KEYGEN_BIN,
    PEM,
    SSH_BIN,
    SSHD_BIN,
    free_port,
    needs_sshd,
    wait_listening,
)


@pytest.fixture
def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client environment the run loop reads."""
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")


RSYNC_BIN = shutil.which("rsync")

needs_rsync = pytest.mark.skipif(
    RSYNC_BIN is None,
    reason="rsync must be on PATH (the devenv shell ships it)",
)


# --- the empty-host fill ---


@pytest.mark.parametrize(
    ("raw", "filled"),
    [
        (":/root/site/", "alpha:/root/site/"),
        (":out.tar", "alpha:out.tar"),
        (":", "alpha:"),
        ("root@:/root/site/", "root@alpha:/root/site/"),
        ("root@:out.tar", "root@alpha:out.tar"),
        ("a@b@:/x", "a@b@alpha:/x"),
        ("host:/x", "host:/x"),
        ("root@host:/x", "root@host:/x"),
        ("::mod", "::mod"),  # the daemon protocol, not an empty host
        ("host::mod", "host::mod"),
        ("./local", "./local"),
        ("out.tar", "out.tar"),
        ("-av", "-av"),
        ("sub/dir:file", "sub/dir:file"),  # colon past a slash is local
    ],
)
def test_filled_host(raw: str, filled: str) -> None:
    assert rsync.filled_host(raw, "alpha") == filled


def test_rsync_paths_fills_each_argument() -> None:
    assert rsync.rsync_paths(
        ["-av", "./site/", ":/root/site/", "--delete"], "alpha"
    ) == ["-av", "./site/", "alpha:/root/site/", "--delete"]


# --- the remote-shell string and the argv ---


def test_config_directives_restate_the_session_options() -> None:
    lines = rsync.config_directives(
        [
            "-o",
            'ProxyCommand="/usr/bin/python -m x" fwd a 22',
            "-o",
            'UserKnownHostsFile="/tmp/known hosts"',
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            "/tmp/my id.pub",
        ],
        "msks",
    )
    assert lines == [
        "User msks",
        'ProxyCommand "/usr/bin/python -m x" fwd a 22',
        'UserKnownHostsFile "/tmp/known hosts"',
        "IdentitiesOnly yes",
        'IdentityFile "/tmp/my id.pub"',
    ]


def test_config_directives_skip_words_that_name_no_directive() -> None:
    """A word that is neither ``-o`` nor ``-i`` (session_options
    emits none) adds no line — the translation restates the options
    it knows and invents nothing for the rest."""
    assert rsync.config_directives(["-q"], "msks") == ["User msks"]


def test_rsh_string_is_one_quoting_level() -> None:
    assert rsync.rsh_string("/tmp/msks-agent-x/ssh_config") == (
        "ssh -F /tmp/msks-agent-x/ssh_config"
    )
    assert rsync.rsh_string("/tmp/known cfg") == 'ssh -F "/tmp/known cfg"'


def test_write_ssh_config_carries_user_and_transport(tmp_path: Path) -> None:
    with agent.serve(agent.load_private(PEM), "") as served:
        options = ssh.session_options(
            "alpha", served.server_address, served.identity_path, "/kh"
        )
        path = rsync.write_ssh_config(served, options, "alice")
        assert path == str(Path(served.identity_path).parent / "ssh_config")
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        assert lines[0] == "User alice"
        assert ssh.proxy_command("alpha").partition("=")[2] in " ".join(lines)
        assert any(line.startswith("IdentityAgent ") for line in lines)
        assert any(line.startswith("IdentityFile ") for line in lines)
        assert "UserKnownHostsFile /kh" in lines
        assert "StrictHostKeyChecking accept-new" in lines
        assert "IdentitiesOnly yes" in lines


def test_build_args_carries_the_transport_and_fills_the_paths() -> None:
    argv = rsync.build_args("alpha", "/cfg", ["-av", ":/dst/"])
    assert argv[0] == "rsync"
    assert argv[1] == "-e"
    assert argv[2] == "ssh -F /cfg"  # every setting rides the config
    assert argv[3:] == ["-av", "alpha:/dst/"]


def test_build_args_quotes_a_config_path_with_whitespace() -> None:
    argv = rsync.build_args("alpha", "/tmp/known cfg", [])
    assert argv[2] == 'ssh -F "/tmp/known cfg"'


def test_build_args_puts_a_passthrough_e_last() -> None:
    """rsync takes the last ``-e`` it receives, so a passthrough
    ``-e`` overrides msks's transport — the same override shape
    ssh passthrough options have."""
    argv = rsync.build_args("alpha", "/cfg", ["-e", "ssh -l root", ":/dst/"])
    assert argv[1:3] == ["-e", "ssh -F /cfg"]  # msks's shell comes first
    assert argv[3:] == ["-e", "ssh -l root", "alpha:/dst/"]


def test_require_args_names_the_usage() -> None:
    with pytest.raises(SystemExit, match="pass the rsync arguments"):
        rsync.require_args([])
    rsync.require_args(["-av", ":/x"])


# --- the run loop ---


class FakeAgent:
    """The served agent stand-in: socket and pubkey paths the run
    loop can write beside."""

    def __init__(self, tmp_path: Path) -> None:
        self.server_address = str(tmp_path / "agent.sock")
        self.identity_path = str(tmp_path / "identity.pub")


def fake_agent_serve(tmp_path: Path):
    """The agent.serve stand-in for one tmp directory."""

    @contextmanager
    def serve(private, comment):
        yield FakeAgent(tmp_path)

    return serve


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """The run loop with a not-running workspace, a fake agent, and a
    recording subprocess; the recorded argvs come back."""

    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, False

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        rsync,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )
    monkeypatch.setattr(ssh.agent, "serve", fake_agent_serve(tmp_path))
    commands: list[list[str]] = []
    monkeypatch.setattr(
        ssh.subprocess,
        "run",
        lambda argv, **kwargs: (
            commands.append(argv),
            SimpleNamespace(returncode=7, stderr=""),
        )[1],
    )
    return commands


def test_run_workspace_rsync_runs_rsync_and_stops_the_agent(
    monkeypatch: pytest.MonkeyPatch,
    client_env: None,
    tmp_path: Path,
    wired: list[list[str]],
) -> None:
    stopped: list = []

    @contextmanager
    def tracking_serve(private, comment):
        try:
            yield FakeAgent(tmp_path)
        finally:
            stopped.append("stopped")

    monkeypatch.setattr(ssh.agent, "serve", tracking_serve)
    rc = rsync.run_workspace_rsync("alpha", ["-av", "./s/", ":/d/"])
    assert rc == 7
    argv = wired[0]
    assert argv[0] == "rsync"
    assert argv[1] == "-e"
    assert argv[-1] == "alpha:/d/"  # the empty host was filled
    assert stopped == ["stopped"]  # the agent tears down after rsync exits


def test_run_workspace_rsync_skips_the_wait_when_not_booted(
    client_env: None, wired: list[list[str]]
) -> None:
    rc = rsync.run_workspace_rsync("alpha", [":/d/", "./d/"])
    assert rc == 7
    assert len(wired) == 1  # an already-running guest gets one command


def test_run_workspace_rsync_waits_out_a_first_boot(
    monkeypatch: pytest.MonkeyPatch,
    client_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, True

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        rsync,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )
    monkeypatch.setattr(ssh.agent, "serve", fake_agent_serve(tmp_path))
    clock = {"now": 0.0}
    monkeypatch.setattr(rsync.time, "monotonic", lambda: clock["now"])

    def advance(seconds: float) -> None:
        clock["now"] += 1.0

    monkeypatch.setattr(rsync.time, "sleep", advance)
    codes = iter([255, 255, 0, 7])  # two refusals, then seeded; rsync: 7
    commands: list[list[str]] = []

    def fake_run(argv, **kwargs) -> SimpleNamespace:
        commands.append(argv)
        clock["now"] += 1.0
        return SimpleNamespace(returncode=next(codes), stderr="")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    rc = rsync.run_workspace_rsync("alpha", ["-av", "./s/", ":/d/"])
    assert rc == 7
    assert len(commands) == 4  # three probes, then the one real run
    assert commands[-1][0] == "rsync"  # the copy itself carries no probe
    assert all(c[-1] == "true" for c in commands[:-1])
    err = capsys.readouterr().err
    assert err.count("not accepting the login yet") == 2


def test_run_workspace_rsync_names_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch, client_env: None, wired: list[list[str]]
) -> None:
    def missing(argv, **kwargs):
        raise FileNotFoundError("rsync")

    monkeypatch.setattr(ssh.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="rsync not found"):
        rsync.run_workspace_rsync("alpha", [":/x", "./x"])


def test_run_workspace_rsync_names_a_missing_ssh_from_the_wait(
    monkeypatch: pytest.MonkeyPatch,
    client_env: None,
    tmp_path: Path,
) -> None:
    """The booted path probes before the copy: a missing ssh is the
    one-line exit there, naming THIS command (the probe machinery is
    shared with ``msks ssh``)."""

    async def fake_prepare(*args, **kwargs) -> tuple[dict, bool]:
        return KEY, True

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(
        rsync,
        "known_hosts_path",
        lambda ws, base=None, instance=None: str(tmp_path),
    )
    monkeypatch.setattr(ssh.agent, "serve", fake_agent_serve(tmp_path))

    def probe_only(argv, timeout=None, **kwargs):
        if argv[0] == "ssh":
            raise FileNotFoundError("ssh")
        raise AssertionError("rsync ran before the probe")

    monkeypatch.setattr(ssh.subprocess, "run", probe_only)
    with pytest.raises(SystemExit, match="msks rsync: ssh not found"):
        rsync.run_workspace_rsync("alpha", ["-av", ":/x", "./x"])


# --- CLI wiring ---


def test_cli_parses_the_rsync_passthrough() -> None:
    args = cli.build_parser().parse_args(
        ["rsync", "alpha", "--", "-av", "./s/", ":/d/"]
    )
    assert args.workspace_id == "alpha"
    assert args.passthrough == ["-av", "./s/", ":/d/"]  # argparse eats the --


def test_cli_dispatch_reaches_the_rsync_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        cli,
        "run_workspace_rsync",
        lambda ws, passthrough, transport=None: (
            calls.append((ws, passthrough)),
            5,
        )[1],
    )
    args = cli.build_parser().parse_args(
        ["rsync", "alpha", "-av", ":/d/", "./d/"]
    )
    assert cli.dispatch(args) == 5
    assert calls == [("alpha", ["-av", ":/d/", "./d/"])]


# --- the precedence the design rides on (ssh -G, stock parsing) ---


def ssh_g(config_path: str, *extra: str) -> dict[str, str]:
    """ssh's resolved configuration for a destination, as a
    keyword→value map (``ssh -G`` answers the parse the real
    connection would use)."""
    done = subprocess.run(
        [SSH_BIN, "-G", "-F", config_path, *extra, "alpha"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return {
        line.split(None, 1)[0]: line.split(None, 1)[1]
        for line in done.stdout.splitlines()
        if len(line.split(None, 1)) == 2
    }


@needs_rsync
def test_the_config_user_is_a_default_the_command_line_overrides(
    tmp_path: Path,
) -> None:
    """The two stock-ssh behaviors the ``root@:`` spelling rides
    on, pinned: the generated config's ``User`` is the resolved
    default, and rsync's appended ``-l user`` (its ``user@host:``
    translation) overrides it. Also the translated, quoted config
    lines parse — a whitespace-carrying ``UserKnownHostsFile`` and
    ``ProxyCommand`` survive ``ssh``'s own config parser whole."""
    with agent.serve(agent.load_private(PEM), "") as served:
        options = ssh.session_options(
            "alpha",
            served.server_address,
            served.identity_path,
            str(tmp_path / "known hosts"),
        )
        cfg = tmp_path / "ssh_config"
        cfg.write_text(
            "\n".join(rsync.config_directives(options, "msks")) + "\n",
            encoding="utf-8",
        )
        resolved = ssh_g(str(cfg))
        assert resolved["user"] == "msks"
        assert resolved["userknownhostsfile"] == str(tmp_path / "known hosts")
        assert (
            resolved["proxycommand"]
            == (ssh.proxy_command("alpha").partition("=")[2])
        )
        assert resolved["identityfile"].splitlines()[0] == (
            served.identity_path
        )
        # rsync appends -l user after the -e words for user@host:
        # paths — the command line wins over the config default.
        assert ssh_g(str(cfg), "-l", "root")["user"] == "root"


@needs_rsync
def test_stock_rsync_shapes_the_remote_shell_argv(tmp_path: Path) -> None:
    """rsync's own argument shaping, pinned against the real
    binary with a stand-in remote shell: ``user@host:`` appends
    ``-l user`` and the host AFTER the ``-e`` words, the empty
    host passes an EMPTY host word (ssh would parse rsync's first
    remote-command word as its destination — why msks fills it),
    the last ``-e`` wins, and the ``-e`` value is word-split
    honoring double quotes — the one quoting level the design
    allows."""
    argv_log = tmp_path / "rsh-argv.txt"
    standin = tmp_path / "ssh"
    standin.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > ' + shlex_quote(str(argv_log)) + "\n"
    )
    standin.chmod(0o755)
    spaced = tmp_path / "ssh cfg"
    spaced.write_text("")
    source = tmp_path / "f"
    source.write_text("x")

    def recorded(*call: str) -> list[str]:
        argv_log.write_text("")
        done = subprocess.run(
            [RSYNC_BIN, *call], capture_output=True, timeout=30
        )
        assert done.returncode != 0 or argv_log.read_text(), done
        return argv_log.read_text().splitlines()

    argv = recorded(
        "-e", f'{standin} -F "{spaced}"', str(source), "root@alpha:/x"
    )
    assert argv[:2] == ["-F", str(spaced)]  # one word, quotes honored
    assert argv[2:5] == ["-l", "root", "alpha"]
    assert argv[5] == "rsync" and "--server" in argv
    assert recorded("-e", f"{standin}", str(source), ":/x")[:2] == [
        "",
        "rsync",
    ]
    # ^ the empty host passes an EMPTY host word — ssh would then
    # parse rsync's first remote-command word as its destination,
    # which is why msks fills the host
    later = tmp_path / "later-shell"
    later.write_text("#!/bin/sh\nexit 42\n")
    later.chmod(0o755)
    argv_log.write_text("")
    done = subprocess.run(
        [
            RSYNC_BIN,
            "-e",
            f"{standin}",
            "-e",
            str(later),
            str(source),
            "alpha:/x",
        ],
        capture_output=True,
        timeout=30,
    )
    # The exit status is whichever side of rsync's internal race
    # wins: 42, the later shell's own status, when rsync reaps the
    # child first; 12, rsync's protocol-stream error, when it sees
    # the closed pipe first (both observed on 3.5.0, ~2% the
    # latter locally). Either way the LAST -e ran and the first
    # one never did — its log stayed empty.
    assert done.returncode in (42, 12) and not argv_log.read_text()


def shlex_quote(value: str) -> str:
    """A path safe inside the stand-in's own double-quoted shell
    string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


# --- stock rsync against a local sshd, through the agent ---


@needs_sshd
@needs_rsync
def test_rsync_copies_both_directions_through_the_agent(
    tmp_path: Path,
) -> None:
    """The semantics ``msks rsync`` depends on, proven against the
    real tools: rsync's ssh child authenticates from the
    IdentityAgent listing alone (no identity file), under
    IdentitiesOnly, and the per-session ``-F`` config supplies the
    default login user — the stock behaviors the ``-e`` string and
    :func:`rsync.write_ssh_config` build on."""
    import os
    import pwd

    user = pwd.getpwuid(os.getuid()).pw_name
    hostkey = tmp_path / "host_ed25519"
    keygen = subprocess.run(
        [KEYGEN_BIN, "-t", "ed25519", "-N", "", "-q", "-f", str(hostkey)],
        timeout=30,
    )
    assert keygen.returncode == 0, keygen.stderr
    authorized = tmp_path / "authorized_keys"
    private = agent.load_private(PEM)
    authorized.write_text(
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
        + "\n"
    )
    port = free_port()
    config = tmp_path / "sshd_config"
    config.write_text(
        f"Port {port}\n"
        "ListenAddress 127.0.0.1\n"
        f"HostKey {hostkey}\n"
        f"AuthorizedKeysFile {authorized}\n"
        "UsePAM no\n"
        "StrictModes no\n"
        "PasswordAuthentication no\n"
        f"PidFile {tmp_path / 'sshd.pid'}\n",
    )
    sshd_stderr = tmp_path / "sshd.log"
    sshd_log = open(sshd_stderr, "ab")
    sshd = subprocess.Popen(
        [SSHD_BIN, "-D", "-e", "-f", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=sshd_log,
    )
    try:
        wait_listening(port, evidence=sshd_stderr)
        with agent.serve(private, "msksd:alpha") as served:
            # The real machinery's shape: the session settings as a
            # per-session config, the -e value at "ssh -F <path>" —
            # with the test's port and host-key posture in place of
            # the forward transport.
            ssh_config = tmp_path / "ssh_config"
            ssh_config.write_text(
                "\n".join(
                    rsync.config_directives(
                        [
                            "-o",
                            f"Port={port}",
                            "-o",
                            "BatchMode=yes",
                            "-o",
                            "IdentitiesOnly=yes",
                            "-o",
                            f"IdentityAgent={served.server_address}",
                            "-i",
                            served.identity_path,
                            "-o",
                            "StrictHostKeyChecking=no",
                            "-o",
                            "UserKnownHostsFile=/dev/null",
                        ],
                        user,
                    )
                )
                + "\n"
            )
            rsh = rsync.rsh_string(str(ssh_config))
            src = tmp_path / "src"
            src.mkdir()
            (src / "payload.txt").write_text("over the forward\n")
            remote = tmp_path / "remote-out"
            remote.mkdir()
            pushed = subprocess.run(
                [
                    RSYNC_BIN,
                    "-e",
                    rsh,
                    "-a",
                    f"{src}/",
                    f"127.0.0.1:{remote}/",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert pushed.returncode == 0, pushed.stderr
            assert (remote / "payload.txt").read_text() == "over the forward\n"
            pulled_to = tmp_path / "pull"
            pulled_to.mkdir()
            pulled = subprocess.run(
                [
                    RSYNC_BIN,
                    "-e",
                    rsh,
                    "-a",
                    f"127.0.0.1:{remote}/payload.txt",
                    str(pulled_to / "back.txt"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert pulled.returncode == 0, pulled.stderr
            assert (pulled_to / "back.txt").read_text() == "over the forward\n"
    finally:
        sshd.terminate()
        sshd.wait(timeout=10)
        sshd_log.close()
