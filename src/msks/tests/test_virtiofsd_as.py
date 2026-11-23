"""The owner-exec helper's validation refusals (#1087).

Every malformed invocation exits nonzero and execs nothing. The
privileged paths — the setuid exec as the owner, the scrubbed
environment, the cleared groups, the dropped capabilities, the
audit line — run in the root-only smoke lane
(``test_smoke/egress/test_virtiofsd_as.py``); this module owns
the refusals the normal suite exercises as an unprivileged user.
"""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the helper is a Linux setuid binary",
)

SOURCE = Path(__file__).resolve().parents[3] / "nix" / "virtiofsd-as.c"


def build_helper(tmp_path: Path, target: str) -> Path:
    """Compile the helper with ``target`` as the fixed virtiofsd
    path: the exec target is compile-time, so the test points it
    at a stub that records any exec it survived to."""
    cc = shutil.which("cc")
    if cc is None:  # pragma: no cover -- a host without a C compiler
        pytest.skip("this shell carries no C compiler")
    out = tmp_path / "virtiofsd-as"
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
            str(out),
        ],
        check=True,
    )
    return out


@pytest.fixture
def helper(tmp_path: Path):
    """The compiled helper plus the exec-evidence marker: the stub
    virtiofsd touches the marker, so every refusal test asserts
    both the nonzero exit and that nothing exec'd."""
    marker = tmp_path / "execed"
    stub = tmp_path / "virtiofsd"
    stub.write_text(f"#!/bin/sh\ntouch {marker}\n")
    stub.chmod(0o755)
    return build_helper(tmp_path, str(stub)), marker


def run(binary: Path, args: list[str], *fds: int):
    """One helper invocation over a hostile environment: nothing
    about the caller crosses the exec either way, and the
    refusals never read the environment at all."""
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        pass_fds=list(fds),
        env={**os.environ, "HONEY": "pot", "LD_PRELOAD": "/nope"},
    )


#: (name, argv after the binary, the stderr the refusal names) —
#: every shape error the parse refuses, before any state changes.
SHAPE_CASES = [
    ("no arguments", [], "usage:"),
    ("uid only", ["1000"], "usage:"),
    ("missing separator", ["1000", "--fd", "3", "x"], "usage:"),
    ("empty argv tail", ["1000", "--fd", "3", "--"], "usage:"),
    ("misordered fd flag", ["1000", "--", "--fd", "3", "x"], "usage:"),
    ("non-numeric uid", ["abc", "--fd", "3", "--", "x"], "uid: expected"),
    ("signed uid", ["-1", "--fd", "3", "--", "x"], "uid: expected"),
    ("empty uid", ["", "--fd", "3", "--", "x"], "uid: expected"),
    ("uid past uid_t", ["4294967296", "--fd", "3", "--", "x"], "uid: out"),
    (
        "uid far past uid_t",
        ["99999999999999999999", "--fd", "3", "--", "x"],
        "uid: out",
    ),
    ("non-numeric fd", ["1000", "--fd", "x", "--", "x"], "fd: expected"),
    ("signed fd", ["1000", "--fd", "-1", "--", "x"], "fd: expected"),
    ("fd past int", ["1000", "--fd", "2147483648", "--", "x"], "fd: out"),
    (
        "fd far past int",
        ["1000", "--fd", "999999999999", "--", "x"],
        "fd: out",
    ),
]


@pytest.mark.parametrize(
    ("name", "args", "message"),
    SHAPE_CASES,
    ids=[case[0] for case in SHAPE_CASES],
)
def test_malformed_invocation_refuses(helper, name, args, message):
    binary, marker = helper
    result = run(binary, args)
    # Usage refusals carry their own exit code (2), apart from
    # the system-failure exit (1) — the seam 2b's daemon reads.
    assert result.returncode == 2
    assert message in result.stderr
    assert not marker.exists()


def test_refuses_a_closed_fd(helper):
    binary, marker = helper
    result = run(binary, ["1000", "--fd", "93", "--", "x"])
    # A system failure, not a usage error: exit 1.
    assert result.returncode == 1
    assert "the listening fd is not open" in result.stderr
    assert not marker.exists()


def test_refuses_a_non_socket_fd(helper):
    binary, marker = helper
    read, _write = os.pipe()
    result = run(binary, ["1000", "--fd", str(read), "--", "x"], read)
    assert result.returncode != 0
    assert "the fd is not a socket" in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="as root the exec path proceeds; the smoke lane owns it",
)
def test_valid_shape_refuses_without_the_setuid_bit(helper):
    binary, marker = helper
    ours, _theirs = socket.socketpair()
    with ours:
        result = run(
            binary,
            ["1000", "--fd", str(ours.fileno()), "--", "x"],
            ours.fileno(),
        )
    assert result.returncode != 0
    assert "not running setuid-root" in result.stderr
    assert not marker.exists()
