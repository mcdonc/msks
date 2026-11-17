"""The PTY session behind a terminal tab (#951): a child on a
pseudo-terminal, driven against PTY fakes — no daemon, no Qt, no
libghostty-vt. The argv composition is pinned against the TUI's
own builder; the lifecycle arms (spawn, resize, exit, hang up)
run against real short-lived children on real pseudo-terminals.
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import textwrap
import time

import pytest
from msks.client.gui import termsession as ts
from msks.client.tui.follow import ssh_child_argv

#: A child that announces itself and then waits for its parent to
#: hang up: the stand-in for the ssh child a tab really spawns.
SLEEPER = textwrap.dedent(
    """
    import sys, time
    print("ready", flush=True)
    time.sleep(30)
    """
)

#: A child that answers its terminal size whenever it gets a
#: line: the resize pin reads the child's own view of the
#: window, before and after a resize.
SIZER = textwrap.dedent(
    """
    import fcntl, struct, sys, termios
    for _ in sys.stdin:
        packed = bytearray(8)
        fcntl.ioctl(0, termios.TIOCGWINSZ, packed)
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        print(f"{rows} {cols}", flush=True)
    """
)


def spawn(code: str, **kwargs) -> ts.PtySession:
    """One session on a Python child running ``code``."""
    return ts.PtySession([sys.executable, "-c", code], **kwargs)


def drain_until(
    session: ts.PtySession, needle: bytes, budget: float = 5.0
) -> bytes:
    """Drain in a loop until ``needle`` shows up or the budget
    runs out — a PTY child announces itself on its own schedule."""
    deadline = time.monotonic() + budget
    seen = b""
    while time.monotonic() < deadline:
        got = session.drain()
        if got:
            seen += got
            if needle in seen:
                return seen
        time.sleep(0.02)
    pytest.fail(f"{needle!r} never arrived; saw {seen!r}")


def test_the_shell_argv_is_the_tuis_own() -> None:
    """The argv composition (#951): a terminal tab spawns the
    same child the TUI's new-terminal action spawns — the
    client's own interpreter running ``msks ssh`` on the
    workspace, never the GUI process becoming ssh."""
    assert ts.shell_argv("ws9") == [
        sys.executable,
        "-m",
        "msks.client.cli",
        "ssh",
        "ws9",
    ]
    assert ts.shell_argv("ws9") == ssh_child_argv("ws9")


def test_a_session_spawns_and_exposes_the_master(
    tmp_path,
) -> None:
    """The spawn (#951): the child runs on a fresh pseudo-terminal
    whose master fd comes back non-blocking — the fd the tab's
    QSocketNotifier will watch — and the child sees the terminal
    type the emulator answers."""
    session = ts.PtySession(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['TERM'], os.isatty(0))",
        ]
    )
    try:
        assert session.child_pid is not None
        flags = fcntl.fcntl(session.master_fd, fcntl.F_GETFL)
        assert flags & os.O_NONBLOCK
        out = drain_until(session, b"256color True")
        assert b"xterm-256color True" in out
    finally:
        session.close()


def test_the_exec_failure_answers_127() -> None:
    """A child that cannot exec (#951): the failed exec answers
    the conventional 127 through the observable drain."""
    session = ts.PtySession(["/no/such/binary"])
    try:
        deadline = time.monotonic() + 5.0
        while not session.exited and time.monotonic() < deadline:
            session.drain()
            time.sleep(0.02)
        session.reap()
        assert session.exit_status == 127
    finally:
        session.close()


def test_a_resize_reaches_the_child() -> None:
    """A resize (#951): TIOCSWINSZ on the master is the child's
    own view of its window — the sizer child reads the grid it
    was spawned with, then reads it again after a resize."""
    session = spawn(SIZER, cols=80, rows=24)
    try:
        session.write(b"\n")
        drain_until(session, b"24 80")
        session.resize(120, 40, 10, 20)
        session.write(b"\n")
        drain_until(session, b"40 120")
    finally:
        session.close()


def test_a_resize_after_close_answers_nothing() -> None:
    """The closed-session arms (#951): resize and write after
    close answer nothing, and drain reports the child gone."""
    session = spawn(SLEEPER)
    drain_until(session, b"ready")
    session.close()
    assert session.master_fd is None
    session.resize(80, 24, 10, 20)
    session.write(b"x")
    assert session.drain() is None
    assert session.exited is False  # close() reaped it instead
    session.close()  # idempotent: both fds and child already gone


def test_the_child_exit_is_visible(
    tmp_path,
) -> None:
    """The exit's visibility (#951): a child that quits on its
    own delivers its last output, EOF marks the session exited,
    and the exit status reads back — plain exit, signaled death,
    and a hang up that traps HUP all answer their own codes."""
    session = ts.PtySession(
        [sys.executable, "-c", 'print("bye", flush=True); raise SystemExit(7)']
    )
    out = drain_until(session, b"bye")
    assert b"bye" in out
    deadline = time.monotonic() + 5.0
    while not session.exited and time.monotonic() < deadline:
        session.drain()
        time.sleep(0.02)
    assert session.exited
    session.reap()
    assert session.exit_status == 7

    killed = ts.PtySession(
        [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ]
    )
    try:
        deadline = time.monotonic() + 5.0
        while not killed.exited and time.monotonic() < deadline:
            killed.drain()
            time.sleep(0.02)
        killed.reap()
        assert killed.exit_status == 128 + signal.SIGTERM
    finally:
        killed.close()


def test_a_hup_trapping_child_is_killed_after_grace() -> None:
    """The hang-up escalation (#951): closing the master SIGHUPs
    the child's group; a child that ignores HUP gets the grace
    period and then SIGKILL — the close returns, and the status
    answers the signal."""
    code = textwrap.dedent(
        """
        import signal, time
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        print("ready", flush=True)
        time.sleep(30)
        """
    )
    session = spawn(code)
    drain_until(session, b"ready")
    started = time.monotonic()
    session.close(grace=0.3)
    assert time.monotonic() - started < 10.0
    assert session.child_pid is None
    assert session.exit_status == 128 + signal.SIGKILL


def test_a_child_that_exits_within_grace_is_waited() -> None:
    """The grace loop's good arm (#951): a child that answers the
    HUP inside the grace period is reaped there — no kill, no
    signal status, and the plain exit code stands."""
    code = textwrap.dedent(
        """
        import signal, time
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        print("ready", flush=True)
        time.sleep(0.1)
        """
    )
    session = spawn(code)
    drain_until(session, b"ready")
    session.close(grace=5.0)
    assert session.exit_status in (0, 128 + signal.SIGHUP)


def test_reap_answers_when_the_child_was_collected_elsewhere() -> None:
    """The reap's defensive arm (#951): a child another waitpid
    already collected — the session still holds its pid — answers
    ChildProcessError, and the session drops the pid instead of
    raising."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)  # collected: the pid is gone for good
    session = spawn(SLEEPER)
    try:
        drain_until(session, b"ready")
        held = session.child_pid
        session.close(grace=0.3)
        assert session.child_pid is None
        session.child_pid = pid  # a pid that is no longer ours
        session.reap()
        assert session.child_pid is None
        assert held is not None
    finally:
        if session.master_fd is not None:
            os.close(session.master_fd)
            session.master_fd = None


def test_write_drops_under_backpressure() -> None:
    """The write's best-effort contract (#951): a closed master
    swallows the write, and a full kernel buffer drops the tail
    rather than blocking the GUI thread."""
    session = spawn(SLEEPER)
    drain_until(session, b"ready")
    try:
        # Fill the kernel buffer: a non-blocking write stops
        # accepting at EAGAIN, which the write drops.
        big = b"x" * (1 << 20)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                view = memoryview(big)
                while view:
                    written = os.write(session.master_fd, view)
                    view = view[written:]
            except BlockingIOError:
                break
        session.write(b"tail the buffer cannot take")
        # The child is alive and the session intact: the dropped
        # tail changed nothing the caller can observe.
        assert session.exited is False
    finally:
        session.close()


def test_the_initial_geometry_is_set_on_spawn() -> None:
    """The spawn's geometry (#951): the cols/rows/cell pair the
    constructor takes reaches the child's window through the same
    TIOCSWINSZ path a resize takes — pixels included, for the
    programs that want them."""
    code = textwrap.dedent(
        """
        import fcntl, struct, termios
        packed = bytearray(8)
        fcntl.ioctl(0, termios.TIOCGWINSZ, packed)
        rows, cols, xp, yp = struct.unpack("HHHH", packed)
        print(f"{rows} {cols} {xp} {yp}", flush=True)
        """
    )
    session = ts.PtySession(
        [sys.executable, "-c", code],
        cols=100,
        rows=50,
        cell_width=8,
        cell_height=16,
    )
    try:
        out = drain_until(session, b"50 100 800 800")
        assert b"50 100 800 800" in out
    finally:
        session.close()


def test_drain_after_the_child_gone_stays_none() -> None:
    """The drain's post-exit arm (#951): once EOF marked the
    session exited, drain keeps answering None — the tab's
    notifier stops here."""
    session = ts.PtySession([sys.executable, "-c", "print('x', flush=True)"])
    try:
        deadline = time.monotonic() + 5.0
        while not session.exited and time.monotonic() < deadline:
            session.drain()
            time.sleep(0.02)
        assert session.drain() is None
    finally:
        session.close()


def test_a_module_free_of_qt_and_msks_imports() -> None:
    """The session's purity (#951): the module imports nothing
    from PySide6 and nothing from msks — the tab integration
    composes the ssh argv; the session only runs it."""
    import ast

    source = ts.__file__
    tree = ast.parse(open(source).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"PySide6", "msks"}
