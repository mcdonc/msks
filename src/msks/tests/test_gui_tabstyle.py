"""The tab strips' active-tab mark (#1012): the accent-tinted
background the selected tab carries on every strip the window
owns. The sheet is colored from the bar's own palette, the
tint itself is pinned by pixel under the offscreen platform
(the mark is paint, and a string assert alone would not say
the tab renders it), the mark recolors when the palette
changes under the window, and the tab geometry holds as the
selection crosses — the focus outline is reserved space,
colors swapping only.
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

from msks.client.gui.tabstyle import (  # noqa: E402
    HOVER_ALPHA,
    TINT_ALPHA,
    active_sheet,
    mark_active,
    rgba,
)


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


def near(seen, wanted, tol: int = 32) -> bool:
    """Whether two colors read as one at the probe's tolerance:
    a rendered pixel carries the antialiasing of the corners
    beside it, and the tint over the base is a blend of the
    palette's own triples, never the accent's exact one."""
    return all(
        abs(a - b) <= tol
        for a, b in (
            (seen.red(), wanted.red()),
            (seen.green(), wanted.green()),
            (seen.blue(), wanted.blue()),
        )
    )


def tinted(qt, accent, base, alpha: int):
    """The color the sheet's rgba fills render: the accent at
    the given alpha over the base, the blend Qt's own painter
    makes of the two."""
    mixed = qt.QtGui.QColor(
        (accent.red() * alpha + base.red() * (255 - alpha)) // 255,
        (accent.green() * alpha + base.green() * (255 - alpha)) // 255,
        (accent.blue() * alpha + base.blue() * (255 - alpha)) // 255,
    )
    mixed.setAlpha(255)
    return mixed


def tab_fill(qt, bar, index: int):
    """One tab's rendered fill (#1012): the band just under the
    tab's top border, above where the label's glyphs paint — a
    solid strip of the fill alone, its majority color read
    across the tab's interior."""
    image = bar.grab().toImage()
    rect = bar.tabRect(index)
    band = range(rect.left() + 4, rect.right() - 4)
    counts: dict[str, int] = {}
    for x in band:
        color = qt.QtGui.QColor(image.pixel(x, rect.top() + 3)).name()
        counts[color] = counts.get(color, 0) + 1
    return qt.QtGui.QColor(max(counts, key=counts.get))


def palette_colors(qt, bar):
    """The session's accent and base, the sheet's source: the
    application's palette, the one the sheet itself reads."""
    palette = qt.app.palette()
    return (
        palette.color(qt.QtGui.QPalette.ColorRole.Highlight),
        palette.color(qt.QtGui.QPalette.ColorRole.Button),
    )


def test_the_sheet_paints_the_palette_accent(qt) -> None:
    """The mark's colors from the session palette (#1012): the
    Button base names every tab, the Highlight tint names the
    selected one at its full weight and the pointer's hover at
    a weaker one, and the sheet states the keyboard's focus —
    the states the mark renders."""
    bar = qt.QtWidgets.QTabBar()
    mark_active(bar)
    sheet = bar.styleSheet()
    assert sheet == active_sheet()
    accent, base = palette_colors(qt, bar)
    assert base.name() in sheet  # the tab's own base
    assert f"rgba({accent.red()}," in sheet  # the tints
    # The hover leans toward the mark at a weaker weight
    # (#1012): the accent at HOVER_ALPHA, under the selected
    # tab's TINT_ALPHA.
    assert (
        rgba(
            qt.QtGui.QColor(
                accent.red(), accent.green(), accent.blue(), HOVER_ALPHA
            )
        )
        in sheet
    )
    assert "QTabBar::tab:selected" in sheet
    assert "QTabBar::tab:hover:!selected" in sheet
    assert "QTabBar::tab:focus" in sheet


def test_the_selected_tab_carries_the_tint(qt) -> None:
    """The mark as paint (#1012): the current tab renders the
    accent's tint over the base, the tabs beside it the base
    alone — the difference the issue asks to read at a glance,
    pinned under the offscreen platform's own renderer."""
    bar = shown_bar(qt, "Shells", "Mounts", "Info")
    qt.app.processEvents()
    accent, base = palette_colors(qt, bar)
    assert near(tab_fill(qt, bar, 0), tinted(qt, accent, base, TINT_ALPHA))
    assert near(tab_fill(qt, bar, 1), base)
    assert near(tab_fill(qt, bar, 2), base)


def test_the_tint_follows_the_selection(qt) -> None:
    """The mark moves with the current tab (#1012): the tint
    crosses to the tab the operator picks, and the tab
    geometry holds on the way — the focus outline is reserved
    space, so no tab moves as the selection crosses it."""
    bar = shown_bar(qt, "Shells", "Mounts")
    qt.app.processEvents()
    rects = (bar.tabRect(0), bar.tabRect(1))
    bar.setCurrentIndex(1)
    qt.app.processEvents()
    accent, base = palette_colors(qt, bar)
    assert near(tab_fill(qt, bar, 0), base)
    assert near(tab_fill(qt, bar, 1), tinted(qt, accent, base, TINT_ALPHA))
    assert (bar.tabRect(0), bar.tabRect(1)) == rects


def test_the_mark_recolors_when_the_palette_changes(qt) -> None:
    """The mark follows the session's palette (#1012): a
    palette that lands after the sheet — the session switching
    its theme under a live window — recolors it through the
    watcher, the new accent naming the tint the selected tab
    carries. The bar the sheet paints freezes its own palette,
    so the change is driven the way a session drives it: on
    the application, the plain widgets carrying the event the
    watcher reads. The application's own palette stands back
    afterwards, the same watcher recoloring on the restore."""
    bar = shown_bar(qt, "Shells", "Mounts")
    qt.app.processEvents()
    before = bar.styleSheet()
    original = qt.QtGui.QPalette(qt.app.palette())
    fresh = qt.QtGui.QColor("#ff00ff")
    palette = qt.QtGui.QPalette(original)
    palette.setColor(qt.QtGui.QPalette.ColorRole.Highlight, fresh)
    try:
        qt.app.setPalette(palette)
        qt.app.processEvents()
        after = bar.styleSheet()
        assert after != before
        assert f"rgba({fresh.red()}," in after
        base = original.color(qt.QtGui.QPalette.ColorRole.Button)
        assert near(tab_fill(qt, bar, 0), tinted(qt, fresh, base, TINT_ALPHA))
    finally:
        qt.app.setPalette(original)


def test_the_focused_tab_carries_the_dotted_rim(qt) -> None:
    """The keyboard's mark as paint (#1012): the focused tab
    dots its rim in the palette's mid tone — the strip joins
    the tab chain (#962), and an arrow landing on it must show
    where the keyboard stands. The rim is a dotted border, so
    the probe counts the dots, not a solid row, and the
    baseline is taken with the focus cleared: an offscreen
    window hands its one focusable child the focus at open."""
    bar = shown_bar(qt, "Shells", "Mounts")
    qt.app.processEvents()  # the window activates and focuses first
    bar.setFocusPolicy(qt.QtCore.Qt.FocusPolicy.StrongFocus)
    bar.clearFocus()
    qt.app.processEvents()

    def mid_fraction() -> float:
        image = bar.grab().toImage()
        rect = bar.tabRect(0)
        mid = qt.app.palette().color(qt.QtGui.QPalette.ColorRole.Mid)
        inside = range(rect.left() + 2, rect.right() - 2)
        hits = sum(
            1
            for x in inside
            if near(qt.QtGui.QColor(image.pixel(x, rect.top())), mid)
        )
        return hits / max(1, rect.width() - 4)

    unfocused = mid_fraction()
    bar.setFocus()
    qt.app.processEvents()
    assert mid_fraction() > max(unfocused, 0.2)
