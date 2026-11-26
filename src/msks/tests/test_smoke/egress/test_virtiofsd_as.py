"""The owner-exec helper's privileged paths (#1087): the setuid
exec as the workspace owner's uid, the scrubbed environment, the
cleared supplementary groups, the dropped capabilities, the
one-fd-across-the-exec exclusivity, and the audit line — plus the
uid-0 target, the one case where the kernel would keep the
capabilities through the uid switch and the helper's explicit
empty capset is the only thing that drops them. Root-only — this
lane is the suite that runs as root; no guest, no egress, no
/dev/kvm.

The last test closes the #1088 chain this file's halves leave
open: the daemon's own spawn path — the real bind, the real
helper setuid-root with the host's real virtiofsd as its fixed
exec target, the real serve wait — runs a serving virtiofsd as
the workspace owner's uid behind the daemon-bound socket.

The probe the helper execs is a compiled C program, not a shell
script: it dumps its identity, capabilities, environment, and fd
table to stdout (the pipe survives the exec), so no shell, no
record file, and no environment but the helper's own is between
the exec and the assertions.
"""

import asyncio
import contextlib
import os
import pwd
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from msks.microvm.fsdaemons import virtiofsd_meets_floor, virtiofsd_version
from msks.microvm.local import LocalCloudHypervisor, fs_exit_nudge
from msks.settings import (
    MountSettings,
    Settings,
    VmmSettings,
    resolve_virtiofsd,
)
from msks.spec.mounts import MountSpec, mount_tag
from msks.spec.vm import VmSpec

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
#: list, the capability and signal lines from /proc/self/status,
#: every environment entry, every open fd and whether it is a
#: socket, and every argument after argv[0] as hex, so a control
#: byte in an argument never breaks the evidence's own lines.
PROBE = """\
#include <stdio.h>
#include <string.h>
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
                (line[0] == 'N' && line[1] == 'o' && line[2] == 'N') ||
                (line[0] == 'S' && line[1] == 'i' && line[2] == 'g'))
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
    for (int i = 1; i < argc; i++) {
        printf("arg%d=", i);
        for (const unsigned char *p = (const unsigned char *)argv[i]; *p; p++)
            printf("%02x", *p);
        printf("\\n");
    }
    fflush(stdout);
    return 0;
}
"""

#: The linger probe (#1176): records its pid where the test can
#: read it, then sleeps until a signal ends it — the stand-in for
#: a serving virtiofsd, which nothing but a signal stops.
LINGER = """\
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc < 2)
        return 64;
    FILE *pid = fopen(argv[1], "w");
    if (pid == NULL)
        return 65;
    fprintf(pid, "%ld\\n", (long)getpid());
    fclose(pid);
    for (;;)
        pause();
}
"""

#: The hostile parent (#1176): SIGTERM ignored and every signal
#: blocked — the state a daemon could leave in the thread that
#: forks — then the helper's exec in a forked child, a wait for
#: the linger probe's pid file, and "ready" on stdout. It then
#: sleeps for good; only SIGKILL ends it, which is the parent
#: death the test stages.
IGNORING_PARENT = """\
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    int sv[2];
    sigset_t all;
    pid_t child;
    if (argc != 4 || socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0)
        return 65;
    if (signal(SIGTERM, SIG_IGN) == SIG_ERR)
        return 66;
    sigfillset(&all);
    if (sigprocmask(SIG_BLOCK, &all, NULL) != 0)
        return 66;
    child = fork();
    if (child < 0)
        return 67;
    if (child == 0) {
        char fd[16];
        snprintf(fd, sizeof(fd), "%d", sv[0]);
        execl(argv[1], argv[1], argv[2], "--fd", fd, "--", argv[3],
              (char *)NULL);
        _exit(127);
    }
    for (int i = 0; i < 200; i++) {
        struct stat st;
        if (stat(argv[3], &st) == 0) {
            printf("ready\\n");
            fflush(stdout);
            for (;;)
                pause();
        }
        usleep(50000);
    }
    return 68;
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


def build_program(tmp_path: Path, name: str, source: str) -> Path:
    """One more C program of the lane's own, runnable by the
    owner uid."""
    out = tmp_path / name
    cc(source, out)
    out.chmod(0o755)
    return out


def process_state(pid: int) -> str | None:
    """The state letter of /proc/<pid>/stat, None once the pid is
    gone. A zombie (Z) is a process that died and nobody reaped
    yet — dead all the same."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return stat.rpartition(")")[2].split()[0]


def gone_within(pid: int, seconds: float) -> bool:
    """True when the pid is dead (gone or a zombie) within the
    window."""
    deadline = time.monotonic() + seconds
    while True:
        state = process_state(pid)
        if state is None or state == "Z":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def run_as(
    caller_uid: int,
    caller_gid: int,
    target_uid: int,
    binary: Path,
    fd: int,
    *,
    inh_caps: str | None = None,
    ambient_caps: str | None = None,
    argv: tuple[str, ...] = ("carried-arg",),
):
    """One helper invocation as an unprivileged caller through
    setpriv — the setuid bit on the helper is what lifts euid to
    root — targeting ``target_uid``, and carrying a hostile
    environment nothing of which may cross the exec. ``inh_caps``
    raises one inheritable capability in the caller and
    ``ambient_caps`` one ambient (util-linux 2.38+): the uid-0 leg
    uses them to prove the helper's explicit capset, not the
    caller's sets, is what drops the capabilities."""
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
    if ambient_caps is not None:
        command += ["--ambient-caps", ambient_caps]
    command += [
        str(binary),
        str(target_uid),
        "--fd",
        str(fd),
        "--",
        *argv,
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
        if (
            line.startswith("Cap")
            or line.startswith("Sig")
            or line.startswith("NoNewPrivs")
        ) and ":" in line:
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
    for key in ("CapEff", "CapPrm", "CapInh", "CapBnd", "CapAmb"):
        assert int(str(evidence_map[key]), 16) == 0, (key, evidence_map[key])
    # The exec carries no-new-privileges: nothing about the
    # target path's packaging can lift the child later.
    assert evidence_map["NoNewPrivs"] == "1"
    # The caller's signal state does not cross (#1176): nothing
    # ignored, nothing blocked.
    assert int(str(evidence_map["SigIgn"]), 16) == 0, evidence_map["SigIgn"]
    assert int(str(evidence_map["SigBlk"]), 16) == 0, evidence_map["SigBlk"]
    # Exactly the fixed environment crosses — one PATH, nothing
    # else; no shell sits between exec and environ.
    assert evidence_map["env"] == [HELPER_PATH]
    # Exactly one fd crosses: the standard three and the listening
    # socket, nothing else.
    open_fds = {int(line.split(":")[1]) for line in evidence_map["fds"]}
    assert open_fds == {0, 1, 2, fd}
    assert any(line == f"fd:{fd}:socket" for line in evidence_map["fds"])
    assert bytes.fromhex(str(evidence_map["arg1"])) == b"carried-arg"


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
    here, whatever the host session's own capability state.

    The caller also carries that capability ambient (#1176) — the
    daemon's own hosting condition, msks-caps raising
    CAP_NET_ADMIN and CAP_NET_BIND_SERVICE ambient — and the
    probe's ambient set must read zero as well. The kernel
    empties the ambient set at the exec of a setuid binary
    (capabilities(7): a privileged file clears it) before the
    helper's first instruction runs, and the empty capset keeps
    it empty; the assertion pins both, so a future lane that
    reaches the helper without the setuid exec still answers."""
    entry = owner_account()
    open_tmp_tree(tmp_path)
    probe = build_probe(tmp_path)
    binary = build_helper(tmp_path, probe)
    server, client = socket.socketpair()
    fd = server.fileno()
    with server, client:
        result = run_as(
            entry.pw_uid,
            entry.pw_gid,
            0,
            binary,
            fd,
            inh_caps="+wake_alarm",
            ambient_caps="+wake_alarm",
        )
    assert result.returncode == 0, result.stderr
    assert_common_drops(evidence(result), 0, fd)


def test_parent_death_ends_the_share_though_the_parent_ignores_sigterm(
    tmp_path,
):
    """The helper's parent-death signal is SIGTERM, and both an
    ignored disposition and a blocked mask cross an execve
    (#1176): a daemon that left either behind in the thread that
    forked would arm a signal the share's virtiofsd never takes,
    and every helper-lane share would outlive the dead daemon.
    The helper resets the signal state before the exec, so the
    share dies with its parent whatever the parent's state. The
    hostile parent ignores SIGTERM and blocks every signal, then
    forks the helper; the linger probe stands in for virtiofsd.
    It lives while the parent lives — the parent-death signal is
    the only thing that takes it — and dies once the parent
    does."""
    entry = owner_account()
    open_tmp_tree(tmp_path)
    pids = tmp_path / "pids"
    pids.mkdir()
    os.chown(pids, entry.pw_uid, entry.pw_gid)
    linger = build_program(tmp_path, "linger", LINGER)
    parent = build_program(tmp_path, "ignoring-parent", IGNORING_PARENT)
    binary = build_helper(tmp_path, linger)
    pidfile = pids / "linger.pid"
    proc = subprocess.Popen(
        [
            "setpriv",
            "--reuid",
            str(entry.pw_uid),
            "--regid",
            str(entry.pw_gid),
            "--clear-groups",
            str(parent),
            str(binary),
            str(entry.pw_uid),
            str(pidfile),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = None
    try:
        readable = select.select([proc.stdout], [], [], 10.0)[0]
        assert readable, "the parent never reached the pid file"
        line = proc.stdout.readline()
        assert line == "ready\n", (
            f"the parent exited {proc.wait()} without the pid file: "
            f"{proc.stderr.read()}"
        )
        pid = int(pidfile.read_text())
        # Alive while its parent lives.
        time.sleep(0.5)
        assert process_state(pid) not in (None, "Z"), (
            "the share died before its parent"
        )
        # The parent dies: the kernel's parent-death signal takes
        # the share, ignored SIGTERM and blocked mask or not.
        proc.kill()
        proc.wait(timeout=10.0)
        assert gone_within(pid, 5.0), (
            f"the share (pid {pid}) outlived its dead parent"
        )
        assert "audit:" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            stream.close()
        if pid is not None:
            # A failed assertion leaves the share behind; take it.
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_audit_line_cannot_be_forged(tmp_path):
    """The audit line's author is the helper (#1176): control
    bytes in the passed argv become '?', so no argument can forge
    a second journal line under the helper's ident, and a long
    argv truncates at the line's bound. The exec'd virtiofsd still
    receives every argument verbatim."""
    entry = owner_account()
    open_tmp_tree(tmp_path)
    probe = build_probe(tmp_path)
    binary = build_helper(tmp_path, probe)
    server, client = socket.socketpair()
    fd = server.fileno()
    long_arg = "a" * 5000
    argv = ("carried\narg", "x\x1b[31m", long_arg)
    with server, client:
        result = run_as(
            entry.pw_uid, entry.pw_gid, entry.pw_uid, binary, fd, argv=argv
        )
    assert result.returncode == 0, result.stderr
    prefix = "virtiofsd-as: audit: "
    audit_lines = [
        line for line in result.stderr.splitlines() if line.startswith(prefix)
    ]
    assert len(audit_lines) == 1, result.stderr
    line = audit_lines[0]
    assert "argv=carried?arg x?[31m " in line
    assert "\x1b" not in line
    assert len(line) <= len(prefix) + 4095
    assert line.count("a") < len(long_arg)
    # The exec'd virtiofsd gets the argv verbatim: the scrub is
    # the log's, not the child's.
    facts = evidence(result)
    assert bytes.fromhex(str(facts["arg1"])) == b"carried\narg"
    assert bytes.fromhex(str(facts["arg2"])) == b"x\x1b[31m"
    assert bytes.fromhex(str(facts["arg3"])) == long_arg.encode()


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


async def test_the_daemon_chain_serves_a_real_share_as_the_owner(
    tmp_path,
):
    """The whole #1088 chain on one host: the daemon's own spawn
    path binds the share socket, execs the setuid helper, and the
    helper execs the host's REAL virtiofsd at its fixed target —
    a serving daemon behind the daemon-bound socket, running as
    the workspace owner's uid, killed by the nudge's disconnect
    exit and cleaned by the daemon's own teardown. The hermetic
    suite shape-matches this chain; this lane runs it."""
    entry = owner_account()
    open_tmp_tree(tmp_path)
    # The daemon's own resolution rule, environment included: the
    # smoke lane's packaged virtiofsd answers the bare name, a
    # NixOS dev host points MSKSD_VIRTIOFSD at the store path.
    configured = os.environ.get("MSKSD_VIRTIOFSD", "virtiofsd")
    virtiofsd = Path(resolve_virtiofsd(configured))
    if not (virtiofsd.is_file() and os.access(virtiofsd, os.X_OK)):
        pytest.skip("no real virtiofsd on this host")
    report = subprocess.run(
        [str(virtiofsd), "--version"], capture_output=True, text=True
    )
    if not virtiofsd_meets_floor(virtiofsd_version(report.stdout)):
        pytest.skip("the host's virtiofsd sits below the 1.13 floor")

    # A shallow state root (the hermetic suite's rule): the
    # daemon-bound socket must stay under AF_UNIX's limit.
    state = Path(tempfile.mkdtemp(prefix="msks-smoke-", dir="/tmp"))
    vm_dir = state / "vms" / "ws-chain"
    vm_dir.mkdir(parents=True)
    share = tmp_path / "share"
    share.mkdir()
    share.chmod(0o755)  # the owner uid may read what it serves
    helper = build_helper(tmp_path, virtiofsd)

    class App:
        pass

    app = App()
    app.state = type("State", (), {})()
    app.state.settings = Settings(
        vmm=VmmSettings(
            state_dir=state,
            virtiofsd=str(virtiofsd),
            virtiofsd_as=str(helper),
        )
    )
    app.state.settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver = LocalCloudHypervisor(app)
    spec = VmSpec(
        workspace_id="ws-chain",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        owner_uid=entry.pw_uid,
        mounts=(MountSpec(host=str(share), guest="/mnt/share"),),
    )
    tag = mount_tag(spec.mounts[0])
    socket_path = vm_dir / f"fs-{tag}.sock"
    pidfile = vm_dir / f"fs-{tag}.pid"
    try:
        entries = await driver._start_fs_daemons(spec, vm_dir)
        assert entries[0]["tag"] == tag
        proc = driver._fs_procs["ws-chain"][0]
        # The serving identity: the real child runs as the
        # workspace's owner — the whole point of the lane.
        status = Path(f"/proc/{proc.pid}/status").read_text()
        assert f"Uid:\t{entry.pw_uid}\t" in status
        # The daemon-bound socket serves, and the child's argv is
        # the --fd shape (no socket path anywhere in it).
        assert socket_path.exists()
        cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        assert b"--fd" in cmdline
        assert b"--socket-path" not in cmdline
        # The nudge's disconnect exit — the lever for a daemon
        # that cannot signal this child — ends it.
        fs_exit_nudge(socket_path)
        await asyncio.wait_for(proc.wait(), 10.0)
        # The teardown completes and leaves no residue behind.
        await driver._stop_fs_daemons("ws-chain")
        assert not socket_path.exists()
        assert not pidfile.exists()
    finally:
        await driver._stop_fs_daemons("ws-chain")
        shutil.rmtree(state, ignore_errors=True)
