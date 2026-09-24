"""The egress fuzzer's host-process hygiene (#307).

The fuzzer lives at scripts/fuzz-egress.py -- outside the package
-- so these tests load it by path.  The /proc-facing helpers are
exercised through monkeypatched pid and cmdline providers; no
real process is signaled and no real daemon is booted.
"""

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "fuzz-egress.py"


def load_fuzz():
    """The fuzzer module, imported from its script path."""
    spec = importlib.util.spec_from_file_location("fuzz_egress", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fuzz_egress"] = module
    spec.loader.exec_module(module)
    return module


FUZZ = load_fuzz()


def harness_args(**over):
    """The argparse namespace Harness needs, with overrides."""
    base = dict(
        url=None,
        token=None,
        cafile=None,
        keep_workspace=False,
        seed=1,
        count=1,
        continue_run=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeProc:
    """A Popen stand-in for the daemon's terminate paths."""

    def __init__(self, rc=-15, hang=False):
        self.rc = rc
        self.hang = hang
        self.terminated = 0
        self.killed = 0

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout):
        if self.hang:
            raise subprocess.TimeoutExpired(cmd="msksd", timeout=timeout)
        return self.rc


async def nosleep(_seconds):
    """The asyncio.sleep stand-in (nothing in a test ever waits)."""
    return None


# -- cmdline matching -------------------------------------------------------


def test_socket_of_forms():
    separate = [
        b"/usr/bin/cloud-hypervisor",
        b"--api-socket",
        b"/tmp/x/api.sock",
    ]
    assert FUZZ.socket_of(separate) == b"/tmp/x/api.sock"
    joined = [b"cloud-hypervisor", b"--api-socket=/tmp/y/api.sock"]
    assert FUZZ.socket_of(joined) == b"/tmp/y/api.sock"
    assert FUZZ.socket_of([b"cloud-hypervisor"]) is None


def test_owned_vmm_scopes(tmp_path):
    boot_fields = [
        b"/usr/lib/cloud-hypervisor/cloud-hypervisor",
        b"--api-socket",
        str(tmp_path / "vms" / "ws1" / "api.sock").encode(),
    ]
    assert FUZZ.owned_vmm(boot_fields, tmp_path, set())
    attach = [
        b"cloud-hypervisor",
        b"--api-socket",
        b"/var/lib/msksd/vms/ws2/api.sock",
    ]
    assert FUZZ.owned_vmm(attach, None, {"ws2"})
    assert not FUZZ.owned_vmm(attach, None, {"ws9"})
    assert not FUZZ.owned_vmm(boot_fields, tmp_path / "other", {"ws2"})
    qemu = [
        b"/usr/bin/qemu-system-x86",
        b"--api-socket",
        str(tmp_path / "vms" / "ws1" / "api.sock").encode(),
    ]
    assert not FUZZ.owned_vmm(qemu, tmp_path, {"ws1"})


def test_scan_finds_only_owned(monkeypatch, tmp_path):
    sock = str(tmp_path / "vms" / "ws1" / "api.sock").encode()
    cmdlines = {
        11: [b"cloud-hypervisor", b"--api-socket", sock],
        12: [b"/usr/bin/sleep", b"100"],
        13: [b"cloud-hypervisor", b"--api-socket", b"/elsewhere/s"],
    }
    monkeypatch.setattr(FUZZ, "proc_pids", lambda: [11, 12, 13])
    monkeypatch.setattr(
        FUZZ, "read_cmdline", lambda pid: cmdlines.get(pid, [])
    )
    assert FUZZ.scan_vmm_pids(tmp_path, set()) == [11]


def test_kill_verified_guards_and_delivers(monkeypatch, tmp_path):
    sock = str(tmp_path / "vms" / "ws1" / "api.sock").encode()
    killed = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(
        FUZZ,
        "read_cmdline",
        lambda pid: [b"cloud-hypervisor", b"--api-socket", sock],
    )
    assert FUZZ.kill_verified(7, signal.SIGKILL, tmp_path, set())
    assert killed == [(7, signal.SIGKILL)]
    monkeypatch.setattr(
        FUZZ, "read_cmdline", lambda pid: [b"/usr/bin/sleep", b"1"]
    )
    killed.clear()
    assert not FUZZ.kill_verified(7, signal.SIGKILL, tmp_path, set())
    assert killed == []


def test_cmdline_is_msksd():
    assert FUZZ.cmdline_is_msksd([b"/usr/bin/msksd", b"--config=none"])
    assert FUZZ.cmdline_is_msksd(
        [b"/venv/bin/python", b"-m", b"msks.server.main", b"--config=none"]
    )
    assert not FUZZ.cmdline_is_msksd([b"/usr/bin/sleep", b"100"])
    assert not FUZZ.cmdline_is_msksd([])


def test_read_pidfile(tmp_path):
    path = tmp_path / FUZZ.DAEMON_PID
    assert FUZZ.read_pidfile(path) is None
    path.write_text("123\n")
    assert FUZZ.read_pidfile(path) == 123
    path.write_text("not-a-pid")
    assert FUZZ.read_pidfile(path) is None


# -- the watchdog -----------------------------------------------------------


def test_reap_active_transitions(tmp_path, monkeypatch):
    monkeypatch.setattr(FUZZ, "pid_alive", lambda pid: True)
    assert FUZZ.reap_active(1, tmp_path)
    (tmp_path / FUZZ.REAP_SENTINEL).touch()
    assert not FUZZ.reap_active(1, tmp_path)
    (tmp_path / FUZZ.REAP_SENTINEL).unlink()
    assert not FUZZ.reap_active(1, tmp_path / "gone")
    monkeypatch.setattr(FUZZ, "pid_alive", lambda pid: False)
    assert not FUZZ.reap_active(1, tmp_path)


def test_run_reaper_stands_down(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(FUZZ, "reap_active", lambda pid, d: False)
    monkeypatch.setattr(FUZZ, "pid_alive", lambda pid: True)
    monkeypatch.setattr(FUZZ, "reap_daemon_stack", calls.append)
    assert FUZZ.run_reaper(5, tmp_path) == 0
    assert calls == []


def test_run_reaper_reaps_on_death(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(FUZZ, "reap_active", lambda pid, d: False)
    monkeypatch.setattr(FUZZ, "pid_alive", lambda pid: False)
    monkeypatch.setattr(FUZZ, "reap_daemon_stack", calls.append)
    assert FUZZ.run_reaper(5, tmp_path) == 0
    assert calls == [tmp_path]


def test_reap_daemon_stack_paths(monkeypatch, tmp_path):
    order = []
    killed = []
    (tmp_path / FUZZ.DAEMON_PID).write_text("42\n")
    monkeypatch.setattr(
        FUZZ,
        "read_cmdline",
        lambda pid: [b"/usr/bin/msksd", b"--config=none"],
    )
    monkeypatch.setattr(
        FUZZ, "term_then_kill", lambda pid: order.append(("term", pid))
    )
    monkeypatch.setattr(FUZZ, "scan_vmm_pids", lambda root, ids: [7, 8])

    def record_kill(pid, sig, root, ids):
        killed.append(pid)
        return True

    monkeypatch.setattr(FUZZ, "kill_verified", record_kill)
    monkeypatch.setattr(
        FUZZ,
        "restore_forwarding_file",
        lambda path: order.append(("forward", path)),
    )
    FUZZ.reap_daemon_stack(tmp_path)
    assert order[0] == ("term", 42)
    assert killed == [7, 8]
    assert order[-1] == ("forward", tmp_path / FUZZ.IP_FORWARD_WAS)


def test_reap_daemon_stack_ignores_recycled_pid(monkeypatch, tmp_path):
    (tmp_path / FUZZ.DAEMON_PID).write_text("42\n")
    monkeypatch.setattr(
        FUZZ, "read_cmdline", lambda pid: [b"/usr/bin/sleep", b"9"]
    )
    monkeypatch.setattr(
        FUZZ, "term_then_kill", lambda pid: pytest.fail("signaled recycled")
    )
    monkeypatch.setattr(FUZZ, "scan_vmm_pids", lambda root, ids: [])
    monkeypatch.setattr(FUZZ, "restore_forwarding_file", lambda path: None)
    FUZZ.reap_daemon_stack(tmp_path)


def test_term_then_kill_graceful(monkeypatch):
    sent = []
    sleeps = []
    monkeypatch.setattr("os.kill", lambda pid, sig: sent.append(sig))
    polls = iter([True, False])
    monkeypatch.setattr(FUZZ, "cmdline_is_msksd", lambda fields: next(polls))
    monkeypatch.setattr(FUZZ.time, "sleep", sleeps.append)
    FUZZ.term_then_kill(99)
    assert sent == [signal.SIGTERM]
    assert sleeps == [FUZZ.REAP_POLL_S]


def test_term_then_kill_force(monkeypatch):
    sent = []
    monkeypatch.setattr("os.kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(FUZZ, "cmdline_is_msksd", lambda fields: True)
    clock = iter([0.0, 10000.0])
    monkeypatch.setattr(FUZZ.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(FUZZ.time, "sleep", lambda seconds: None)
    FUZZ.term_then_kill(99)
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_restore_forwarding_file_without_saved(tmp_path):
    FUZZ.restore_forwarding_file(tmp_path / "missing")


# -- the daemon stop paths --------------------------------------------------


async def test_daemon_stop_graceful_and_idempotent(tmp_path):
    daemon = FUZZ.Daemon(harness_args())
    proc = FakeProc(rc=-15)
    daemon.proc = proc
    daemon.state_dir = tmp_path
    (tmp_path / "daemon.out").write_text("ok\n")
    assert await daemon.stop() is None
    assert daemon.proc is None
    assert not tmp_path.exists()
    assert await daemon.stop() is None
    assert proc.terminated == 1


async def test_daemon_stop_returns_problem_on_bad_rc(tmp_path):
    daemon = FUZZ.Daemon(harness_args())
    daemon.proc = FakeProc(rc=3)
    daemon.state_dir = tmp_path
    problem = await daemon.stop()
    assert "not the graceful path" in problem
    assert not tmp_path.exists()


async def test_daemon_stop_kills_hung_daemon(tmp_path):
    daemon = FUZZ.Daemon(harness_args())
    proc = FakeProc(hang=True)
    daemon.proc = proc
    daemon.state_dir = tmp_path
    problem = await daemon.stop()
    assert "did not exit on SIGTERM" in problem
    assert proc.killed == 1


# -- the harness teardown ---------------------------------------------------


async def test_teardown_order_and_independence():
    harness = FUZZ.Harness(harness_args())
    order = []

    async def workspaces():
        order.append("workspaces")
        raise RuntimeError("one step's failure must strand nothing")

    async def stop_daemon():
        order.append("stop_daemon")

    async def stand_down():
        order.append("stand_down")

    async def stop_dns():
        order.append("stop_dns")

    class FakeClient:
        async def aclose(self):
            order.append("client")

    class FakeDecider:
        async def close(self):
            order.append("decider")

    harness.teardown_workspaces = workspaces
    harness.stop_daemon = stop_daemon
    harness.stand_down_reaper = stand_down
    harness.stop_dns = stop_dns
    harness.client = FakeClient()
    harness.decider = FakeDecider()
    await harness.teardown()
    assert order == [
        "decider",
        "workspaces",
        "client",
        "stop_daemon",
        "stand_down",
        "stop_dns",
    ]


async def test_teardown_workspaces_verifies_and_retries(monkeypatch):
    harness = FUZZ.Harness(harness_args())
    deleted = []
    remaining = iter([[51], []])

    async def fake_delete(wid):
        deleted.append(wid)

    harness.delete_workspace = fake_delete
    harness.ws_id = "ws1"
    monkeypatch.setattr(
        FUZZ, "scan_vmm_pids", lambda root, ids: next(remaining)
    )
    monkeypatch.setattr(FUZZ.asyncio, "sleep", nosleep)
    await harness.teardown_workspaces()
    assert deleted == ["ws1", "ws1"]


async def test_teardown_workspaces_keeps_when_asked(monkeypatch):
    harness = FUZZ.Harness(harness_args(keep_workspace=True))
    scanned = []

    def record_scan(root, ids):
        scanned.append(ids)
        return []

    monkeypatch.setattr(FUZZ, "scan_vmm_pids", record_scan)
    harness.ws_id = "ws1"
    await harness.teardown_workspaces()
    assert scanned == []


async def test_reap_strays_records_finding(monkeypatch):
    harness = FUZZ.Harness(harness_args())
    kills = []
    monkeypatch.setattr(FUZZ, "scan_vmm_pids", lambda root, ids: [31, 32])
    monkeypatch.setattr(
        FUZZ,
        "kill_verified",
        lambda pid, sig, root, ids: kills.append(pid) or True,
    )
    monkeypatch.setattr(FUZZ, "alive_pids", lambda pids: [])
    monkeypatch.setattr(FUZZ.asyncio, "sleep", nosleep)
    await harness.reap_strays("host sweep", {"ws"})
    assert kills == [31, 32]
    row = harness.summary.rows[0]
    assert row.status == FUZZ.FINDING
    assert "reaped 2 leftover VMM" in row.detail


async def test_reap_strays_records_survivors(monkeypatch):
    harness = FUZZ.Harness(harness_args())
    monkeypatch.setattr(FUZZ, "scan_vmm_pids", lambda root, ids: [31, 32])
    monkeypatch.setattr(FUZZ, "kill_verified", lambda pid, sig, r, i: True)
    monkeypatch.setattr(FUZZ, "alive_pids", lambda pids: [32])
    monkeypatch.setattr(FUZZ.asyncio, "sleep", nosleep)
    await harness.reap_strays("host sweep", {"ws"})
    row = harness.summary.rows[0]
    assert row.status == FUZZ.MISMATCH
    assert "survived SIGKILL" in row.detail


async def test_reap_strays_honors_keep_workspace(monkeypatch):
    harness = FUZZ.Harness(harness_args(keep_workspace=True))
    monkeypatch.setattr(
        FUZZ, "scan_vmm_pids", lambda root, ids: pytest.fail("scanned")
    )
    await harness.reap_strays("host sweep", {"ws"})
    assert harness.summary.rows == []


async def test_stop_daemon_records_problem(monkeypatch):
    harness = FUZZ.Harness(harness_args())

    async def fake_stop():
        return "msksd did not exit on SIGTERM within 90s\n"

    harness.daemon.stop = fake_stop
    monkeypatch.setattr(FUZZ, "scan_vmm_pids", lambda root, ids: [])
    await harness.stop_daemon()
    row = harness.summary.rows[0]
    assert row.status == FUZZ.MISMATCH
    assert "did not exit" in row.detail


async def test_stop_daemon_clean_when_graceful(monkeypatch):
    harness = FUZZ.Harness(harness_args())

    async def fake_stop():
        return None

    harness.daemon.stop = fake_stop
    monkeypatch.setattr(FUZZ, "scan_vmm_pids", lambda root, ids: [])
    await harness.stop_daemon()
    assert harness.summary.rows == []


def test_workspace_ids_skips_empty():
    harness = FUZZ.Harness(harness_args())
    assert harness.workspace_ids() == set()
    harness.ws_id = "a"
    harness.extra_ws = ["b"]
    assert harness.workspace_ids() == {"a", "b"}


def test_reap_result_shapes():
    row = FUZZ.reap_result("sweep", [5], [])
    assert row.status == FUZZ.FINDING
    assert "5" in row.detail
    row = FUZZ.reap_result("sweep", [5, 6], [6])
    assert row.status == FUZZ.MISMATCH
    assert "survived SIGKILL" in row.detail


# -- the reaper lifecycle ---------------------------------------------------


def test_arm_reaper_spawns_detached_watchdog(monkeypatch, tmp_path):
    harness = FUZZ.Harness(harness_args())
    recorded = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs

    monkeypatch.setattr(FUZZ.subprocess, "Popen", FakePopen)
    harness.daemon.state_dir = tmp_path
    harness.arm_reaper()
    argv = recorded["argv"]
    assert argv[0] == sys.executable
    assert argv[argv.index("--reap-for") + 1] == str(FUZZ.os.getpid())
    assert argv[argv.index("--state-dir") + 1] == str(tmp_path)
    kwargs = recorded["kwargs"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_arm_reaper_skips_attach_mode(monkeypatch):
    harness = FUZZ.Harness(
        harness_args(url="https://127.0.0.1:1", token="t", cafile="c")
    )
    monkeypatch.setattr(
        FUZZ.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned")
    )
    harness.arm_reaper()
    assert harness.reaper is None


def test_stand_down_writes_sentinel(tmp_path):
    harness = FUZZ.Harness(harness_args())
    harness.daemon.state_dir = tmp_path
    harness.reaper = object()
    harness.stand_down_reaper()
    assert (tmp_path / FUZZ.REAP_SENTINEL).exists()
    assert harness.reaper is None


def test_stand_down_survives_missing_dir(tmp_path):
    harness = FUZZ.Harness(harness_args())
    harness.daemon.state_dir = tmp_path / "gone"
    harness.reaper = object()
    harness.stand_down_reaper()
    assert harness.reaper is None
