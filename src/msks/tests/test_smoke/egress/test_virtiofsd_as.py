"""The owner-exec helper's privileged paths (#1087): the setuid
exec as the workspace owner's uid, the scrubbed environment, the
cleared supplementary groups, the dropped capabilities, and the
audit line. Root-only — this lane is the suite that runs as root;
no guest, no egress, no /dev/kvm.
"""

import os
import pwd
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="root only: the setuid paths need it",
)

SOURCE = Path(__file__).resolve().parents[4] / "nix" / "virtiofsd-as.c"

#: The stub the helper execs: it records its own identity,
#: capabilities, environment, and open fds — everything the
#: acceptance criteria name — to a file the test reads back.
STUB = """\
#!/bin/sh
out=$1
{
  echo "uid=$(id -u)"
  echo "gid=$(id -g)"
  sed -n 's/^Groups:[[:space:]]*/groups=/p' /proc/self/status
  sed -n 's/^CapEff:[[:space:]]*/capeff=/p' /proc/self/status
  sed -n 's/^CapBnd:[[:space:]]*/capbnd=/p' /proc/self/status
  env | LC_ALL=C sort
  for f in /proc/self/fd/*; do
    echo "fd:${f##*/}:$(readlink "$f")"
  done
} > "$out" 2>&1
"""

#: The fixed environment the exec lands with (nix/virtiofsd-as.c):
#: one PATH and nothing else.
HELPER_PATH = (
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def owner_account():
    """A passwd row with uid == gid — the user-private-group shape
    the helper's gid rule assumes; nobody/nogroup carries it on
    every distro this lane runs on."""
    if shutil.which("setpriv") is None:
        pytest.skip("util-linux setpriv not on this host")
    entry = pwd.getpwnam("nobody")
    if entry.pw_uid != entry.pw_gid:
        pytest.skip("nobody holds no user-private group on this host")
    return entry


def build_helper(tmp_path: Path, target: Path) -> tuple[Path, Path]:
    """Compile the helper (``target`` as the fixed virtiofsd path)
    and install it setuid-root, plus the world-writable record
    file the stub writes as the owner uid."""
    binary = tmp_path / "virtiofsd-as"
    cc = shutil.which("cc")
    assert cc is not None, "this lane's shell carries no C compiler"
    subprocess.run(
        [
            cc,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            f'-DVIRTIOFSD_PATH="{target}"',
            str(SOURCE),
            "-o",
            str(binary),
        ],
        check=True,
    )
    os.chown(binary, 0, 0)
    os.chmod(binary, 0o4755)
    record = tmp_path / "record"
    record.touch()
    os.chmod(record, 0o666)
    return binary, record


def write_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "virtiofsd"
    stub.write_text(STUB)
    stub.chmod(0o755)
    return stub


def record_lines(record: Path) -> dict[str, str]:
    """The stub's evidence, keyed: uid/gid/groups/caps are single
    lines; the environment and fd lines ride along unparsed."""
    lines = record.read_text().splitlines()
    keyed = {}
    env = []
    fds = []
    for line in lines:
        if line.startswith(("uid=", "gid=", "groups=", "capeff=", "capbnd=")):
            key, _, value = line.partition("=")
            keyed[key] = value
        elif "=" in line:
            env.append(line)
        else:
            fds.append(line)
    keyed["env"] = "\n".join(env)
    keyed["fds"] = "\n".join(fds)
    return keyed


def test_execs_as_the_owner_uid_with_every_privilege_dropped(tmp_path):
    entry = owner_account()
    stub = write_stub(tmp_path)
    binary, record = build_helper(tmp_path, stub)
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = subprocess.run(
            [
                "setpriv",
                "--reuid",
                str(entry.pw_uid),
                "--regid",
                str(entry.pw_gid),
                "--clear-groups",
                str(binary),
                str(entry.pw_uid),
                "--fd",
                str(fd),
                "--",
                str(record),
            ],
            pass_fds=[fd],
            capture_output=True,
            text=True,
            env={**os.environ, "HONEY": "pot", "LD_PRELOAD": "/nope"},
        )
    assert result.returncode == 0, result.stderr
    evidence = record_lines(record)
    assert evidence["uid"] == str(entry.pw_uid)
    assert evidence["gid"] == str(entry.pw_gid)
    assert evidence["groups"] == ""
    for key in ("capeff", "capbnd"):
        assert evidence[key] == "0000000000000000"
    # Nothing of the caller's environment crosses the exec: the
    # one fixed PATH, the shell's own PWD at most.
    env_names = {
        line.partition("=")[0] for line in evidence["env"].splitlines()
    }
    assert env_names <= {"PATH", "PWD"}
    assert HELPER_PATH in evidence["env"]
    # The listening fd survives the exec, still a socket.
    assert any(
        line.startswith(f"fd:{fd}:socket:")
        for line in evidence["fds"].splitlines()
    )
    # One audit line per exec: uid, fd, and the argv (the record
    # path rides in it) — on stderr where the daemon's spawn log
    # reads it; the journal copy goes to the lane host's syslog.
    assert "audit:" in result.stderr
    assert f"uid={entry.pw_uid}" in result.stderr
    assert f"fd={fd}" in result.stderr
    assert str(record) in result.stderr


def test_refuses_when_the_setuid_bit_is_absent(tmp_path):
    entry = owner_account()
    stub = write_stub(tmp_path)
    binary, record = build_helper(tmp_path, stub)
    os.chmod(binary, 0o755)
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = subprocess.run(
            [
                "setpriv",
                "--reuid",
                str(entry.pw_uid),
                "--regid",
                str(entry.pw_gid),
                "--clear-groups",
                str(binary),
                str(entry.pw_uid),
                "--fd",
                str(fd),
                "--",
                str(record),
            ],
            pass_fds=[fd],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0
    assert "not running setuid-root" in result.stderr
    assert record.read_text() == ""


def test_a_dropped_exec_failure_names_the_target(tmp_path):
    """The exec itself failing after the privilege drop: the
    refusal names the fixed path, and the audit line still stands
    — the exec was attempted as the owner."""
    binary, record = build_helper(tmp_path, tmp_path / "absent-virtiofsd")
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = subprocess.run(
            [
                str(binary),
                "65534",
                "--fd",
                str(fd),
                "--",
                str(record),
            ],
            pass_fds=[fd],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0
    assert "cannot exec" in result.stderr
    assert "audit:" in result.stderr
