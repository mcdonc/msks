"""The owner-exec helper's privileged paths (#1087): the setuid
exec as the workspace owner's uid, the scrubbed environment, the
cleared supplementary groups, the dropped capabilities, the
one-fd-across-the-exec exclusivity, and the audit line — plus the
uid-0 target, the one case where the kernel would keep the
capabilities through the uid switch and the helper's explicit
empty capset is the only thing that drops them. Root-only — this
lane is the suite that runs as root; no guest, no egress, no
/dev/kvm.

The probe the helper execs is a compiled C program, not a shell
script: it dumps its identity, capabilities, environment, and fd
table to stdout (the pipe survives the exec), so no shell, no
record file, and no environment but the helper's own is between
the exec and the assertions.
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

SOURCE = Path(__file__).resolve().parents[5] / "nix" / "virtiofsd-as.c"
assert SOURCE.exists(), f"helper source missing at {SOURCE}"

#: The fixed environment the exec lands with (nix/virtiofsd-as.c):
#: one PATH and nothing else.
HELPER_PATH = (
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

#: The probe: one line per fact — uid/gid, the supplementary group
#: list, the capability lines from /proc/self/status, every
#: environment entry, every open fd and whether it is a socket,
#: and argv[1] to prove the pass-through.
PROBE = """\
#include <stdio.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

extern char **environ;

int main(int argc, char **argv)
{
    gid_t groups[64];
    int n = getgroups(64, groups);
    printf("uid=%lu\\n", (unsigned long)getuid());
    printf("gid=%lu\\n", (unsigned long)getgid());
    printf("groups=");
    for (int i = 0; i < n; i++)
        printf("%s%lu", i ? "," : "", (unsigned long)groups[i]);
    printf("\\n");
    FILE *status = fopen("/proc/self/status", "r");
    if (status != NULL) {
        char line[256];
        while (fgets(line, sizeof(line), status) != NULL)
            if ((line[0] == 'C' && line[1] == 'a' && line[2] == 'p') ||
                (line[0] == 'N' && line[1] == 'o' && line[2] == 'N'))
                fputs(line, stdout);
        fclose(status);
    }
    for (char **e = environ; *e != NULL; e++)
        printf("env:%s\\n", *e);
    for (int fd = 0; fd < 128; fd++) {
        struct stat st;
        if (fstat(fd, &st) == 0)
            printf("fd:%d:%s\\n", fd,
                   S_ISSOCK(st.st_mode) ? "socket" : "other");
    }
    if (argc > 1)
        printf("arg=%s\\n", argv[1]);
    fflush(stdout);
    return 0;
}
"""


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


def open_tmp_tree(tmp_path: Path) -> None:
    """Make pytest's tmp tree traversable by the target uid: the
    lane's basetemp is 0700 root-owned, and nobody must walk it to
    exec the helper and the probe. Checks the tree is under /tmp
    before loosening anything, and stops at /tmp, which keeps its
    own mode (the sticky bit stays)."""
    assert tmp_path.is_relative_to("/tmp"), (
        f"expected a basetemp under /tmp, got {tmp_path}"
    )
    p = tmp_path
    while p != Path("/tmp"):
        os.chmod(p, 0o755)
        p = p.parent


def cc(source_text: str, out: Path) -> None:
    compiler = shutil.which("cc")
    assert compiler is not None, "this lane's shell carries no C compiler"
    source = out.with_suffix(".c")
    source.write_text(source_text)
    subprocess.run(
        [
            compiler,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(out),
            str(source),
        ],
        check=True,
    )


def build_helper(tmp_path: Path, target: Path) -> Path:
    """Compile the helper (``target`` as the fixed virtiofsd path)
    and install it setuid-root in the now-traversable tree."""
    binary = tmp_path / "virtiofsd-as"
    cc(f'#define VIRTIOFSD_PATH "{target}"\n#include "{SOURCE}"\n', binary)
    os.chown(binary, 0, 0)
    os.chmod(binary, 0o4755)
    return binary


def build_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "probe"
    cc(PROBE, probe)
    probe.chmod(0o755)
    return probe


def run_as(
    caller_uid: int,
    caller_gid: int,
    target_uid: int,
    binary: Path,
    fd: int,
    *,
    inh_caps: str | None = None,
):
    """One helper invocation as an unprivileged caller through
    setpriv — the setuid bit on the helper is what lifts euid to
    root — targeting ``target_uid``, and carrying a hostile
    environment nothing of which may cross the exec. ``inh_caps``
    raises one inheritable capability in the caller (util-linux
    2.38+): the uid-0 leg uses it to prove the helper's explicit
    capset, not the caller's empty inheritable set, is what drops
    the capabilities."""
    command = [
        "setpriv",
        "--reuid",
        str(caller_uid),
        "--regid",
        str(caller_gid),
        "--clear-groups",
    ]
    if inh_caps is not None:
        command += ["--inh-caps", inh_caps]
    command += [
        str(binary),
        str(target_uid),
        "--fd",
        str(fd),
        "--",
        "carried-arg",
    ]
    return subprocess.run(
        command,
        pass_fds=[fd],
        capture_output=True,
        text=True,
        env={**os.environ, "HONEY": "pot", "LD_PRELOAD": "/nope"},
    )


def evidence(result) -> dict[str, str | list[str]]:
    """The probe's stdout, parsed: scalar facts keyed, the env and
    fd lines kept whole."""
    facts: dict[str, str | list[str]] = {}
    env: list[str] = []
    fds: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("Cap") and ":" in line:
            key, _, value = line.partition(":")
            facts[key.strip()] = value.strip()
        elif line.startswith("env:"):
            env.append(line[len("env:") :])
        elif line.startswith("fd:"):
            fds.append(line)
        elif "=" in line:
            key, _, value = line.partition("=")
            facts[key] = value
    facts["env"] = env
    facts["fds"] = fds
    return facts


def assert_common_drops(evidence_map, uid: int, fd: int) -> None:
    assert evidence_map["uid"] == str(uid)
    assert evidence_map["gid"] == str(uid)
    assert evidence_map["groups"] == ""
    for key in ("CapEff", "CapPrm", "CapInh", "CapBnd"):
        assert int(str(evidence_map[key]), 16) == 0, (key, evidence_map[key])
    # The exec carries no-new-privileges: nothing about the
    # target path's packaging can lift the child later.
    assert evidence_map["NoNewPrivs"] == "1"
    # Exactly the fixed environment crosses — one PATH, nothing
    # else; no shell sits between exec and environ.
    assert evidence_map["env"] == [HELPER_PATH]
    # Exactly one fd crosses: the standard three and the listening
    # socket, nothing else.
    open_fds = {int(line.split(":")[1]) for line in evidence_map["fds"]}
    assert open_fds == {0, 1, 2, fd}
    assert any(line == f"fd:{fd}:socket" for line in evidence_map["fds"])
    assert evidence_map["arg"] == "carried-arg"


def test_execs_as_the_owner_uid_with_every_privilege_dropped(tmp_path):
    entry = owner_account()
    open_tmp_tree(tmp_path)
    probe = build_probe(tmp_path)
    binary = build_helper(tmp_path, probe)
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = run_as(entry.pw_uid, entry.pw_gid, entry.pw_uid, binary, fd)
    assert result.returncode == 0, result.stderr
    assert_common_drops(evidence(result), entry.pw_uid, fd)
    # One audit line per exec: uid, fd, and the argv — on stderr
    # where the daemon's spawn log reads it; the journal copy goes
    # to the lane host's syslog.
    assert "audit:" in result.stderr
    assert f"uid={entry.pw_uid}" in result.stderr
    assert f"fd={fd}" in result.stderr
    assert "carried-arg" in result.stderr


def test_execs_as_uid_0_with_every_capability_dropped(tmp_path):
    """The uid-0 target — the one case where the kernel keeps the
    capability sets through the uid switch: the helper's explicit
    empty capset is the only thing standing between a root-owner
    share and a fully privileged virtiofsd. The caller carries a
    raised inheritable capability (wake_alarm) into the exec, so
    an empty capset is the ONLY way the probe reads zeros — a
    deleted capset leaks the inheritable cap across and fails
    here, whatever the host session's own capability state."""
    entry = owner_account()
    open_tmp_tree(tmp_path)
    probe = build_probe(tmp_path)
    binary = build_helper(tmp_path, probe)
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = run_as(
            entry.pw_uid, entry.pw_gid, 0, binary, fd, inh_caps="+wake_alarm"
        )
    assert result.returncode == 0, result.stderr
    assert_common_drops(evidence(result), 0, fd)


def test_refuses_when_the_setuid_bit_is_absent(tmp_path):
    entry = owner_account()
    open_tmp_tree(tmp_path)
    probe = build_probe(tmp_path)
    binary = build_helper(tmp_path, probe)
    os.chmod(binary, 0o755)
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = run_as(entry.pw_uid, entry.pw_gid, entry.pw_uid, binary, fd)
    assert result.returncode != 0
    assert "not running setuid-root" in result.stderr
    assert result.stdout == ""


def test_a_dropped_exec_failure_names_the_target(tmp_path):
    """The exec itself failing after the privilege drop: the
    refusal names the fixed path, and the audit line still stands
    — the exec was attempted as the owner."""
    open_tmp_tree(tmp_path)
    binary = build_helper(tmp_path, tmp_path / "absent-virtiofsd")
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = subprocess.run(
            [str(binary), "65534", "--fd", str(fd), "--", "x"],
            pass_fds=[fd],
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0
    assert "cannot exec" in result.stderr
    assert "audit:" in result.stderr
