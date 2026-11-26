"""The screenshot pipe's per-connection helper (#1194).

The holder spawns one fresh child per guest request — ``python -m
msks.client.screenshotserv`` — with the accepted connection as the
child's stdin and stdout, so the helper answers exactly one HTTP
exchange and exits: a fresh process tree per capture bounds memory
(a leak or a crash ends one request, not the pipe) and isolates
the capture command's failures from the holder serving the rest.

The helper is the wire's host end. The request is one GET line
(``GET /screenshot?monitor=N``); the answer is the capture
command's stdout with a length header, or one of the refusal
shapes the guest skill branches on:

- ``400`` — a malformed request: a bad selector, an unknown
  parameter, a method or path this surface does not speak. The
  body names the rule and the valid spellings.
- ``500`` — the capture command failed: its stderr tail verbatim,
  a timeout, or the missing configuration (the helper runs only
  when the holder found a command configured, but the environment
  it reads is the one it was spawned with — the answer stays
  honest if the two ever disagree).

The command's argv comes from the environment the holder passed
( :data:`SCREENSHOT_COMMAND_ENV`, the materialized
``screenshot_command`` setting) and from nowhere else: the
request's selector is validated to a whole number and handed to
the command as the :data:`MONITOR_ENV` variable, never spliced
into the command line — a request that could shape argv would be
host code execution, and this is the seam that must refuse it.

One JSONL line lands under the client state root
(:data:`HISTORY_NAME`) per command run — the pipe's whole audit
trail, the daemon holding none of it.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime

from .config_keys import SCREENSHOT_COMMAND_ENV
from .sshsessions import state_dir

#: The pipe's default guest-side port (#1194): the loopback TCP
#: port the guest's sshd binds when the holder's reverse forward
#: lands. The ``screenshot_port`` setting overrides it.
DEFAULT_PORT = 8662

#: The trusted, holder-set environment variable carrying the
#: workspace's human-facing name, for the audit line and a
#: notify-per-capture command's message. It is set host-side at
#: spawn time; nothing from the request reaches it.
WORKSPACE_ENV = "MSKS_SCREENSHOT_WORKSPACE"

#: The selector's validated form as the capture command reads it:
#: a whole number 0 or greater, handed to the command as this
#: variable. It never joins the command's argv.
MONITOR_ENV = "MSKS_SCREENSHOT_MONITOR"

#: The one request path this surface speaks.
REQUEST_PATH = "/screenshot"

#: The audit trail's file name, one line per command run, directly
#: under the client state root.
HISTORY_NAME = "screenshots.jsonl"

#: The request header budget: one request line and its headers
#: must fit; a request that never ends is a refusal, not a read
#: until the connection does.
HEADER_LIMIT = 16 * 1024

#: How long the capture command may run before the helper kills
#: it and answers the timeout: a hung command must not hold the
#: connection (and the helper) open — the guest's own client
#: timeout lands first, but the helper cannot assume one.
CAPTURE_TIMEOUT_S = 60.0

#: The response body budget: a command that writes more than this
#: is refused rather than ferried whole.
OUTPUT_LIMIT = 64 * 1024 * 1024

#: How much of a failed command's stderr the ``500`` body carries:
#: the tail, where the actual error lives.
STDERR_TAIL = 500

#: The most captures one client runs at once (#1194): beyond this
#: the holder's accept loop answers the busy refusal inline — a
#: guest that floods the port gets cheap 503s, not a pile of
#: capture commands on the operator's desktop.
MAX_CONCURRENT = 4

#: The busy refusal's body: one line, the holder writes it inline
#: without spawning anything.
BUSY_BODY = b"msks: too many captures are already running; retry\n"

#: The busy refusal, whole response bytes, one sendall.
BUSY_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    + f"Content-Length: {len(BUSY_BODY)}\r\n".encode("ascii")
    + b"Connection: close\r\n"
    b"\r\n" + BUSY_BODY
)

#: How long the request's head may take to arrive: a connection
#: that sends nothing answers nothing and holds its capture slot
#: for this long, never the holder's lifetime (#1196 round 2).
HEAD_TIMEOUT_S = 30.0

#: The capture command currently running, for the stop path: a
#: SIGTERM'd helper terminates it, so a stop leaves no capture
#: command (a blocking prompt among them) running on.
_running: subprocess.Popen | None = None


def request_argv() -> list[str] | None:
    """The capture command's argv: the shell-split
    :data:`SCREENSHOT_COMMAND_ENV` the holder inherited — the
    materialized ``screenshot_command`` setting. An empty value is
    the unconfigured form (None); the helper that runs without one
    answers the named refusal rather than guessing a program."""
    raw = os.environ.get(SCREENSHOT_COMMAND_ENV, "")
    if not raw.strip():
        return None
    return shlex.split(raw)


def read_request(fd: int) -> bytes:
    """The request's head: bytes until the blank line, one read
    at a time inside :data:`HEAD_TIMEOUT_S`. A head that never
    closes returns what arrived before the deadline; one that
    exceeds :data:`HEADER_LIMIT` returns what arrived — the
    caller's malformed-request branch names the rule for whatever
    is left."""
    head = b""
    deadline = time.monotonic() + HEAD_TIMEOUT_S
    while b"\r\n\r\n" not in head and len(head) < HEADER_LIMIT:
        if not wait_readable(fd, deadline):
            return b""
        chunk = read_chunk(fd)
        if not chunk:
            return head
        head += chunk
    return head


def wait_readable(fd: int, deadline: float) -> bool:
    """Whether more head bytes arrive before the deadline: the
    budget left, or the deadline already passed."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        return bool(select.select([fd], [], [], remaining)[0])
    except OSError:
        return False


def read_chunk(fd: int) -> bytes:
    """One read's worth: the bytes that arrived, or empty when the
    descriptor cannot be read."""
    try:
        return os.read(fd, 4096)
    except OSError:
        return b""


def request_line(head: bytes) -> tuple[str, str]:
    """(method, target) of the request line; anything else raises
    :class:`ValueError` with the rule the ``400`` body carries."""
    lines = head.split(b"\r\n", 1)
    parts = lines[0].split()
    if len(parts) != 3 or parts[2] != b"HTTP/1.1":
        raise ValueError(
            "the pipe speaks one request line: GET /screenshot HTTP/1.1"
        )
    return parts[0].decode("ascii", "replace"), parts[1].decode(
        "ascii", "replace"
    )


def request_monitor(target: str) -> int:
    """The request's selector: the ``monitor`` parameter as a whole
    number 0 or greater (absent means 0). Anything else raises
    with the rule, and nothing the request carried reaches any
    later step: the answer is a number this function chose."""
    path, _, query = target.partition("?")
    if path != REQUEST_PATH:
        raise ValueError(
            f"the pipe serves {REQUEST_PATH} (got {path or 'an empty path'})"
        )
    if not query:
        return 0
    return monitor_value(query)


def monitor_value(query: str) -> int:
    """The query's one parameter, spelled ``monitor=``: the number
    it carries, or the refusal naming the one parameter this
    surface speaks."""
    key, separator, value = query.partition("=")
    if separator and key == "monitor":
        return monitor_number(value)
    raise ValueError(
        f"'{query}' is not a parameter this surface speaks "
        "(the one parameter is monitor=N)"
    )


def monitor_number(value: str) -> int:
    """The selector's number: a whole number 0 or greater — the
    bound keeps the answer a number, never a spelling."""
    if value.isdigit() and len(value) < 10:
        return int(value)
    raise ValueError(
        f"monitor must be a whole number 0 or greater (got {value!r})"
    )


def run_capture(argv: list[str], monitor: int) -> bytes:
    """Run the capture command once and return its stdout: the
    argv from the environment alone, the selector as the
    :data:`MONITOR_ENV` variable, a bounded wait, and the child's
    own stderr kept for the failure body."""
    done = capture_child(argv, monitor)
    if done.returncode != 0:
        raise ValueError(capture_failure(done))
    if len(done.stdout) > OUTPUT_LIMIT:
        raise ValueError(
            f"the capture command wrote more than the "
            f"{OUTPUT_LIMIT // (1024 * 1024)} MiB answer limit"
        )
    return done.stdout


def capture_child(
    argv: list[str], monitor: int
) -> subprocess.CompletedProcess:
    """The command's own run: its environment carries the selector
    as a variable, and a missing binary or a run past the timeout
    raises the named :class:`ValueError` the ``500`` body carries.
    The running child is pinned module-wide for the stop path,
    under a blocked SIGTERM so a stop signal landing between the
    spawn and the pin cannot miss the child (#1196 round 2)."""
    global _running
    child_env = dict(os.environ, **{MONITOR_ENV: str(monitor)})
    with blocked_sigterm():
        try:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
            )
        except FileNotFoundError:
            raise ValueError(
                f"the capture command {argv[0]!r} was not found on PATH"
            ) from None
        _running = child
    try:
        out, err = child.communicate(timeout=CAPTURE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        raise ValueError(
            f"the capture command did not finish within "
            f"{CAPTURE_TIMEOUT_S:.0f}s"
        ) from None
    finally:
        _running = None
    return subprocess.CompletedProcess(child.args, child.returncode, out, err)


@contextlib.contextmanager
def blocked_sigterm():
    """SIGTERM blocked for the span: a stop signal arriving inside
    it stays pending and lands once the pin is in place."""
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


def stop_running_capture(signum, frame) -> None:
    """The SIGTERM answer: the capture command this helper is
    running goes with the helper, and the helper goes too — an
    exit, not a wait."""
    if _running is not None:
        with contextlib.suppress(OSError):
            _running.terminate()
    raise SystemExit(128 + signum)


def capture_failure(done: subprocess.CompletedProcess) -> str:
    """The ``500`` body's cause for a failed command: its exit code
    and its stderr's tail — the end, where the error lives."""
    tail = (done.stderr or b"").decode("utf-8", "replace").strip()
    tail = tail[-STDERR_TAIL:]
    detail = f": {tail}" if tail else ""
    return f"the capture command exited {done.returncode}{detail}"


def write_all(fd: int, payload: bytes) -> None:
    """The whole response, one write at a time — a partial socket
    write is waited out, and a closed connection ends the helper
    (the response was for a connection already gone)."""
    view = memoryview(payload)
    while view:
        try:
            written = os.write(fd, view)
        except OSError:
            return
        view = view[written:]


def respond(fd: int, status: int, reason: str, body: bytes) -> None:
    """Answer the one exchange: the status line, the length, and
    the body — image bytes for a capture, the named rule or cause
    as text for a refusal."""
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {'application/octet-stream' if status == 200 else 'text/plain; charset=utf-8'}\r\n"  # noqa: E501
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    write_all(fd, head + body)


def audit(workspace: str, monitor: int, status: str, size: int) -> None:
    """Append the exchange's one JSONL line under the client state
    root. Best-effort by design: a state root that cannot be
    written says so on stderr and costs the line, never the
    answer — the capture itself already succeeded or failed on
    its own terms."""
    line = json.dumps(
        {
            "ts": datetime.now(UTC).isoformat(),
            "workspace": workspace,
            "monitor": monitor,
            "status": status,
            "bytes": size,
        }
    )
    try:
        root = state_dir()
        root.mkdir(parents=True, exist_ok=True)
        with open(root / HISTORY_NAME, "a", encoding="utf-8") as log:
            log.write(line + "\n")
    except OSError as exc:
        print(
            f"msks: screenshot audit line not written ({exc})", file=sys.stderr
        )


def serve(fd: int = 0) -> int:
    """Answer the one connection the holder handed over. Returns
    the helper's exit code: 0 when the exchange was answered
    (a refusal is an answer), 1 when the head never arrived."""
    head = read_request(fd)
    if not head:
        return 1
    workspace = os.environ.get(WORKSPACE_ENV, "")
    argv = request_argv()
    if argv is None:
        respond(
            fd,
            500,
            "Unconfigured",
            f"no screenshot command is configured on this client; set "
            f"screenshot_command in the client config, or export "
            f"{SCREENSHOT_COMMAND_ENV} for one shell".encode(),
        )
        return 0
    try:
        monitor = request_monitor(request_line(head)[1])
    except ValueError as exc:
        respond(fd, 400, "Bad Request", f"msks: {exc}\n".encode())
        return 0
    try:
        image = run_capture(argv, monitor)
    except ValueError as exc:
        respond(fd, 500, "Capture Failed", f"msks: {exc}\n".encode())
        audit(workspace, monitor, "error", 0)
        return 0
    respond(fd, 200, "OK", image)
    audit(workspace, monitor, "ok", len(image))
    return 0


def main() -> int:
    """The module entry: one connection, one answer, one exit —
    and a SIGTERM that takes the running capture command with the
    helper instead of leaving it on the desktop."""
    signal.signal(signal.SIGTERM, stop_running_capture)
    return serve()


if __name__ == "__main__":  # pragma: no cover — the -m entry
    raise SystemExit(main())
