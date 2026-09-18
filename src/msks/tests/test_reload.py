"""The development reload watcher (#144)."""

import os

import pytest
from msks.server.reload import (
    POLL_SECONDS,
    arm_reload_watcher,
    changed,
    package_roots,
    restart_argv,
    snapshot,
    watch_loop,
)


class LoopDone(Exception):
    """Raised by the fake sleep to end a watch_loop under test."""


def write(path: str, content: bytes = b"x") -> None:
    with open(path, "wb") as f:
        f.write(content)


def test_snapshot_fingerprints_python_files(tmp_path):
    write(str(tmp_path / "a.py"))
    write(str(tmp_path / "b.txt"))
    snap = snapshot([str(tmp_path)])
    assert set(snap) == {str(tmp_path / "a.py")}
    mtime, size = snap[str(tmp_path / "a.py")]
    assert size == 1
    assert mtime > 0


def test_snapshot_walks_subdirectories(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    write(str(sub / "c.py"))
    assert set(snapshot([str(tmp_path)])) == {str(sub / "c.py")}


def test_snapshot_skips_files_deleted_mid_walk(tmp_path, monkeypatch):
    # A listed file that disappears before stat is skipped, not fatal
    # (an editor's atomic rename does this between listdir and stat).
    write(str(tmp_path / "gone.py"))
    real_stat = os.stat

    def failing_stat(path, **kwargs):
        if str(path).endswith("gone.py"):
            raise FileNotFoundError(path)
        return real_stat(path, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)
    assert snapshot([str(tmp_path)]) == {}


def test_changed_detects_add_remove_and_touch(tmp_path):
    before = snapshot([str(tmp_path)])
    assert not changed(before, snapshot([str(tmp_path)]))
    write(str(tmp_path / "new.py"))
    after = snapshot([str(tmp_path)])
    assert changed(before, after)
    write(str(tmp_path / "new.py"), b"longer content")
    assert changed(after, snapshot([str(tmp_path)]))
    os.remove(str(tmp_path / "new.py"))
    assert not changed(before, snapshot([str(tmp_path)]))


def test_package_roots_is_the_msks_package_dir():
    (root,) = package_roots()
    assert os.path.basename(root) == "msks"
    assert os.path.isfile(os.path.join(root, "server", "reload.py"))


def test_restart_argv_preserves_flags(monkeypatch):
    monkeypatch.setattr("sys.executable", "/fake/python")
    monkeypatch.setattr(
        "sys.argv",
        ["/fake/msksd", "--config", "/run/msksd/msksd.yaml", "--reload"],
    )
    assert restart_argv() == [
        "/fake/python",
        "-m",
        "msks.server.main",
        "--config",
        "/run/msksd/msksd.yaml",
        "--reload",
    ]


def test_watch_loop_restarts_on_change(tmp_path, monkeypatch):
    write(str(tmp_path / "a.py"))
    calls = {"sleeps": 0, "restarts": []}

    def fake_sleep(seconds):
        assert seconds == POLL_SECONDS
        calls["sleeps"] += 1
        if calls["sleeps"] == 1:
            # The edit lands between the first and second poll.
            write(str(tmp_path / "a.py"), b"changed")
        elif calls["sleeps"] == 3:
            raise LoopDone

    def fake_restart(argv):
        calls["restarts"].append(list(argv))

    monkeypatch.setattr("sys.executable", "/fake/python")
    monkeypatch.setattr(
        "sys.argv", ["/fake/msksd", "--reload", "--config", "none"]
    )
    with pytest.raises(LoopDone):
        watch_loop([str(tmp_path)], sleep=fake_sleep, restart=fake_restart)
    assert len(calls["restarts"]) == 1
    assert calls["restarts"][0][0] == "/fake/python"


def test_watch_loop_no_restart_without_change(tmp_path):
    write(str(tmp_path / "a.py"))

    def fake_sleep(_seconds):
        raise LoopDone

    restarts: list[list[str]] = []

    with pytest.raises(LoopDone):
        watch_loop([str(tmp_path)], sleep=fake_sleep, restart=restarts.append)
    assert restarts == []


def test_arm_reload_watcher_starts_daemon_thread(monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    seen = {}

    def fake_loop(roots):
        seen["roots"] = list(roots)
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr("msks.server.reload.watch_loop", fake_loop)
    arm_reload_watcher()
    assert started.wait(timeout=5)
    threads = [t for t in threading.enumerate() if t.name == "msksd-reload"]
    assert threads and threads[0].daemon and threads[0].is_alive()
    assert seen["roots"] == package_roots()
    release.set()
    threads[0].join(timeout=5)


def test_watch_loop_survives_a_failed_restart(tmp_path):
    """A failed exec must not kill the watcher thread.

    The interpreter path can vanish under a live-shared tree; the
    watcher logs, adopts the changed baseline, and keeps serving
    the current process until the next edit retries.
    """
    write(str(tmp_path / "a.py"))
    calls = {"sleeps": 0, "restarts": 0}

    def fake_sleep(seconds):
        assert seconds == POLL_SECONDS
        calls["sleeps"] += 1
        if calls["sleeps"] == 1:
            write(str(tmp_path / "a.py"), b"changed")
        elif calls["sleeps"] == 3:
            raise LoopDone

    def failing_restart(_argv):
        calls["restarts"] += 1
        raise OSError("interpreter vanished")

    with pytest.raises(LoopDone):
        watch_loop([str(tmp_path)], sleep=fake_sleep, restart=failing_restart)
    assert calls["restarts"] == 1
