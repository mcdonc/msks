"""The client-held screenshot pipe (#1194): the holder's second
duty.

The pipe connects, like proxies, at startup: while the bridge
holder serves a workspace, it also keeps one ``ssh -N -R`` child
whose reverse forward asks the guest's own sshd to bind a loopback
TCP listener — the port the guest's skill dials. The other end of
the forward is a unix socket this holder owns on the client host,
and each guest connection to the port becomes, through the
forward, one connection to that socket.

The holder's accept loop answers each such connection by spawning
a fresh helper child
(:mod:`msks.client.screenshotserv`) with the connection as the
child's stdin and stdout — spawn, hand over, close this side's
copy, and let a waiter thread reap the exit. The holder itself
keeps no per-connection state beyond that waiter, which discards
itself when the child exits: a leak or a crash in one capture
ends one request, not the pipe.

The pipe exists exactly while a holder this client started serves
the workspace. The capability is that session: the ssh child
authenticates with the workspace's own identity through the same
transport ``msks ssh`` uses (the forward websocket as
ProxyCommand), the daemon relays its bytes and learns nothing, no
tap port joins the input chain, and stopping the holder removes
the guest's listener with it. A client with no
``screenshot_command`` configured spawns no pipe at all — the
command's presence is the enablement — and the guest's connection
refusal covers both that and a gone holder alike (the skill's own
remedy line carries the pair).

The guest learns one thing about the pipe (#1197): its port. One
short ssh exec, the forward child's transport with a command in
place of the forward, writes the port to :data:`PORT_FILE` in the
guest, where the guest's own tooling (the pi screenshot extension)
reads it. The default is the only thing the guest could assume
before, and ``screenshot_port`` breaks that assumption.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

from .config_keys import SCREENSHOT_PORT_ENV
from .hold import pipe_due
from .screenshotserv import (
    BUSY_RESPONSE,
    DEFAULT_PORT,
    MAX_CONCURRENT,
    WORKSPACE_ENV,
)
from .ssh import session_options

#: The ssh binary the pipe drives — the same one ``msks ssh`` and
#: the bridge's channels shell out to.
SSH = "ssh"

#: Seconds before the supervisor respawns a dead forward child:
#: a fast death retries at this pace, the holder serving on.
RETRY_S = 5.0

#: The slower pace a refused guest-side port retries at: the bind
#: failed because something in the guest holds the port, and a
#: retry cannot change that until the guest's service goes away.
BIND_RETRY_S = 30.0

#: The ssh stderr wording that names a refused remote bind — the
#: start-time failure the holder reports.
BIND_REFUSED = "remote port forwarding failed"

#: The module the holder spawns per connection — the helper that
#: answers the one exchange.
HELPER_MODULE = "msks.client.screenshotserv"

#: The guest-side file the holder writes its port to (#1197): the
#: guest's tooling reads it instead of assuming the default. Under
#: /tmp, world-readable: every guest account's agent reaches the
#: one pipe the workspace serves. A stale file is harmless — with
#: no holder the port is refused either way, and the next hold
#: rewrites it.
PORT_FILE = "/tmp/msks-screenshot-port"

#: Seconds the one-shot exec that writes the port file may take:
#: ssh through the forward websocket and one shell command. A child
#: past it is terminated and the write retried.
PORT_FILE_TIMEOUT_S = 30.0


def pipe_port(environ=None) -> int:
    """The guest-side port the forward asks sshd to bind: the
    variable's value, else the default. The variable was validated
    at config load; a holder that meets an unvalidated value (a
    hand-spawned child) refuses it by name rather than dialing a
    guessed port."""
    env = os.environ if environ is None else environ
    raw = env.get(SCREENSHOT_PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_PORT
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise SystemExit(
            f"msks: {SCREENSHOT_PORT_ENV} wants a port 1-65535, got {raw!r}"
        )
    return int(raw)


def socket_path(workspace_id: str, pid: int | None = None) -> Path:
    """The host-side unix socket this holder's pipe listens on:
    in the user's runtime directory — the namespace the daemon's
    own local client socket uses, short and per-user and alive
    exactly as long as the login session the holder lives in —
    one file per holder: the workspace's short hash beside the
    holder's pid, so two holders on one workspace (a manual hold
    beside a start's own) each keep their own pipe instead of
    colliding. A runtime directory whose depth would push the
    path past the socket's own 108-byte budget falls back to the
    system temp. The path unlinks when the pipe stops."""
    name = socket_name(workspace_id, pid)
    direct = runtime_dir() / name
    if len(str(direct)) < UNIX_PATH_BUDGET:
        return direct
    return Path(tempfile.gettempdir()) / name


#: The socket path's own budget: the kernel's ``sun_path`` carries
#: 108 bytes including the terminating null, so 107 is the deepest
#: path that can bind.
UNIX_PATH_BUDGET = 107


def runtime_dir() -> Path:
    """The listener socket's directory: ``XDG_RUNTIME_DIR`` when
    the host carries one (every systemd login does), else the
    system temp — a host without a runtime directory (a container)
    still serves the pipe."""
    override = os.environ.get("XDG_RUNTIME_DIR")
    if override and Path(override).is_absolute():
        return Path(override)
    return Path(tempfile.gettempdir())


def socket_name(workspace_id: str, pid: int | None = None) -> str:
    """The socket's file name: ``shot-<hash>-<pid>.sock`` — the
    workspace id hashed to eight hex characters (a workspace name
    can be any length the operator typed) and the spawning
    holder's pid for the per-holder uniqueness."""
    digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:8]
    return f"shot-{digest}-{pid or os.getpid()}.sock"


def reverse_argv(
    workspace_id: str,
    agent: str,
    identity: str,
    user: str,
    known_hosts: str,
    port: int,
    target: str,
) -> list[str]:
    """The forward child's ssh argv: the same transport, host-key,
    and identity options every msks ssh session carries, with ``-N``
    (no shell) and one reverse forward — the guest's sshd binds
    ``127.0.0.1:port`` and connects it to this holder's unix
    socket. ``ExitOnForwardFailure`` makes a refused bind an exit
    the supervisor reports, not a silent gap — and no ``-q``: a
    healthy forward child is silent on its own, and the quiet
    flag would suppress exactly the refusal and diagnostic lines
    :meth:`ScreenshotPipe.watch_stderr` reads."""
    return [
        SSH,
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        *session_options(workspace_id, agent, identity, known_hosts),
        "-l",
        user,
        "-R",
        f"127.0.0.1:{port}:{target}",
        workspace_id,
    ]


def port_file_argv(
    workspace_id: str,
    agent: str,
    identity: str,
    user: str,
    known_hosts: str,
    port: int,
) -> list[str]:
    """The one-shot exec that writes :data:`PORT_FILE` in the
    guest (#1197): the forward child's transport, host-key, and
    identity options with a command in place of ``-N`` and the
    forward. ``BatchMode`` keeps a holder that serves unattended
    from parking on a prompt it cannot answer.

    The login shell writes a fresh file and moves it over the
    path: the move is one rename, so a reader sees the whole port
    or the old file, never a half-written line, and a file another
    guest account planted at the path (a symlink among the
    sticky-dir tricks) is replaced, not followed. The file lands
    world-readable: the exec runs as the workspace's login user,
    and every guest account's agent reads it."""
    return [
        SSH,
        "-o",
        "BatchMode=yes",
        *session_options(workspace_id, agent, identity, known_hosts),
        "-l",
        user,
        workspace_id,
        f"f=$(mktemp {PORT_FILE}.XXXXXX) && printf '%s' {port} > \"$f\" "
        f'&& chmod 644 "$f" '
        f'&& {{ mv -fT "$f" {PORT_FILE} '
        f'|| {{ r=$?; rm -f "$f"; exit $r; }}; }}',
    ]


class ScreenshotPipe:
    """One holder's pipe: the unix listener, the reverse-forward
    child, the port-file exec, and the threads that serve them —
    two threads while idle (the supervisor and the accept loop),
    one short-lived port-file writer until the guest learns the
    port, and one short-lived waiter per connection.

    The ``ssh_runner`` and ``helper_runner`` seams exist for the
    tests; both default to :class:`subprocess.Popen`."""

    def __init__(
        self,
        workspace_id: str,
        agent: str,
        identity: str,
        user: str,
        known_hosts: str,
        name: str | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.agent = agent
        self.identity = identity
        self.user = user
        self.known_hosts = known_hosts
        self.name = name or workspace_id
        self.port = pipe_port()
        self.target = socket_path(workspace_id)
        self.ssh_runner = subprocess.Popen
        self.helper_runner = subprocess.Popen
        self.stopping = threading.Event()
        self.reported = False
        self.listener: socket.socket | None = None
        self.ssh: subprocess.Popen | None = None
        self.port_child: subprocess.Popen | None = None
        self.port_reported = False
        self.children: set[subprocess.Popen] = set()
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        """Bind the socket and start the two threads. The file is
        owner-only — a same-host user who is not this one cannot
        reach the pipe. A bind that fails (a runtime directory that
        cannot be written) raises — the caller names it and serves
        the bridge without the pipe."""
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.target))
        os.chmod(self.target, 0o600)
        listener.listen(8)
        self.listener = listener
        workers = (
            threading.Thread(
                target=self.supervise,
                name="msks-screenshot-forward",
                daemon=True,
            ),
            threading.Thread(
                target=self.accept_loop,
                args=(listener,),
                name="msks-screenshot-accept",
                daemon=True,
            ),
            threading.Thread(
                target=self.write_port_file,
                name="msks-screenshot-port",
                daemon=True,
            ),
        )
        for worker in workers:
            self.threads.append(worker)
            worker.start()

    def stop(self) -> None:
        """End the pipe: the listener closes (the accept loop
        leaves), every child the pipe spawned terminates — the
        forward child (the guest's listener goes with it, and every
        helper's connection closes under it), a port-file exec in
        flight, and a helper mid-capture (its own handler takes the
        capture command with it) — and the socket path unlinks."""
        self.stopping.set()
        if self.listener is not None:
            with suppress(OSError):
                self.listener.close()
        self.terminate_children()
        for thread in self.threads:
            thread.join(timeout=RETRY_S)
        self.target.unlink(missing_ok=True)

    def terminate_children(self) -> None:
        """SIGTERM to the forward child, the port-file exec, and
        every helper mid-capture: each takes its own side of the
        pipe down with it. A child already gone is not an error."""
        with self.lock:
            helpers = list(self.children)
        for child in (self.ssh, self.port_child, *helpers):
            if child is not None:
                with suppress(OSError):
                    child.terminate()

    def supervise(self) -> None:
        """Keep one forward child alive until the pipe stops: a
        death restarts at :data:`RETRY_S`, a refused guest-side
        bind (OpenSSH's own line, stderr-piped here) is reported
        once as the pipe's start-time failure and retried at the
        slower :data:`BIND_RETRY_S` pace — the guest's holder of
        the port must go away before a retry can land."""
        while not self.stopping.is_set():
            child = self.start_forward_child()
            if child is None:
                continue
            refused = self.watch_stderr(child)
            child.wait()
            if not self.stopping.is_set():
                self.stopping.wait(self.retry_after(refused))

    def start_forward_child(self):
        """Spawn one forward child, or None when it cannot start
        (a missing binary — the named line says so, and the slow
        retry paces the next attempt)."""
        argv = reverse_argv(
            self.workspace_id,
            self.agent,
            self.identity,
            self.user,
            self.known_hosts,
            self.port,
            str(self.target),
        )
        try:
            child = self.ssh_runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
        except OSError as exc:
            print(
                f"msks: the screenshot pipe's ssh did not start "
                f"({exc}); the guest port {self.port} will not serve",
                file=sys.stderr,
            )
            self.stopping.wait(BIND_RETRY_S)
            return None
        self.ssh = child
        return child

    def retry_after(self, refused: bool) -> float:
        """How long to wait before the next forward child: the slow
        pace for a refused bind (reported once — the guest's holder
        of the port must go away before a retry can land), the fast
        one for a plain death."""
        if not refused:
            return RETRY_S
        if not self.reported:
            self.reported = True
            print(
                f"msks: the screenshot pipe's guest port {self.port} "
                "was refused (something in the guest holds it); "
                "retrying",
                file=sys.stderr,
            )
        return BIND_RETRY_S

    def watch_stderr(self, child) -> bool:
        """The forward child's stderr, line by line, until it ends:
        True when a line named the refused remote bind, and every
        other line relayed to the holder's own stderr — a quiet
        pipe that never answers must be able to say why (an
        authentication refusal, a dead ProxyCommand) or the guest's
        connection-refused is the only symptom anyone ever sees."""
        refused = False
        stream = child.stderr
        if stream is None:
            return refused
        with stream:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if BIND_REFUSED in line:
                    refused = True
                    continue
                if line:
                    print(
                        f"msks: screenshot pipe ssh: {line}",
                        file=sys.stderr,
                    )
        return refused

    def write_port_file(self) -> None:
        """Tell the guest the port (#1197): one ssh exec, retried
        at the fast pace until it lands — the guest's sshd can
        come up after the pipe starts, the same race the
        supervisor waits out for the forward child. The first
        failure says why on the holder's stderr and the rest stay
        quiet.

        The stop path is checked twice: a stop that lands while
        the exec spawns (after the loop's check, before the child
        is recorded) takes the fresh child here, and a stop that
        lands while it runs ends the writer without a failure
        line (:meth:`wait_port_child`)."""
        while not self.stopping.is_set():
            child = self.start_port_child()
            if child is None:
                continue
            if self.stopping.is_set():
                self.end_port_child(child)
                return
            if self.wait_port_child(child):
                return

    def wait_port_child(self, child) -> bool:
        """One exec's outcome: True when the writer is done (the
        file landed, or the stop path took the exec), False when
        the write is retried. A child past
        :data:`PORT_FILE_TIMEOUT_S` is terminated and counted a
        failure; the exec's own ssh diagnostics reach the holder's
        stderr the way the forward child's do. A stop that landed
        while the exec ran is the writer's end, not a failure to
        report: the SIGTERM death is the stop."""
        try:
            code = child.wait(timeout=PORT_FILE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                child.terminate()
            self.watch_stderr(child)
            self.port_file_failed(
                f"ssh did not finish within {PORT_FILE_TIMEOUT_S:.0f}s"
            )
            return False
        self.watch_stderr(child)
        self.port_child = None
        if self.stopping.is_set() or code == 0:
            return True
        self.port_file_failed(f"ssh exited {code}")
        return False

    def end_port_child(self, child) -> None:
        """The stop path's view of the exec in flight: SIGTERM, and
        the record cleared."""
        with suppress(OSError):
            child.terminate()
        self.port_child = None

    def start_port_child(self):
        """Spawn the port-file exec, or None when it cannot start
        (a missing binary — the named line says so, and the retry
        paces the next attempt)."""
        argv = port_file_argv(
            self.workspace_id,
            self.agent,
            self.identity,
            self.user,
            self.known_hosts,
            self.port,
        )
        try:
            child = self.ssh_runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
        except OSError as exc:
            self.port_file_failed(f"ssh did not start ({exc})")
            return None
        self.port_child = child
        return child

    def port_file_failed(self, reason: str) -> None:
        """One failed port-file write: the reason on the holder's
        stderr once, then the fast retry pace. A stop that landed
        meanwhile prints nothing and wakes at once."""
        if self.stopping.is_set():
            return
        if not self.port_reported:
            self.port_reported = True
            print(
                f"msks: the screenshot pipe's port file did not reach "
                f"the guest ({reason}); retrying",
                file=sys.stderr,
            )
        self.stopping.wait(RETRY_S)

    def accept_loop(self, listener: socket.socket) -> None:
        """Serve connections until the pipe stops: each accepted
        connection goes to :meth:`answer`, and a listener error
        the stop path did not cause is reported and retried — the
        pipe keeps serving after a transient failure (descriptor
        exhaustion among them) instead of ending."""
        while not self.stopping.is_set():
            try:
                conn, _ = listener.accept()
            except OSError as exc:
                if self.stopping.is_set():
                    return
                self.listener_failed(exc)
                continue
            self.answer(conn)

    def listener_failed(self, exc: OSError) -> None:
        """One listener error the stop path did not cause: the
        named line, then the retry pace."""
        print(
            f"msks: the screenshot pipe's listener failed ({exc}); retrying",
            file=sys.stderr,
        )
        self.stopping.wait(RETRY_S)

    def answer(self, conn: socket.socket) -> None:
        """One accepted connection: the busy refusal inline beyond
        :data:`MAX_CONCURRENT` captures in flight, else one fresh
        helper child — spawn, hand over the connection, close this
        side's copy, and let a waiter thread reap. The holder keeps
        no state per connection beyond that waiter, which discards
        itself when the child exits."""
        if self.busy():
            with suppress(OSError):
                conn.sendall(BUSY_RESPONSE)
            with suppress(OSError):
                conn.close()
            return
        env = dict(os.environ)
        env[WORKSPACE_ENV] = self.name
        try:
            child = self.helper_runner(
                [sys.executable, "-m", HELPER_MODULE],
                stdin=conn,
                stdout=conn,
                stderr=None,
                env=env,
            )
        except OSError as exc:
            print(
                f"msks: the screenshot helper did not start ({exc})",
                file=sys.stderr,
            )
            with suppress(OSError):
                conn.close()
            return
        with suppress(OSError):
            conn.close()
        with self.lock:
            self.children.add(child)
        waiter = threading.Thread(
            target=self.reap,
            args=(child,),
            name="msks-screenshot-reap",
            daemon=True,
        )
        waiter.start()

    def busy(self) -> bool:
        """Whether the pipe is already running the most captures
        it runs at once: a guest that floods the port gets the
        inline refusal, not a pile of capture commands."""
        with self.lock:
            return len(self.children) >= MAX_CONCURRENT

    def reap(self, child) -> None:
        """One helper's exit, waited out and discarded from the
        children the stop path terminates."""
        child.wait()
        with self.lock:
            self.children.discard(child)


def pipe_from_env(
    workspace_id: str,
    agent: str,
    identity: str,
    user: str,
    known_hosts: str,
    name: str | None = None,
) -> ScreenshotPipe | None:
    """The holder's pipe for one workspace, or None when this
    client carries no screenshot command — the pipe is down by
    configuration, and the bridge serves on without it. A socket
    that cannot bind (a runtime directory that cannot be written)
    costs one stderr line, never the bridge."""
    if not pipe_due():
        return None
    pipe = ScreenshotPipe(
        workspace_id, agent, identity, user, known_hosts, name
    )
    try:
        pipe.start()
    except OSError as exc:
        print(
            f"msks: the screenshot pipe did not start ({exc}); "
            "the bridge serves without it",
            file=sys.stderr,
        )
        return None
    print(
        f"msks: screenshot pipe serving — the guest reaches it at "
        f"127.0.0.1:{pipe.port}",
        file=sys.stderr,
    )
    return pipe
