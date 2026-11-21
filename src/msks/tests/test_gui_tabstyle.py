"""The tab strips' active-tab mark (#1012): the accent
underline and tinted background the selected tab carries on
every strip the window owns. The sheet is colored from the
bar's own palette, the underline itself is pinned by pixel
under the offscreen platform (the mark is paint, and a string
assert alone would not say the tab renders it), and the tab
geometry holds as the selection crosses — the underline and
the focus outline are reserved space, colors swapping only.
"""

import importlib.util
import os
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.tabstyle import active_sheet, mark_active  # noqa: E402


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the bar needs, the list's
    own fixture shape (#939): the silent fontconfig, the
    platform pin, and the Qt names imported inside the fixture
    (a collection-time controller has no business loading the
    binding)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        keep=[],
    )
    yield ns
    del ns.keep[:]
    # The same teardown the screens' fixtures owe their worker
    # (#959): close and delete every top-level widget, then flush
    # the deferred deletions.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def shown_bar(qt, *labels: str):
    """A styled bar of the given labels, first tab current,
    shown in a real window — the shape the pixel probe reads,
    the window kept alive for the test's span."""
    bar = qt.QtWidgets.QTabBar()
    for label in labels:
        bar.addTab(label)
    mark_active(bar)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(bar)
    window.show()
    qt.keep.append(window)
    return bar


def near(qt, seen, wanted, tol: int = 48) -> bool:
    """Whether two colors read as one at the probe's tolerance:
    the sheet's own blend (the tint over the base) never lands
    on the accent's exact triple, and a rendered pixel carries
    the antialiasing of the corners beside it."""
    return all(
        abs(a - b) <= tol
        for a, b in (
            (seen.red(), wanted.red()),
            (seen.green(), wanted.green()),
            (seen.blue(), wanted.blue()),
        )
    )


def accent_rows(qt, bar, index: int) -> list[int]:
    """The rows under one tab that read as the palette's accent
    (#1012): a row counts when its interior carries the accent
    across at least four fifths of its width — the underline is
    a bar, not a speck."""
    image = bar.grab().toImage()
    accent = bar.palette().color(qt.QtGui.QPalette.ColorRole.Highlight)
    rect = bar.tabRect(index)
    rows: list[int] = []
    for y in range(rect.top(), rect.bottom() + 1):
        inside = range(rect.left() + 4, rect.right() - 4)
        hits = sum(
            1
            for x in inside
            if near(qt, qt.QtGui.QColor(image.pixel(x, y)), accent)
        )
        if hits > (rect.width() - 8) * 0.8:
            rows.append(y)
    return rows


def test_the_sheet_paints_the_palette_accent(qt) -> None:
    """The mark's colors from the bar's own palette (#1012):
    the accent names the underline, the Button base names every
    tab, and the sheet states the selected tab, the hover, and
    the keyboard's focus — the states the mark renders."""
    bar = qt.QtWidgets.QTabBar()
    mark_active(bar)
    sheet = bar.styleSheet()
    assert sheet == active_sheet(bar)
    accent = bar.palette().color(qt.QtGui.QPalette.ColorRole.Highlight).name()
    base = bar.palette().color(qt.QtGui.QPalette.ColorRole.Button).name()
    assert accent in sheet  # the underline
    assert base in sheet  # the tab's own base
    assert "QTabBar::tab:selected" in sheet
    assert "QTabBar::tab:hover:!selected" in sheet
    assert "QTabBar::tab:focus" in sheet


def test_the_selected_tab_carries_the_underline(qt) -> None:
    """The mark as paint (#1012): the current tab renders a row
    of the palette's accent at its foot, the tab beside it
    none — the difference the issue asks to read at a glance,
    pinned under the offscreen platform's own renderer."""
    bar = shown_bar(qt, "Shells", "Mounts", "Info")
    qt.app.processEvents()
    assert len(accent_rows(qt, bar, 0)) >= 2  # the 3px underline
    assert accent_rows(qt, bar, 1) == []
    assert accent_rows(qt, bar, 2) == []


def test_the_underline_follows_the_selection(qt) -> None:
    """The mark moves with the current tab (#1012): the
    underline crosses to the tab the operator picks, and the
    tab geometry holds on the way — the underline is reserved
    space, so no tab moves as the selection crosses it."""
    bar = shown_bar(qt, "Shells", "Mounts")
    qt.app.processEvents()
    rects = (bar.tabRect(0), bar.tabRect(1))
    bar.setCurrentIndex(1)
    qt.app.processEvents()
    assert accent_rows(qt, bar, 0) == []
    assert len(accent_rows(qt, bar, 1)) >= 2
    assert (bar.tabRect(0), bar.tabRect(1)) == rects
