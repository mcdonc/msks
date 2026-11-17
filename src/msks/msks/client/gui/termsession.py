"""A child process on a pseudo-terminal (#951): the process
behind a GUI terminal tab, ported from the spike (#935).

The session is pure stdlib — no Qt, no msks imports. The owning
tab (a later issue) drives :meth:`PtySession.drain` from a
``QSocketNotifier`` and writes through :meth:`PtySession.write`;
nothing here knows the GUI exists.

Production spawns the ssh argv the TUI builds today — the
client's own interpreter and module running ``msks ssh`` on the
workspace, which composes the real ssh command through
:mod:`msks.client.ssh` inside the child — on the PTY instead of
replacing the GUI process with it. :func:`shell_argv` builds that
argv; the suite pins it against the TUI's own builder.
"""

from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import time

#: The terminal type the child sees. The emulator the tab renders
#: through (#950) answers the 256-color xterm vocabulary.
TERM = "xterm-256color"

#: How long the close waits for the SIGKILLed child to become
#: collectable before abandoning it (#951): SIGKILL lands at
#: once, but uninterruptible sleep can hold the corpse.
KILL_WAIT_S = 0.5


def shell_argv(workspace_id: str) -> list[str]:
    """The argv a terminal tab spawns on its PTY (#951): this
    client's own interpreter and module running ``msks ssh`` on
    the workspace — the same child the TUI's new-terminal action
    spawns (#341). The child composes the real ssh command
    through :mod:`msks.client.ssh` itself, so the GUI process
    never becomes ssh and the child needs no connection flags:
    it reaches the same daemon by inheritance."""
    return [sys.executable, "-m", "msks.client.cli", "ssh", workspace_id]


class PtySession:
    """One child process attached to a fresh pseudo-terminal.

    ``argv`` is required — production passes :func:`shell_argv`'s
    ssh child, the tests their own fakes; the spike's ``$SHELL``
    default went with the port because a GUI tab always knows
    what it is running. The pair opens before the fork and is
    sized then, the child becomes a session leader on the slave,
    and the master fd is non-blocking and exposed as
    :attr:`master_fd` for the tab's ``QSocketNotifier``.
    """

    def __init__(
        self,
        argv: list[str],
        cols: int = 80,
        rows: int = 24,
        cell_width: int = 10,
        cell_height: int = 20,
    ) -> None:
        self.argv = list(argv)
        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.exited = False
        self.exit_status: int | None = None
        # The pair opens in the parent so the window is sized
        # before the child can exec: pty.fork cannot, and a child
        # reading its size at startup (ssh sizing the guest pty)
        # would race the parent's first ioctl.
        master, slave = pty.openpty()
        self.resize_fd(master, cols, rows, cell_width, cell_height)
        pid = os.fork()
        # The child block runs only in the fork — the parent's
        # coverage never sees it; the suite asserts its observable
        # outcomes instead (the TERM env, the controlling tty, and
        # the 127 exit of a failed exec through drain()).
        if pid == 0:  # pragma: no cover
            try:
                os.close(master)
                # CPython runs SIGPIPE ignored and the disposition
                # survives exec — real terminals restore it, or a
                # pipeline's reader gets EPIPE-less writes.
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                os.environ["TERM"] = TERM
                os.execvp(self.argv[0], self.argv)
            except OSError, ValueError, IndexError:
                # exec failures and argv shapes Python rejects
                # (an empty argv, an embedded NUL) all answer the
                # conventional 127.
                os._exit(127)
        os.close(slave)
        self.child_pid = pid
        self.master_fd = master
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # --- data flow ----------------------------------------------------

    def write(self, data: bytes) -> None:
        """Best-effort write; drops what does not fit when the
        kernel buffer is full, matching what terminals do under
        back-pressure. A closed master answers nothing."""
        if self.master_fd is None:
            return
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.master_fd, view)
            except OSError:
                return
            view = view[written:]

    def drain(self) -> bytes | None:
        """Read everything available. Returns ``None`` when the
        child is gone (EOF, or the EIO Linux reports once the
        slave side closes — the buffer still holds what the child
        wrote, so that path delivers the bytes it can and marks
        the exit), the accumulated bytes otherwise, possibly
        empty when there was nothing to read."""
        if self.master_fd is None or self.exited:
            return None
        return self.read_available()

    def read_available(self) -> bytes | None:
        """Everything readable right now: the accumulated bytes,
        ``None`` when the session is finished — or has no master
        to read — so the method is safe on a closed session too."""
        if self.master_fd is None or self.exited:
            return None
        return self.collect_reads()

    def collect_reads(self) -> bytes | None:
        """The drain loop over :meth:`read_once`, on a master
        known to be live."""
        chunks: list[bytes] = []
        while True:
            data, finished = self.read_once()
            if finished:
                return b"".join(chunks) if chunks else None
            if data is None:
                break  # nothing available right now
            chunks.append(data)
        return b"".join(chunks)

    def read_once(self) -> tuple[bytes | None, bool]:
        """One read off the master, on a master known to be
        live: the bytes (``None`` when nothing was available),
        and whether the child side is finished — EIO with the
        slave side closed (the kernel still holds the child's
        last output; this path delivers it), or the empty read
        macOS answers with."""
        try:
            data = os.read(self.master_fd, 65536)
        except BlockingIOError:
            return None, False
        except OSError:
            self.mark_exited()
            return None, True
        # Linux answers EIO, not a clean EOF, once the slave side
        # closes; macOS delivers the empty read here.
        if not data:  # pragma: no cover
            self.mark_exited()
            return None, True
        return data, False

    # --- geometry -----------------------------------------------------

    def resize(
        self, cols: int, rows: int, cell_width: int, cell_height: int
    ) -> None:
        """Carry a viewport resize to the child: the pixel size
        rides the same ``TIOCSWINSZ`` ioctl as the grid, for the
        programs that want both."""
        if self.master_fd is None:
            return
        self.resize_fd(self.master_fd, cols, rows, cell_width, cell_height)

    def resize_fd(
        self, fd: int, cols: int, rows: int, cell_width: int, cell_height: int
    ) -> None:
        """Carry the window's grid and pixel size to one pty fd
        — the master the tab resizes, or the freshly opened pair
        before the child execs."""
        winsize = struct.pack(
            "HHHH", rows, cols, cols * cell_width, rows * cell_height
        )
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    # --- lifecycle ----------------------------------------------------

    def mark_exited(self) -> None:
        """Record the end, release the master, and reap the
        child: nothing is deliverable after the child side
        finishes, so the fd closes here rather than leaking until
        an explicit close(). Called only with a live master — the
        reads that reach it have one."""
        self.exited = True
        os.close(self.master_fd)
        self.master_fd = None
        self.reap()

    def reap(self) -> None:
        """Collect the child without blocking, recording its exit
        status: 128 plus the signal for a signaled death, the
        plain code otherwise."""
        if self.child_pid is None:
            return
        try:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            # Someone else's waitpid(-1) already collected it.
            self.child_pid = None
            return
        if pid == 0:
            return
        self.child_pid = None
        self.exit_status = exit_code(status)

    def close(self, grace: float = 0.3) -> None:
        """Hang up the child and release the master fd without
        blocking the GUI: closing the master makes the kernel
        SIGHUP the child's foreground process group, then the
        child gets a short grace period before SIGKILL. A child
        that traps HUP cannot stall the window close."""
        if self.master_fd is not None:
            os.close(self.master_fd)
            self.master_fd = None
        if self.child_pid is None:
            return
        self.reap()
        self.await_exit(grace)
        if self.child_pid is not None:
            self.escalate()

    def await_exit(self, grace: float) -> None:
        """Give the hung-up child ``grace`` seconds to exit on
        its own, collecting it the moment it does."""
        deadline = time.monotonic() + grace
        while self.child_pid is not None and time.monotonic() < deadline:
            time.sleep(0.02)
            self.reap()

    def escalate(self) -> None:
        """Force-kill the child's whole process group: the child
        leads its own session, and grandchildren that trapped HUP
        survive a direct-child kill. The wait after the kill stays
        bounded — a child wedged in uninterruptible sleep (a hung
        mount) never becomes waitable, and the close must not
        block on it; such a child is abandoned, and its zombie
        stays until this process itself exits. The kill's error
        arms cover the child dying between the grace loop and
        here, or belonging to another user — races the suite
        cannot arrange."""
        try:
            os.killpg(os.getpgid(self.child_pid), signal.SIGKILL)
        except (  # pragma: no cover — the races named above
            ProcessLookupError,
            PermissionError,
        ):
            self.child_pid = None
            return
        self.await_exit(KILL_WAIT_S)
        self.child_pid = None


def exit_code(status: int) -> int:
    """The caller-facing exit status of one waitpid answer: 128
    plus the signal for a signaled death, the plain code
    otherwise — the convention shells report."""
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)
