"""The owner-exec helper's validation refusals (#1087).

Every malformed invocation exits nonzero and execs nothing. The
privileged paths — the setuid exec as the owner, the scrubbed
environment, the cleared groups, the dropped capabilities, the
audit line — run in the root-only smoke lane
(``test_smoke/egress/test_virtiofsd_as.py``); this module owns
the refusals the normal suite exercises as an unprivileged user.

Every refusal runs against two builds of the same source
(#1176): the plain ``-O2`` binary, and one under
AddressSanitizer and UndefinedBehaviorSanitizer with
``-fno-sanitize-recover`` — a parse that overflows or trips
undefined behavior fails there by its own exit, instead of
passing on the strength of the expected exit code alone.
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

#: The two builds every refusal runs against (#1176): the plain
#: optimized binary, and the same source under the sanitizers.
SANITIZER_FLAGS = (
    "-O1",
    "-g",
    "-fsanitize=address,undefined",
    "-fno-sanitize-recover=all",
)
BUILDS = {"plain": (), "sanitized": SANITIZER_FLAGS}


def build_helper(
    tmp_path: Path, target: str, extra_flags: tuple[str, ...] = ()
) -> Path:
    """Compile the helper with ``target`` as the fixed virtiofsd
    path: the exec target is compile-time, so the test points it
    at a stub that records any exec it survived to."""
    cc = shutil.which("cc")
    if cc is None:  # pragma: no cover -- a host without a C compiler
        pytest.skip("this shell carries no C compiler")
    out = tmp_path / "virtiofsd-as"
    # The sanitized build's stderr is captured so the fixture's
    # skip can carry it; the plain build's goes to the test's
    # stderr, where a failing gate shows it in pytest's output.
    captured = bool(extra_flags)
    subprocess.run(
        [
            cc,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            *extra_flags,
            f'-DVIRTIOFSD_PATH="{target}"',
            str(SOURCE),
            "-o",
            str(out),
        ],
        check=True,
        capture_output=captured,
        text=captured,
    )
    return out


@pytest.fixture(params=sorted(BUILDS), scope="module")
def helper(request, tmp_path_factory):
    """The compiled helper plus the exec-evidence marker: the stub
    virtiofsd touches the marker, so every refusal test asserts
    both the nonzero exit and that nothing exec'd. One build per
    module: the refusals share a binary the way a host does."""
    directory = tmp_path_factory.mktemp(f"virtiofsd-as-{request.param}")
    marker = directory / "execed"
    stub = directory / "virtiofsd"
    stub.write_text(f"#!/bin/sh\ntouch {marker}\n")
    stub.chmod(0o755)
    flags = BUILDS[request.param]
    try:
        binary = build_helper(directory, str(stub), flags)
    except subprocess.CalledProcessError as exc:
        if not flags:  # pragma: no cover -- the plain build is the gate
            raise
        # The plain build of the same source compiled under the
        # same -Werror; a sanitizer build that cannot link is the
        # shell lacking the runtime, not the source.
        pytest.skip(f"this shell's cc cannot build with {flags}: {exc.stderr}")
    return binary, marker


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
    ("plus-signed uid", ["+1", "--fd", "3", "--", "x"], "uid: expected"),
    ("empty uid", ["", "--fd", "3", "--", "x"], "uid: expected"),
    (
        "uid with a trailing space",
        ["1 ", "--fd", "3", "--", "x"],
        "uid: expected",
    ),
    (
        "uid with a leading space",
        [" 1", "--fd", "3", "--", "x"],
        "uid: expected",
    ),
    ("hex uid", ["0x3e8", "--fd", "3", "--", "x"], "uid: expected"),
    ("uid past uid_t", ["4294967295", "--fd", "3", "--", "x"], "uid: out"),
    (
        "uid far past uid_t",
        ["99999999999999999999", "--fd", "3", "--", "x"],
        "uid: out",
    ),
    ("uid one past uid_t", ["4294967296", "--fd", "3", "--", "x"], "uid: out"),
    (
        # The last usable uid parses; the refusal moves on to the fd.
        "uid at the last usable value",
        ["4294967294", "--fd", "x", "--", "x"],
        "fd: expected",
    ),
    ("non-numeric fd", ["1000", "--fd", "x", "--", "x"], "fd: expected"),
    ("signed fd", ["1000", "--fd", "-1", "--", "x"], "fd: expected"),
    (
        "fd with a trailing space",
        ["1000", "--fd", "3 ", "--", "x"],
        "fd: expected",
    ),
    ("hex fd", ["1000", "--fd", "0x3", "--", "x"], "fd: expected"),
    ("fd past int", ["1000", "--fd", "2147483648", "--", "x"], "fd: out"),
    (
        "fd far past int",
        ["1000", "--fd", "999999999999", "--", "x"],
        "fd: out",
    ),
    (
        "fd below three",
        ["1000", "--fd", "2", "--", "x"],
        "fd: 0, 1, and 2 are the standard streams",
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


def test_fd_at_int_max_is_a_system_refusal(helper):
    """The top of the fd range parses; fstat on it answers EBADF —
    the refusal is the system's (exit 1), not the parse's."""
    binary, marker = helper
    result = run(binary, ["1000", "--fd", "2147483647", "--", "x"])
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


def test_print_target_reports_the_fixed_path(helper):
    """The daemon's coherence probe (#1088): --print-target prints
    the compile-time virtiofsd path as one line and exits zero —
    no privilege, no exec behind it, and the fd the daemon bound
    is never touched (the arm answers before any fd parsing)."""
    binary, _marker = helper
    result = run(binary, ["--print-target"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(_marker.parent / "virtiofsd")
    assert "usage:" not in result.stderr
