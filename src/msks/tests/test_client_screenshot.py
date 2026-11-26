"""The screenshot pipe (#1194): the helper's HTTP exchange, the
holder's pipe halves, and the start-path gate — against fakes and
real sockets, no display server anywhere near."""

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from msks.client import bridge as bridge_flow
from msks.client import cli, screenshot, screenshotserv
from msks.client import data as clientdata
from msks.client import env as env_mod
from msks.client import rest as rest_mod
from msks.client.workspaces import lifecycle
from test_client_bridge import FakeSocket, closed_with

#: A command that answers instantly with fixed bytes — the stand-in
#: for a capture program, the same shape the smoke test's stub
#: uses end to end.
ECHO_COMMAND = (
    f"{sys.executable} -c \"import sys; sys.stdout.buffer.write(b'PNG-MARK')\""
)


@pytest.fixture(autouse=True)
def pipe_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A clean pipe environment: no command configured, the state
    root under the test's tree, a short runtime root (the socket
    path must stay inside its budget whatever pytest's tmp depth),
    and no stray port setting."""
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "")
    monkeypatch.setenv("MSKSC_SCREENSHOT_PORT", "")
    monkeypatch.setenv("MSKSC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/msks-shot-test")


# --- the helper's configuration read ---


def test_request_argv_reads_the_command_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MSKSC_SCREENSHOT_COMMAND", "import -window root -crop 1x1+0+0"
    )
    assert screenshotserv.request_argv() == [
        "import",
        "-window",
        "root",
        "-crop",
        "1x1+0+0",
    ]


def test_request_argv_unset_and_blank_are_the_unconfigured_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "")
    assert screenshotserv.request_argv() is None
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "   ")
    assert screenshotserv.request_argv() is None


# --- the request line's grammar ---


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"GET /screenshot HTTP/1.1\r\n\r\n", ("GET", "/screenshot")),
        (
            b"GET /screenshot?monitor=2 HTTP/1.1\r\nHost: x\r\n\r\n",
            ("GET", "/screenshot?monitor=2"),
        ),
    ],
)
def test_request_line_reads_well_formed_heads(
    head: bytes, expected: tuple[str, str]
) -> None:
    assert screenshotserv.request_line(head) == expected


@pytest.mark.parametrize(
    "head",
    [
        b"GET /screenshot\r\n\r\n",
        b"GET /screenshot HTTP/1.0\r\n\r\n",
        b"\r\n",
    ],
)
def test_request_line_refuses_everything_else(head: bytes) -> None:
    with pytest.raises(ValueError, match="one request line"):
        screenshotserv.request_line(head)


@pytest.mark.parametrize(
    ("target", "monitor"),
    [
        ("/screenshot", 0),
        ("/screenshot?", 0),
        ("/screenshot?monitor=0", 0),
        ("/screenshot?monitor=7", 7),
    ],
)
def test_request_monitor_accepts_the_valid_spellings(
    target: str, monitor: int
) -> None:
    assert screenshotserv.request_monitor(target) == monitor


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("/other", "serves /screenshot"),
        ("", "serves /screenshot"),
        ("/screenshot?screen=1", "not a parameter"),
        ("/screenshot?monitor", "not a parameter"),
        ("/screenshot?monitor=x", "whole number"),
        ("/screenshot?monitor=", "whole number"),
        ("/screenshot?monitor=-1", "whole number"),
        ("/screenshot?monitor=1234567890", "whole number"),
    ],
)
def test_request_monitor_refuses_the_rest(target: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        screenshotserv.request_monitor(target)


# --- the capture command's run ---


def test_run_capture_returns_the_command_stdout() -> None:
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'PNG-MARK')",
    ]
    assert screenshotserv.run_capture(argv, 0) == b"PNG-MARK"


def test_run_capture_passes_the_monitor_as_a_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            self.args = argv
            self.returncode = 0
            seen["argv"] = argv
            seen["env"] = kwargs["env"]

        def communicate(self, timeout=None):
            return b"ok", b""

    monkeypatch.setattr(screenshotserv.subprocess, "Popen", FakePopen)
    screenshotserv.run_capture(["import"], 4)
    assert seen["argv"] == ["import"]
    assert seen["env"]["MSKS_SCREENSHOT_MONITOR"] == "4"


def test_run_capture_names_a_nonzero_exit_with_its_stderr() -> None:
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('no display'); sys.exit(3)",
    ]
    with pytest.raises(ValueError, match="exited 3: no display"):
        screenshotserv.run_capture(argv, 0)


def test_run_capture_names_a_silent_nonzero_exit() -> None:
    argv = [sys.executable, "-c", "import sys; sys.exit(2)"]
    with pytest.raises(ValueError, match="exited 2"):
        screenshotserv.run_capture(argv, 0)


def test_run_capture_names_a_missing_binary() -> None:
    with pytest.raises(ValueError, match="not found on PATH"):
        screenshotserv.run_capture(["definitely-not-a-binary-xyz"], 0)


def test_run_capture_names_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screenshotserv, "CAPTURE_TIMEOUT_S", 0.2)
    argv = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(ValueError, match="did not finish"):
        screenshotserv.run_capture(argv, 0)


def test_run_capture_refuses_an_oversized_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screenshotserv, "OUTPUT_LIMIT", 4)
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'12345678')",
    ]
    with pytest.raises(ValueError, match="answer limit"):
        screenshotserv.run_capture(argv, 0)


# --- the response writes ---


def test_write_all_waits_out_partial_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left, right = socket.socketpair()
    real_write = os.write
    payload = b"HTTP/1.1 200 OK\r\n\r\n" + b"PNG-MARK"

    def one_byte_at_a_time(fd: int, data) -> int:
        if len(data) > 1:
            data = data[:1]
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", one_byte_at_a_time)
    try:
        screenshotserv.write_all(left.fileno(), payload)
    finally:
        monkeypatch.undo()
    left.close()
    right.settimeout(5)
    assert right.recv(65536) == payload
    right.close()


def test_write_all_ends_quietly_on_a_closed_connection() -> None:
    left, right = socket.socketpair()
    fd = left.fileno()
    left.close()
    right.close()
    assert screenshotserv.write_all(fd, b"gone") is None


def test_respond_carries_the_length_and_the_body() -> None:
    left, right = socket.socketpair()
    try:
        screenshotserv.respond(left.fileno(), 200, "OK", b"PNG-MARK")
        right.settimeout(5)
        head, _, body = right.recv(65536).partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"Content-Length: 8" in head
        assert body == b"PNG-MARK"
    finally:
        left.close()
        right.close()


# --- the audit line ---


def test_audit_appends_one_line_per_run() -> None:
    root = Path(os.environ["MSKSC_STATE_DIR"])
    screenshotserv.audit("dev", 2, "ok", 821349)
    line = json.loads((root / screenshotserv.HISTORY_NAME).read_text().strip())
    assert line["workspace"] == "dev"
    assert line["monitor"] == 2
    assert line["status"] == "ok"
    assert line["bytes"] == 821349
    assert line["ts"]


def test_audit_survives_an_unwritable_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the root should be")
    monkeypatch.setenv("MSKSC_STATE_DIR", str(blocker / "state"))
    screenshotserv.audit("dev", 0, "ok", 1)
    assert "not written" in capsys.readouterr().err


# --- one whole exchange, against a real socketpair ---


def exchange(head: bytes) -> tuple[int, bytes]:
    """Serve one exchange over a fresh socketpair and return the
    helper's exit code with everything the other end read. The
    command and the workspace name come from the environment the
    test set."""
    client, server = socket.socketpair()
    try:
        client.sendall(head)
        client.shutdown(socket.SHUT_WR)
        code = screenshotserv.serve(server.fileno())
        server.close()
        client.settimeout(5)
        read = b""
        while True:
            try:
                chunk = client.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            read += chunk
        return code, read
    finally:
        client.close()
        server.close()


def history_line() -> dict:
    """The audit trail's one line, read back."""
    return json.loads(
        (Path(os.environ["MSKSC_STATE_DIR"]) / screenshotserv.HISTORY_NAME)
        .read_text()
        .strip()
    )


def test_serve_answers_the_capture_bytes_and_audits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", ECHO_COMMAND)
    monkeypatch.setenv("MSKS_SCREENSHOT_WORKSPACE", "dev")
    code, response = exchange(
        b"GET /screenshot?monitor=1 HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert code == 0
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert response.endswith(b"PNG-MARK")
    line = history_line()
    assert line["monitor"] == 1
    assert line["status"] == "ok"
    assert line["bytes"] == len(b"PNG-MARK")
    assert capsys.readouterr().err == ""


def test_serve_answers_a_bad_selector_with_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", ECHO_COMMAND)
    code, response = exchange(
        b"GET /screenshot?monitor=x HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert code == 0
    assert response.startswith(b"HTTP/1.1 400 Bad Request\r\n")
    assert b"whole number" in response


def test_serve_answers_a_failed_command_with_its_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = (
        f"{sys.executable} -c "
        "\"import sys; sys.stderr.write('boom'); sys.exit(3)\""
    )
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", failing)
    monkeypatch.setenv("MSKS_SCREENSHOT_WORKSPACE", "dev")
    code, response = exchange(b"GET /screenshot HTTP/1.1\r\nHost: x\r\n\r\n")
    assert code == 0
    assert response.startswith(b"HTTP/1.1 500 Capture Failed\r\n")
    assert b"exited 3: boom" in response
    assert history_line()["status"] == "error"


def test_serve_answers_the_unconfigured_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, response = exchange(b"GET /screenshot HTTP/1.1\r\nHost: x\r\n\r\n")
    assert code == 0
    assert response.startswith(b"HTTP/1.1 500 Unconfigured\r\n")
    assert b"no screenshot command is configured" in response


def test_serve_refuses_a_headless_connection() -> None:
    client, server = socket.socketpair()
    try:
        client.close()
        assert screenshotserv.serve(server.fileno()) == 1
    finally:
        server.close()


def test_a_head_that_never_arrives_loses_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection that sends nothing answers nothing inside the
    head budget (#1196 round 2): it cannot pin a capture slot for
    the holder's lifetime."""
    monkeypatch.setattr(screenshotserv, "HEAD_TIMEOUT_S", 0.05)
    client, server = socket.socketpair()
    try:
        assert screenshotserv.serve(server.fileno()) == 1
    finally:
        client.close()
        server.close()


def test_serve_refuses_a_head_that_never_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screenshotserv, "HEADER_LIMIT", 64)
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", ECHO_COMMAND)
    code, response = exchange(b"GET /screenshot " + b"x" * 512 + b" HTTP/1.1")
    assert code == 0
    assert response.startswith(b"HTTP/1.1 400 Bad Request\r\n")


def test_main_serves_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module entry serves one connection — and its SIGTERM
    setting stays inside the module: the test process's own
    disposition is untouched."""
    monkeypatch.setattr(screenshotserv, "serve", lambda fd=0: 7)
    installed = []
    monkeypatch.setattr(
        screenshotserv.signal, "signal", lambda *args: installed.append(args)
    )
    assert screenshotserv.main() == 7
    assert installed[0][0] is signal.SIGTERM


# --- the holder's halves ---


def test_pipe_due_reads_the_command_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "import")
    assert screenshot.pipe_due() is True
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "")
    assert screenshot.pipe_due() is False
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "  ")
    assert screenshot.pipe_due() is False
    assert screenshot.pipe_due({"MSKSC_SCREENSHOT_COMMAND": "x"}) is True
    assert screenshot.pipe_due({}) is False


@pytest.mark.parametrize(
    ("raw", "port"),
    [("", 8662), ("9000", 9000), ("1", 1), ("65535", 65535)],
)
def test_pipe_port_reads_valid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, port: int
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_PORT", raw)
    assert screenshot.pipe_port() == port


@pytest.mark.parametrize("raw", ["x", "0", "65536", "-1", "8 8"])
def test_pipe_port_refuses_invalid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_PORT", raw)
    with pytest.raises(SystemExit, match="wants a port"):
        screenshot.pipe_port()


def test_socket_path_carries_the_runtime_dir_and_the_pid() -> None:
    runtime = os.environ["XDG_RUNTIME_DIR"]
    path = screenshot.socket_path("ws-one", pid=99)
    assert str(path) == os.path.join(
        runtime, screenshot.socket_name("ws-one", pid=99)
    )
    assert len(str(path)) < 108


def test_socket_path_falls_back_when_the_runtime_dir_is_too_deep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime directory whose depth would push the path past the
    kernel's budget moves the socket to the system temp: the pipe
    serves whatever the environment's own shape."""
    deep = "x" * 100
    monkeypatch.setenv("XDG_RUNTIME_DIR", f"/tmp/{deep}")
    path = screenshot.socket_path("ws-one", pid=99)
    assert str(path) == os.path.join(
        tempfile.gettempdir(), screenshot.socket_name("ws-one", pid=99)
    )


def test_runtime_dir_falls_back_without_xdg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host with no runtime directory (a container) still serves
    the pipe: the system temp answers, short and writable."""
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert str(screenshot.runtime_dir()) == tempfile.gettempdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative/path")
    assert str(screenshot.runtime_dir()) == tempfile.gettempdir()


@pytest.mark.parametrize(
    "raw",
    ["ws-one", "a b/c!", "", "x" * 200],
)
def test_socket_name_hashes_the_workspace_to_a_fixed_budget(
    raw: str,
) -> None:
    name = screenshot.socket_name(raw, pid=99)
    assert name.startswith("shot-")
    assert name.endswith("-99.sock")
    assert len(name) <= len("shot-12345678-9999999.sock")
    # Different ids name different sockets; the same id names one.
    assert screenshot.socket_name("other", pid=99) != name
    assert screenshot.socket_name(raw, pid=99) == name


def test_reverse_argv_shapes_the_forward_child() -> None:
    argv = screenshot.reverse_argv(
        "ws1", "/agent.sock", "/identity.pub", "dev", "/kh", 8662, "/pipe"
    )
    assert argv[0] == "ssh"
    assert "-N" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "-R" in argv
    assert argv[argv.index("-R") + 1] == "127.0.0.1:8662:/pipe"
    assert argv[argv.index("-l") + 1] == "dev"
    assert "UserKnownHostsFile=/kh" in argv
    assert "IdentityAgent=/agent.sock" in argv
    assert argv[-1] == "ws1"


class FakeProc:
    """The subprocess stand-in for the pipe's two children: a
    readable stderr script, a wait the test controls, and a
    terminate the stop path reaches."""

    def __init__(self, stderr: bytes | None = b"", on_wait=None) -> None:
        self.stderr = io.BytesIO(stderr) if stderr is not None else None
        self.returncode = None
        self.terminated = False
        self.on_wait = on_wait

    def wait(self) -> int:
        if self.on_wait is not None:
            self.on_wait()
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True


def test_watch_stderr_reads_the_refusal_line() -> None:
    child = FakeProc(
        stderr=b"ssh: error: remote port forwarding failed"
        b" for listen port 8662\r\n"
    )
    assert screenshot.ScreenshotPipe.watch_stderr(None, child) is True


def test_watch_stderr_without_a_stream_is_not_a_refusal() -> None:
    child = FakeProc(stderr=None)
    assert screenshot.ScreenshotPipe.watch_stderr(None, child) is False


def test_the_supervisor_restarts_and_reports_a_refused_bind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(screenshot, "RETRY_S", 0.01)
    monkeypatch.setattr(screenshot, "BIND_RETRY_S", 0.01)
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    waits = []

    def on_wait() -> None:
        waits.append(1)
        if len(waits) >= 2:
            pipe.stopping.set()

    refused = b"ssh: error: remote port forwarding failed for 8662\n"
    pipe.ssh_runner = lambda argv, **kw: FakeProc(
        stderr=refused, on_wait=on_wait
    )
    pipe.supervise()
    assert len(waits) == 2
    assert "was refused" in capsys.readouterr().err


def test_the_supervisor_retries_a_quiet_death(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(screenshot, "RETRY_S", 0.01)
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    waits = []

    def on_wait() -> None:
        waits.append(1)
        if len(waits) >= 2:
            pipe.stopping.set()

    pipe.ssh_runner = lambda argv, **kw: FakeProc(on_wait=on_wait)
    pipe.supervise()
    assert len(waits) == 2
    assert capsys.readouterr().err == ""


def test_the_supervisor_names_an_ssh_that_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(screenshot, "BIND_RETRY_S", 0.01)
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if len(calls) >= 2:
            pipe.stopping.set()
        raise OSError("no ssh on PATH")

    pipe.ssh_runner = runner
    pipe.supervise()
    assert len(calls) == 2
    assert "did not start" in capsys.readouterr().err


def test_reap_waits_and_discards_the_child() -> None:
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    child = FakeProc()
    pipe.children.add(child)
    pipe.reap(child)
    assert child.returncode == 0
    assert child not in pipe.children


def dial(pipe: screenshot.ScreenshotPipe) -> socket.socket:
    """One client connection to the pipe's socket, with a timeout
    already armed."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(pipe.target))
    return client


def test_the_accept_loop_hands_the_connection_to_a_helper() -> None:
    pipe = screenshot.ScreenshotPipe(
        "ws 1", "/agent", "/ident", "dev", "/kh", name="ws one"
    )
    seen = []

    def fake_helper(argv, stdin, stdout, stderr, env):
        seen.append({"argv": argv, "env": env})
        data = stdin.recv(4096)
        stdout.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + data)
        return FakeProc()

    pipe.helper_runner = fake_helper
    pipe.ssh_runner = lambda argv, **kw: FakeProc(stderr=b"")
    pipe.start()
    try:
        client = dial(pipe)
        client.sendall(b"GET /screenshot HTTP/1.1\r\n\r\nPNG?")
        response = b""
        while b"PNG?" not in response:
            response += client.recv(4096)
        client.close()
        assert seen[0]["argv"] == [
            sys.executable,
            "-m",
            screenshot.HELPER_MODULE,
        ]
        assert seen[0]["env"]["MSKS_SCREENSHOT_WORKSPACE"] == "ws one"
    finally:
        pipe.stop()
    assert not pipe.target.exists()


def test_the_accept_loop_names_a_helper_that_cannot_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")

    def runner(argv, **kw):
        raise OSError("no interpreter")

    pipe.helper_runner = runner
    pipe.ssh_runner = lambda argv, **kw: FakeProc(stderr=b"")
    pipe.start()
    try:
        client = dial(pipe)
        with contextlib.suppress(OSError):
            # The failed spawn closes the connection under the
            # dial — the send is the trigger, the stderr line is
            # the assertion.
            client.sendall(b"GET /screenshot HTTP/1.1\r\n\r\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "did not start" in capsys.readouterr().err:
                break
            time.sleep(0.01)
        else:
            pytest.fail("the accept loop never named the failed helper")
        client.close()
    finally:
        pipe.stop()


def test_stop_terminates_the_forward_and_the_helpers() -> None:
    class Held:
        """A child that stays in the pipe's children until the stop
        path terminates it: wait() parks, terminate() releases. The
        spawn dup is the copy a real child would hold, so the
        accept loop's own close of the connection strands no
        writer."""

        def __init__(self) -> None:
            self.terminated = False
            self.held: int | None = None

        def wait(self) -> int:
            while not self.terminated:
                time.sleep(0.01)
            return 0

        def terminate(self) -> None:
            self.terminated = True
            if self.held is not None:
                with contextlib.suppress(OSError):
                    os.close(self.held)

    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    forward = FakeProc(stderr=b"")
    helper = Held()
    pipe.ssh_runner = lambda argv, **kw: forward

    def held_runner(argv, stdin, stdout, stderr, env):
        helper.held = os.dup(stdin.fileno())
        return helper

    pipe.helper_runner = held_runner
    pipe.start()
    client = dial(pipe)
    client.sendall(b"GET /screenshot HTTP/1.1\r\n\r\n")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with pipe.lock:
            if pipe.children:
                break
        time.sleep(0.01)
    else:
        pytest.fail("the accept loop never held the helper child")
    pipe.stop()
    client.close()
    assert forward.terminated is True
    assert helper.terminated is True


def test_pipe_from_env_stays_down_without_a_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "")
    assert screenshot.pipe_from_env("ws1", "/a", "/i", "dev", "/kh") is None


def test_pipe_from_env_serves_with_a_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "import")
    pipe = screenshot.pipe_from_env(
        "ws1", "/a", "/i", "dev", "/kh", name="ws one"
    )
    assert pipe is not None
    assert pipe.target.exists()
    assert "127.0.0.1:8662" in capsys.readouterr().err
    pipe.stop()


def test_pipe_from_env_names_a_start_that_cannot_bind(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "import")

    def refused_start(self) -> None:
        raise OSError("the runtime directory is not writable")

    monkeypatch.setattr(screenshot.ScreenshotPipe, "start", refused_start)
    assert screenshot.pipe_from_env("ws1", "/a", "/i", "dev", "/kh") is None
    assert "did not start" in capsys.readouterr().err


# --- the holder wiring and the start-path gate ---


def test_the_holder_serves_the_pipe_and_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hold() starts the pipe beside the bridge and stops it when
    the websocket ends: the pipe's lifetime is the holder's."""
    stopped = []

    class FakePipe:
        def stop(self) -> None:
            stopped.append(True)

    def fake_pipe_from_env(*args, **kwargs):
        return FakePipe()

    monkeypatch.setattr(bridge_flow, "pipe_from_env", fake_pipe_from_env)

    async def fake_fetch(*args, **kwargs):
        return {"public_key": "ssh-ed25519 AAAA dev", "user": "dev"}

    monkeypatch.setattr(bridge_flow, "fetch_ssh_key", fake_fetch)

    @contextmanager
    def fake_identity(key, workspace_id):
        yield SimpleNamespace(server_address="/a", identity_path="/i")

    monkeypatch.setattr(bridge_flow, "session_identity", fake_identity)
    monkeypatch.setattr(
        bridge_flow, "known_hosts_path", lambda ws, **kw: "/kh"
    )

    clean = FakeSocket([])

    async def recv_clean():
        raise closed_with(1000)

    clean.recv = recv_clean

    class CleanConnection:
        async def __aenter__(self):
            return clean

        async def __aexit__(self, *exc):
            return None

    def fake_dial(*args, **kwargs):
        async def connect():
            return CleanConnection()

        return connect()

    monkeypatch.setattr(bridge_flow.wsdial, "dial", fake_dial)
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    assert bridge_flow.run_hold("ws-a") == 0
    assert stopped == [True]


async def test_a_session_boot_with_a_pipe_command_leaves_a_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boot helper every session child uses leaves a holder for
    the pipe alone (#1194): no forward rows, a configured command,
    and the spawn follows."""
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "import")
    spawned = []
    monkeypatch.setattr(rest_mod, "spawn_holder", spawned.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "ws1", "status": "running"})
        if request.url.path.endswith("/proxies"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": "ws1", "status": "stopped"})

    booted = await rest_mod.ensure_running(
        "ws1", "https://d", "t", transport=httpx.MockTransport(handler)
    )
    assert booted is True
    assert spawned == ["ws1"]


def test_a_cli_start_with_a_pipe_command_holds_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start's bridge step spawns the holder for the pipe alone
    (#1194): the rows read empty, the command is configured, and
    the spawn follows the same best-effort path."""
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "import")
    spawned = []
    monkeypatch.setattr(lifecycle, "spawn_holder", spawned.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/proxies"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": "ws1", "status": "running"})

    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    rc = cli.cmd_start("ws1", transport=httpx.MockTransport(handler))
    assert rc == 0
    assert spawned == ["ws1"]


async def test_the_data_start_spawns_for_the_pipe_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI/TUI data seam's start leaves the holder for the pipe
    alone (#1194), the same spawn the CLI start makes."""
    monkeypatch.setenv("MSKSC_URL", "https://127.0.0.1:9660")
    monkeypatch.setenv("MSKSC_TOKEN", "t" * 32)
    env_mod._SHARED_SSL[0] = None
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "import")
    spawned = []
    monkeypatch.setattr(clientdata, "spawn_holder", spawned.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/proxies"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": True})

    seam = clientdata.RestData(httpx.MockTransport(handler))
    await seam.start("ws-1")
    assert spawned == ["ws-1"]


# --- the CLI command ---


def test_the_hold_command_refuses_without_a_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    with pytest.raises(SystemExit, match="no screenshot command"):
        cli.main(["screenshot", "hold", "alpha"])


def test_the_hold_command_runs_the_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "import")
    seen = {}

    def fake_run(workspace_id, transport=None):
        seen["workspace"] = workspace_id
        return 0

    monkeypatch.setattr(bridge_flow, "run_hold", fake_run)
    rc = cli.main(
        ["screenshot", "hold", "alpha"],
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=[])
        ),
    )
    assert rc == 0
    assert seen["workspace"] == "alpha"


def test_watch_stderr_relays_the_diagnostics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A line that is not the bind refusal is the forward child's
    own diagnostic — an authentication refusal, a dead
    ProxyCommand — and reaches the holder's stderr instead of
    vanishing into the pipe."""
    child = FakeProc(stderr=b"ssh: Permission denied (publickey).\r\n\n")
    assert screenshot.ScreenshotPipe.watch_stderr(None, child) is False
    assert (
        "screenshot pipe ssh: ssh: Permission denied (publickey)."
        in capsys.readouterr().err
    )


def test_read_request_answers_empty_when_the_fd_cannot_read() -> None:
    """An unreadable descriptor (a connection already gone) answers
    empty: the malformed-request branch names the rule, never a
    traceback."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    assert screenshotserv.read_request(read_fd) == b""


def test_a_never_started_pipe_stops_as_a_no_op() -> None:
    """stop() on a pipe that never started (no listener, no forward
    child, no helpers) is the clean no-op: the teardown arms all
    take their nothing-there paths."""
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    pipe.stop()
    assert pipe.listener is None
    assert pipe.ssh is None
    assert not pipe.target.exists()


def test_the_accept_loop_leaves_when_the_stop_flag_stands() -> None:
    """A loop that checks its flag between accepts leaves without
    touching the listener: the stop path's flag alone ends it."""
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    pipe.stopping.set()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    pipe.accept_loop(listener)
    listener.close()


def test_retry_after_reports_the_refused_bind_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first refused bind says so; the ones after it stay quiet
    and keep the slow pace."""
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    assert pipe.retry_after(False) == screenshot.RETRY_S
    assert pipe.retry_after(True) == screenshot.BIND_RETRY_S
    assert "was refused" in capsys.readouterr().err
    assert pipe.retry_after(True) == screenshot.BIND_RETRY_S
    assert capsys.readouterr().err == ""


# --- the review round's fixes (#1196 round 1) ---


def test_reverse_argv_carries_no_quiet_flag() -> None:
    """The forward child runs without `-q`: a healthy child is
    silent on its own, and the quiet flag suppresses exactly the
    refusal and diagnostic lines the supervisor reads."""
    argv = screenshot.reverse_argv(
        "ws1", "/agent.sock", "/identity.pub", "dev", "/kh", 8662, "/pipe"
    )
    assert "-q" not in argv


def test_beyond_the_concurrency_cap_the_answer_is_the_busy_refusal() -> None:
    """At most MAX_CONCURRENT captures run at once: beyond that the
    holder answers the busy refusal inline — no helper child, no
    capture command, the guest's flood gets cheap 503s."""
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    for _ in range(screenshot.MAX_CONCURRENT):
        with pipe.lock:
            pipe.children.add(FakeProc())

    def must_not_spawn(argv, **kw):
        raise AssertionError("the busy path spawned a helper")

    pipe.helper_runner = must_not_spawn
    left, right = socket.socketpair()
    try:
        pipe.answer(left)
        right.settimeout(5)
        response = right.recv(65536)
    finally:
        left.close()
        right.close()
    assert response.startswith(b"HTTP/1.1 503")
    assert b"too many captures" in response


def test_the_accept_loop_retries_a_listener_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A listener error the stop path did not cause (descriptor
    exhaustion among them) is named and retried: the pipe keeps
    serving instead of ending."""
    monkeypatch.setattr(screenshot, "RETRY_S", 0.01)
    pipe = screenshot.ScreenshotPipe("ws1", "/agent", "/ident", "dev", "/kh")
    calls = []

    class Dead:
        def accept(self):
            calls.append(1)
            if len(calls) >= 2:
                pipe.stopping.set()
            raise OSError("cannot accept")

    pipe.accept_loop(Dead())
    assert len(calls) == 2
    assert "listener failed" in capsys.readouterr().err


def test_terminating_exit_raises_the_stopped_status() -> None:
    """The holder's SIGTERM handler raises SystemExit with the
    terminated status: asyncio's cancellation unwinds the session
    (the smoke test proves the pipe's stop runs from that
    teardown)."""
    with pytest.raises(SystemExit) as exc:
        bridge_flow.terminating_exit(signal.SIGTERM, None)
    assert exc.value.code == 143


def test_holder_sigterm_installs_and_restores() -> None:
    """One holder's stop signals cover the session (SIGTERM and the
    terminal's SIGHUP) and restore the shell's own when it
    leaves."""
    before = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGTERM, signal.SIGHUP)
    }
    with bridge_flow.holder_sigterm():
        for signum in before:
            assert signal.getsignal(signum) is bridge_flow.terminating_exit
    for signum, handler in before.items():
        assert signal.getsignal(signum) is handler


def test_capture_child_pins_and_clears_the_running_child() -> None:
    """The running child is pinned module-wide for the stop path
    and cleared when the capture answers."""
    argv = [sys.executable, "-c", "print('ok')"]
    done = screenshotserv.capture_child(argv, 0)
    assert done.returncode == 0
    assert done.stdout == b"ok\n"
    assert screenshotserv._running is None


def test_a_terminated_helper_kills_its_capture() -> None:
    """A SIGTERM mid-capture terminates the running command and
    ends the helper with it: an exit, not a wait."""
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    screenshotserv._running = child
    try:
        with pytest.raises(SystemExit) as exc:
            screenshotserv.stop_running_capture(signal.SIGTERM, None)
        assert exc.value.code == 143
        assert child.wait(timeout=10) is not None
    finally:
        if child.returncode is None:
            child.kill()
            child.wait()
        screenshotserv._running = None


def test_stop_running_capture_without_a_child_is_a_no_op() -> None:
    """A SIGTERM between captures (nothing running) reaches no
    child: the handler's empty case still ends the helper."""
    screenshotserv._running = None
    with pytest.raises(SystemExit) as exc:
        screenshotserv.stop_running_capture(signal.SIGTERM, None)
    assert exc.value.code == 143


def test_read_chunk_answers_empty_when_the_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descriptor that turns unreadable mid-head answers empty:
    the malformed-request branch names the rule, never a
    traceback."""
    left, right = socket.socketpair()
    try:
        left.sendall(b"GET /")

        def failing(fd: int, count: int) -> bytes:
            raise OSError("gone")

        monkeypatch.setattr(screenshotserv.os, "read", failing)
        assert screenshotserv.read_chunk(left.fileno()) == b""
    finally:
        left.close()
        right.close()


def test_wait_readable_answers_no_when_the_budget_is_spent() -> None:
    """A deadline already past answers no: the head budget ends the
    wait before any select."""
    left, right = socket.socketpair()
    try:
        spent = time.monotonic() - 1.0
        assert screenshotserv.wait_readable(left.fileno(), spent) is False
    finally:
        left.close()
        right.close()
