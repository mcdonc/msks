"""The shared socket's privileged proof (#1093): the daemon-side
chgrp hands the bound socket and its runtime directory to the
named group, and the socket file's own permission bits gate the
connect — a member of the group connects through the shared
socket, a non-member is refused at the connect. Root-only: the
lane that runs as root creates the accounts and groups a real
membership walk needs. No guest, no egress, no /dev/kvm.
"""

import grp
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from msks.server import localsocket

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="root only: creating accounts and groups needs it",
)

GROUP = "msks-smoke"
MEMBER = "msks-member"
OUTSIDER = "msks-outsider"

#: The connect probe, run as the named user through runuser: the
#: kernel's own verdict on the socket file's permission bits —
#: exit 0 when the connect completed, the traceback's name
#: (PermissionError) when the group gate refused it.
PROBE = (
    "import socket, sys\n"
    "probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "probe.settimeout(3)\n"
    "probe.connect(sys.argv[1])\n"
)


def sh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def ensure_group(name: str) -> None:
    try:
        grp.getgrnam(name)
    except KeyError:
        assert sh(["groupadd", name]).returncode == 0


def ensure_user(name: str, group: str | None) -> None:
    try:
        pwd.getpwnam(name)
    except KeyError:
        args = ["useradd", "-M"]
        if group is not None:
            args += ["-G", group]
        assert sh(args + [name]).returncode == 0


def connect_as(user: str, path: Path) -> subprocess.CompletedProcess:
    """One connect probe made as *user*: the member/outsider pair
    reads the group gate the way a real client dials it."""
    return sh(
        [
            "runuser",
            "-u",
            user,
            "--",
            "python3",
            "-c",
            PROBE,
            str(path),
        ]
    )


@pytest.fixture
def shared_socket():
    """A bound socket handed to the group, with a member and an
    outsider account to dial it — everything the pair needs on a
    host with a real account database, and nothing left behind."""
    ensure_group(GROUP)
    ensure_user(MEMBER, GROUP)
    ensure_user(OUTSIDER, None)
    # The runtime root must be traversable by the group: a pytest
    # tmp dir is 0700, and the member would be refused at the
    # directory, not the socket.
    run_root = Path(tempfile.mkdtemp(prefix="msks-shared-", dir="/tmp"))
    run_root.chmod(0o755)
    path = run_root / "msks" / "msksd.sock"
    sock = None
    try:
        sock = localsocket.bind(path)
        gid = localsocket.share_with_group(path, GROUP)
        yield path, gid
    finally:
        if sock is not None:
            sock.close()
        localsocket.unlink(path)
        shutil.rmtree(run_root, ignore_errors=True)
        sh(["userdel", MEMBER])
        sh(["userdel", OUTSIDER])
        sh(["groupdel", GROUP])


def test_the_shared_socket_serves_the_group_alone(shared_socket) -> None:
    path, gid = shared_socket
    group = grp.getgrgid(gid).gr_name
    assert group == GROUP
    # The handover's shape: the socket 0660 under the group, the
    # runtime directory 0750 with it — traverse to the socket,
    # nothing more.
    assert path.stat().st_gid == gid
    assert stat.S_IMODE(path.stat().st_mode) == 0o660
    assert path.parent.stat().st_gid == gid
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o750
    member = connect_as(MEMBER, path)
    assert member.returncode == 0, member.stderr
    outsider = connect_as(OUTSIDER, path)
    assert outsider.returncode != 0
    assert "PermissionError" in outsider.stderr
