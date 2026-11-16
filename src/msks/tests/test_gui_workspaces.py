"""The workspace list screen (#939): the thin widget over the
presenter's row models — name, state, and the unresolved-mounts
marker — refreshed on a timer and a manual action through the
bridge, with a failed refresh flashing the status line while
the rows stand. The widget runs under the offscreen platform on
every host (#939); the screen's slots are driven directly and
through the stub bridge's signal emits, the bridge's own tests
holding the real worker's contract.
"""

import importlib.util
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client import data as clientdata  # noqa: E402
from msks.client.gui import rows as rows_mod  # noqa: E402

#: The poll cadence for the offscreen event loop, seconds — the
#: timer test pumps Qt's queue until the tick lands.
TICK = 0.01


class FakeSignal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once, in the GUI thread
    the real signal's queued delivery would land them in."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class StubBridge:
    """The bridge's surface for the screen's tests: the two
    signals the screen reads and the one verb it calls, every
    call recorded — no worker, no network."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))


def daemon_rows() -> list[dict]:
    """Three daemon rows out of lifecycle order, one with
    unresolved mounts — the listing an answer carries."""
    return [
        {"id": "ws-1", "name": "alpha", "status": "stopped"},
        {
            "id": "ws-2",
            "name": "beta",
            "status": "running",
            "mount_issues": [{"kind": "skipped"}],
        },
        {"id": "ws-3", "name": None, "status": "starting"},
    ]


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the screen needs, with the
    silent fontconfig the window test uses (Qt's shutdown-time
    re-scan runs after fixture teardown, so the variables sit on
    the environment directly, not monkeypatch — undo would hand
    the re-scan the host /etc/fonts). The Qt names import here
    too, inside the fixture: this module is imported at
    collection time by the test runner's controller process as
    well, and a controller has no business loading the binding
    (test_gui_bridge's docstring states the trap)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import workspaces as workspaces_mod
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    return SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        screen_mod=workspaces_mod,
    )


def make_screen(qt) -> tuple:
    """The screen over the stub bridge, shown in a real window —
    the shape every test reads: open, first refresh issued, tick
    running."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    return screen, window, bridge


def test_the_open_screen_issues_its_first_refresh(qt) -> None:
    """The window opens on the list (#939): showing the screen
    issues the first refresh through the bridge and starts the
    standing tick at the listing's cadence — the TUI's own five
    seconds (#470 L2)."""
    screen, window, bridge = make_screen(qt)
    assert bridge.calls == []  # construction alone asks nothing
    window.show()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    assert screen.timer.isActive()
    assert screen.timer.interval() == qt.screen_mod.REFRESH_MS
    window.close()


def test_the_list_renders_the_row_models(qt) -> None:
    """The row set the TUI builds (#939): one row per model, the
    label in its column, the state word in the other with the
    unresolved-mounts marker ahead of it, and the lifecycle rank
    carried as the state cell's sort data."""
    screen, window, _bridge = make_screen(qt)
    window.show()
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert screen.table.rowCount() == 3
    labels = [screen.table.item(row, 0).text() for row in range(3)]
    assert labels == ["alpha", "beta", "ws-3"]
    assert screen.table.item(1, 1).text() == "✗1 running"
    assert screen.table.item(0, 1).text() == "stopped"
    rank = qt.screen_mod.RANK_ROLE
    assert screen.table.item(1, 1).data(rank) == rows_mod.state_rank("running")
    window.close()


def test_a_later_answer_replaces_the_rows(qt) -> None:
    """Each answer rebuilds the table whole (#939): the second
    listing replaces the first — no accumulation, and an empty
    listing empties the screen without leaving the last rows
    behind."""
    screen, window, _bridge = make_screen(qt)
    window.show()
    screen.on_succeeded("workspaces-list", daemon_rows())
    screen.on_succeeded("workspaces-list", [])
    assert screen.table.rowCount() == 0
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
    assert screen.table.rowCount() == 1
    window.close()


def test_a_failed_refresh_flashes_and_keeps_the_rows(qt) -> None:
    """The failure contract (#939, AGENTS.md \"Exception
    handling\"): a refused refresh flashes its message on the
    status line while the rows already on screen stand — the
    operator keeps the list the last answer built — and the next
    successful answer clears the flash with the rows it
    rebuilds."""
    screen, window, _bridge = make_screen(qt)
    window.show()
    screen.on_succeeded("workspaces-list", daemon_rows())
    screen.on_failed(
        "workspaces-list",
        clientdata.RestFailure("409: workspace is running", reach=False),
    )
    assert "409" in screen.status.text()
    assert screen.table.rowCount() == 3  # the rows stand
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
    assert screen.status.text() == ""  # the answer cleared it
    window.close()


def test_the_single_flash_clears_on_the_timeout(qt) -> None:
    """The flash's own span (#939): the timeout's slot clears
    the line without waiting for the next answer."""
    screen, window, _bridge = make_screen(qt)
    window.show()
    screen.flash("msks: the daemon is unreachable")
    assert "unreachable" in screen.status.text()
    screen.clear_flash()
    assert screen.status.text() == ""
    window.close()


def test_another_keys_answer_is_not_the_screens(qt) -> None:
    """The key filter (#939): the bridge carries every screen's
    calls on the one worker — this screen answers its own key
    alone, so another key's result does not rebuild the table
    and another key's failure does not flash the line."""
    screen, window, _bridge = make_screen(qt)
    window.show()
    screen.on_succeeded("workspaces-list", daemon_rows())
    screen.on_succeeded("some-other-key", [])
    assert screen.table.rowCount() == 3
    screen.on_failed(
        "some-other-key", clientdata.RestFailure("nope", reach=False)
    )
    assert screen.status.text() == ""
    window.close()


def test_the_manual_action_refreshes_through_the_bridge(qt) -> None:
    """The manual action (#939): the button's click and the
    timer's tick share the one refresh verb, both through the
    bridge."""
    screen, window, bridge = make_screen(qt)
    window.show()
    bridge.calls.clear()  # the open refresh is already issued
    screen.refresh_button.click()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    bridge.calls.clear()
    # A one-millisecond tick: the pump spins Qt's queue until
    # the standing timer's refresh lands — the wiring the click
    # alone cannot pin.
    screen.timer.setInterval(1)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not bridge.calls:
        qt.app.processEvents()
        time.sleep(TICK)
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    window.close()


def test_the_tick_stops_with_the_closed_window(qt) -> None:
    """A timer tick must not touch widgets after close (#939):
    closing the window hides the screen and its hide hook stops
    the tick; re-showing resumes it with a fresh refresh."""
    screen, window, bridge = make_screen(qt)
    window.show()
    assert screen.timer.isActive()
    window.close()
    assert not screen.timer.isActive()
    bridge.calls.clear()
    window.show()
    assert screen.timer.isActive()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    window.close()


def test_the_state_column_sorts_by_lifecycle_rank(qt) -> None:
    """The rank the presenter carries (#938) is the sort the
    widget serves (#939): a state-column click orders the rows
    by the vocabulary's own sequence — a state outside it last —
    not by the state word's spelling."""
    screen, window, _bridge = make_screen(qt)
    window.show()
    screen.on_succeeded(
        "workspaces-list",
        [
            {"id": "ws-1", "name": "alpha", "status": "stopped"},
            {"id": "ws-2", "name": "beta", "status": "running"},
            {"id": "ws-3", "name": "gamma", "status": "hibernating"},
            {"id": "ws-4", "name": "delta", "status": "created"},
        ],
    )
    screen.table.sortItems(1, qt.QtCore.Qt.SortOrder.AscendingOrder)
    labels = [screen.table.item(row, 0).text() for row in range(4)]
    assert labels == ["delta", "beta", "alpha", "gamma"]
    window.close()
