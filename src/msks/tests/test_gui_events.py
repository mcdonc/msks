"""The workspace Events tab (#989): the receipt stamp, the row
text over the presenter's summary, and the window's cap and
clear — the pane the page once carried, moved whole. The
widgets run under the offscreen platform on every host.
"""

import importlib.util
import os
import time
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.detail import EVENTS_LIMIT  # noqa: E402


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the tab needs, the dialog
    tests' own fixture shape (#962)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import events as events_mod
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(
        app=app, QtWidgets=QtWidgets, keep=[], events=events_mod
    )
    yield ns
    del ns.keep[:]
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    from PySide6 import QtCore

    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def test_the_stamp_reads_the_local_clock() -> None:
    """The receipt stamp (#989): the local clock's time-of-day —
    the daemon's live stream carries no stamp of its own, so
    the row reads the moment the window received the frame."""
    from msks.client.gui.events import stamp_text

    fixed = time.mktime(
        time.strptime("2026-01-04 12:03:22", "%Y-%m-%d %H:%M:%S")
    )
    assert stamp_text(fixed) == "12:03:22"


def test_the_row_keeps_stamp_kind_and_summary() -> None:
    """The row's text (#989): the stamp, the kind, and the
    summary the presenter builds — the row reads as a log
    line."""
    from msks.client.gui.events import row_text

    text = row_text(
        {"event": "workspace.status", "data": {"id": "ws-a", "status": "x"}},
        0.0,
    )
    stamp, kind, summary = text.split("  ", 2)
    assert stamp  # the epoch reads as the local clock's day zero
    assert kind == "workspace.status"
    assert summary == "status x"


def test_the_window_appends_caps_and_clears(qt) -> None:
    """The tab's window (#989): frames append newest last, the
    window caps at the presenter's own limit — the oldest row
    leaves first — and the open's clear empties it whole."""
    tab = qt.events.EventsTab()
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.show()
    qt.keep.append(window)
    assert tab.rows.isHidden() and not tab.empty.isHidden()
    tab.push({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert tab.rows.count() == 1
    assert not tab.rows.isHidden() and tab.empty.isHidden()
    for n in range(EVENTS_LIMIT + 5):
        tab.push({"event": "noise", "data": {"id": "ws-a"}})
    assert tab.rows.count() == EVENTS_LIMIT
    # The oldest row leaves first: the first frame's row is gone.
    assert tab.rows.item(0).text().split("  ", 2)[1] == "noise"
    tab.clear()
    assert tab.rows.count() == 0
    assert tab.rows.isHidden() and not tab.empty.isHidden()
    window.close()
