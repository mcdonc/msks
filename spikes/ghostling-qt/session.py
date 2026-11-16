"""A child process on a pseudo-terminal, the way Ghostling runs a
shell: forkpty, exec, non-blocking master fd.

The session is pure stdlib. The owning widget drives ``drain`` from a
QSocketNotifier and writes through ``write``; nothing here imports Qt.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import signal
import struct
import termios

TERM = "xterm-256color"


class PtySession:
    """One child process attached to a fresh pseudo-terminal."""

    def __init__(
        self,
        argv: list[str] | None = None,
        cols: int = 80,
        rows: int = 24,
        cell_width: int = 10,
        cell_height: int = 20,
    ) -> None:
        self.argv = argv or self.default_shell()
        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.exited = False
        self.exit_status: int | None = None
        pid, fd = pty.fork()
        if pid == 0:
            # Child: become the requested program on the slave side.
            try:
                os.environ["TERM"] = TERM
                os.execvp(self.argv[0], self.argv)
            except OSError:
                os._exit(127)
        self.child_pid = pid
        self.master_fd = fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.resize(cols, rows, cell_width, cell_height)

    @staticmethod
    def default_shell() -> list[str]:
        shell = os.environ.get("SHELL") or "/bin/sh"
        return [shell, "-i"]

    # --- data flow ----------------------------------------------------

    def write(self, data: bytes) -> None:
        """Best-effort write; drops the tail when the kernel buffer is
        full, matching what terminals do under back-pressure."""
        if self.master_fd is None:
            return
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.master_fd, view)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                return
            view = view[written:]

    def drain(self) -> bytes | None:
        """Read everything available. Returns None on EOF or error
        (the child is gone), the accumulated bytes otherwise (possibly
        empty when there was nothing to read)."""
        if self.master_fd is None or self.exited:
            return None
        chunks: list[bytes] = []
        while True:
            try:
                data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                # Linux reports EIO once the slave side is closed. The
                # kernel buffer still holds what the child wrote, so
                # deliver what we have; the next drain reports EOF.
                if exc.errno in (errno.EIO, errno.EBADF):
                    self._mark_exited()
                    return b"".join(chunks) if chunks else None
                return b"".join(chunks)
            if not data:
                self._mark_exited()
                return None
            chunks.append(data)
        return b"".join(chunks)

    # --- geometry -----------------------------------------------------

    def resize(
        self, cols: int, rows: int, cell_width: int, cell_height: int
    ) -> None:
        if self.master_fd is None:
            return
        winsize = struct.pack(
            "HHHH", rows, cols, cols * cell_width, rows * cell_height
        )
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    # --- lifecycle ----------------------------------------------------

    def _mark_exited(self) -> None:
        self.exited = True
        self.reap()

    def reap(self) -> None:
        if self.child_pid is None:
            return
        try:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            self.child_pid = None
            return
        if pid == 0:
            return
        self.child_pid = None
        if os.WIFEXITED(status):
            self.exit_status = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.exit_status = 128 + os.WTERMSIG(status)

    def close(self) -> None:
        """Hang up the child and release the master fd."""
        if self.child_pid is not None:
            try:
                os.kill(self.child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            self.reap()
            if self.child_pid is not None:
                try:
                    os.waitpid(self.child_pid, 0)
                except ChildProcessError:
                    pass
                self.child_pid = None
        if self.master_fd is not None:
            os.close(self.master_fd)
            self.master_fd = None
