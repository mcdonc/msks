"""``msks ssh`` tests: identity staging, argv plumbing, the run loop.

The command's pieces are pinned separately — the sealed memfd, the
known_hosts path, the passthrough rules, the argv — and the run loop
is tested against a stubbed ssh binary, so no network, daemon, or
guest is needed.
"""

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from msks.client import cli, ssh
from msks.identity import mint

KEY = {
    "public_key": "ecdsa-sha2-nistp256 AAAA msksd:alpha",
    "private_key": mint("ed25519")[0],
}

RUNNING_ROW = {"id": "alpha", "status": "running"}


@pytest.fixture
def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")


def mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- the sealed memfd ---


def test_memfd_key_is_sealed_and_readable() -> None:
    fd = ssh.memfd_key(KEY["private_key"])
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        assert mode == 0o600
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, 1 << 20) == KEY["private_key"].encode()
    finally:
        os.close(fd)


def test_memfd_key_refuses_a_host_without_memfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "memfd_create", raising=False)
    with pytest.raises(SystemExit, match="memfd_create"):
        ssh.memfd_key(KEY["private_key"])


def test_memfd_key_names_a_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(fd, data):
        raise OSError("no space")

    monkeypatch.setattr(os, "write", refused)
    with pytest.raises(SystemExit, match="cannot stage the identity"):
        ssh.memfd_key(KEY["private_key"])


# --- the per-workspace known_hosts file ---


def test_known_hosts_path_creates_its_directory(tmp_path: Path) -> None:
    path = ssh.known_hosts_path("alpha", base=tmp_path)
    assert path == str(tmp_path / "alpha" / "known_hosts")
    assert (tmp_path / "alpha").is_dir()


def test_cache_dir_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    assert ssh.cache_dir() == Path("/tmp/xdg-cache/msks")


# --- passthrough handling ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["-l", "root"], ["-l", "root"]),
        (["-A"], ["-A"]),
        ([], []),
        # An ssh-style -- typed past argparse's own separator
        # reaches ssh untouched: it is the options/command split.
        (["--", "uname", "-a"], ["--", "uname", "-a"]),
    ],
)
def test_passthrough_args_passes_verbatim(raw: list[str], expected: list[str]) -> None:
    assert ssh.passthrough_args(raw) == expected


@pytest.mark.parametrize(
    ("args", "split"),
    [
        (["-A"], (["-A"], [])),
        (["-A", "--", "uname"], (["-A"], ["uname"])),
        (["--", "uname"], ([], ["uname"])),
        (["--", "--", "x"], ([], ["--", "x"])),  # split at the first --
    ],
)
def test_split_command_splits_at_ssh_separator(
    args: list[str], split: tuple[list[str], list[str]]
) -> None:
    assert ssh.split_command(args) == split


@pytest.mark.parametrize(
    ("args", "names_user"),
    [
        (["-l", "root"], True),
        (["-o", "User=root"], True),
        (["-o", "User root"], True),
        (["-o", "ProxyCommand=x"], False),
        (["-A"], False),
        (["-l"], False),  # a trailing -l with no value names nothing
        (["-oUser=root"], True),  # the inline -o form
        (["-oProxyCommand=x"], False),
    ],
)
def test_wants_user(args: list[str], names_user: bool) -> None:
    assert ssh.wants_user(args) is names_user


# --- the argv ---


def test_build_args_injects_the_default_user() -> None:
    assert ssh.build_args("alpha", "/proc/self/fd/3", "/kh", ["-A"]) == [
        "ssh",
        "-o",
        "ProxyCommand=msks forward alpha 22",
        "-o",
        "UserKnownHostsFile=/kh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        "/proc/self/fd/3",
        "-l",
        "msks",
        "-A",
        "alpha",
    ]


def test_build_args_leaves_the_user_to_ssh() -> None:
    argv = ssh.build_args("alpha", "/proc/self/fd/3", "/kh", ["-l", "root"])
    assert argv[-3:] == ["-l", "root", "alpha"]


def test_build_args_carries_a_remote_command_after_the_host() -> None:
    argv = ssh.build_args(
        "alpha", "/proc/self/fd/3", "/kh", ["-A", "--", "uname", "-a"]
    )
    assert argv[-4:] == ["-A", "alpha", "uname", "-a"]


# --- prepare: the boot pre-flight and the key fetch ---


def test_prepare_boots_then_fetches() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(200, json=KEY)
        return httpx.Response(200, json=RUNNING_ROW)

    key = asyncio.run(
        ssh.prepare("alpha", "https://daemon", "tok", transport=mock(handler))
    )
    assert key == KEY
    assert seen == [
        "GET /api/v1/workspaces/alpha",
        "GET /api/v1/workspaces/alpha/ssh-key",
    ]


# --- the run loop ---


def test_run_workspace_ssh_runs_ssh_and_closes_the_fd(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    async def fake_prepare(*args, **kwargs) -> dict:
        return KEY

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(ssh, "known_hosts_path", lambda ws, base=None: str(tmp_path))
    calls: list[dict] = []

    def fake_run(argv, pass_fds=()) -> SimpleNamespace:
        calls.append({"argv": argv, "pass_fds": pass_fds})
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    rc = ssh.run_workspace_ssh("alpha", ["-l", "root"])
    assert rc == 7
    argv = calls[0]["argv"]
    assert argv[0] == "ssh"
    assert argv[-3:] == ["-l", "root", "alpha"]
    (fd,) = calls[0]["pass_fds"]
    assert f"/proc/self/fd/{fd}" in argv
    with pytest.raises(OSError):
        os.fstat(fd)  # closed after ssh exits — even on a crash


def test_run_workspace_ssh_names_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch, client_env: None, tmp_path: Path
) -> None:
    async def fake_prepare(*args, **kwargs) -> dict:
        return KEY

    monkeypatch.setattr(ssh, "prepare", fake_prepare)
    monkeypatch.setattr(ssh, "known_hosts_path", lambda ws, base=None: str(tmp_path))

    def missing(argv, pass_fds=()):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(ssh.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="ssh not found"):
        ssh.run_workspace_ssh("alpha", [])


# --- CLI wiring ---


def test_cli_parses_the_ssh_passthrough() -> None:
    args = cli.build_parser().parse_args(["ssh", "alpha", "--", "-l", "root"])
    assert args.workspace_id == "alpha"
    assert args.passthrough == ["-l", "root"]  # argparse eats the --


def test_cli_dispatch_reaches_the_ssh_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        cli,
        "run_workspace_ssh",
        lambda ws, passthrough, transport=None: (
            calls.append((ws, passthrough)),
            5,
        )[1],
    )
    args = cli.build_parser().parse_args(["ssh", "alpha", "-A"])
    assert cli.dispatch(args) == 5
    assert calls == [("alpha", ["-A"])]
