"""The desktop-scheme pairing's suite (#1006, phase 6 of
#924): the light and dark chrome palettes, the startup
application from the desktop's scheme, the live switch through
the style hints' seam, the terminal grids that follow the
chrome, the fallback a desktop that names no scheme keeps, and
a program's own OSC colors that survive the switch — offscreen,
over real sleeper children through the hub's injected argv
factory (the zoom suite's own pattern, #976)."""

import ctypes
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

#: A child that announces itself and then waits for its parent
#: to hang up: the pairing never reads the child's bytes, but
#: the terminals it repaints are real children on real ptys.
SLEEPER = textwrap.dedent(
    """
    import time
    print("ready", flush=True)
    time.sleep(30)
    """
)

# The hub's pty children spawn through ``fork()`` on Qt's own
# threads (termsession's pty design, #951); Python 3.14 warns
# on the multi-threaded fork by design of the spawn, and the
# warning is the suite's own noise, not a defect to fix here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the pairing needs, with
    the scheme module imported inside (the window path's
    contract, #927) and the theme-handed palette restored at
    teardown — the shared test app's own, so later tests in
    this worker measure the platform's default."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtGui, QtWidgets

    from msks.client.gui import scheme as scheme_mod
    from msks.client.gui.terminal import paint

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    handed = QtGui.QPalette(app.palette())
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        scheme=scheme_mod,
        paint=paint,
    )
    app.setPalette(handed)
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def hub():
    """A fresh shell hub over sleeper children — the window
    path's own handoff shape, the zoom suite's (#976)."""
    from msks.client.gui.shells import ShellHub

    return ShellHub(
        argv_factory=lambda workspace_id: [
            sys.executable,
            "-c",
            SLEEPER,
        ]
    )


def grid_of(qt, widget):
    """One terminal's painted pair — the background and
    foreground the render state reports."""
    colors = qt.paint.palette(widget)
    assert colors is not None
    return (
        (colors.background.r, colors.background.g, colors.background.b),
        (colors.foreground.r, colors.foreground.g, colors.foreground.b),
    )


def window_color(qt):
    """The chrome's window role as the palette paints it."""
    return qt.app.palette().color(
        qt.QtGui.QPalette.ColorRole.Window
    )


def test_a_light_desktop_paints_the_light_palette_at_startup(qt):
    """A light desktop at startup (#1006): the chrome takes the
    style's own light standard palette and a fresh terminal's
    grid takes the light pair — white with a black face — the
    two sets painted together."""
    from msks.client.gui import ghostty as gt
    from msks.client.gui.terminal import colors

    hub_ = hub()
    try:
        scheme = qt.scheme.Scheme(
            hub_, qt.app, initial=qt.QtCore.Qt.ColorScheme.Light
        )
        assert scheme.standing == qt.QtCore.Qt.ColorScheme.Light
        assert window_color(qt) == qt.app.style().standardPalette().color(
            qt.QtGui.QPalette.ColorRole.Window
        )
        session = hub_.open_shell("ws")
        assert session.widget.color_scheme == gt.COLOR_SCHEME_LIGHT
        assert grid_of(qt, session.widget) == (
            colors.LIGHT_BACKGROUND,
            colors.LIGHT_FOREGROUND,
        )
    finally:
        hub_.shutdown()


def test_a_dark_desktop_paints_the_dark_palette_at_startup(qt):
    """A dark desktop at startup (#1006): the chrome takes the
    dark formula — the 53-gray window — and the grid takes the
    library's own default dark pair, the palette's own ends."""
    hub_ = hub()
    try:
        scheme = qt.scheme.Scheme(
            hub_, qt.app, initial=qt.QtCore.Qt.ColorScheme.Dark
        )
        assert scheme.standing == qt.QtCore.Qt.ColorScheme.Dark
        assert window_color(qt).name() == "#353535"
        session = hub_.open_shell("ws")
        assert grid_of(qt, session.widget) == ((29, 31, 33), (197, 200, 198))
    finally:
        hub_.shutdown()


def test_a_desktop_that_names_no_scheme_keeps_today_behavior(qt):
    """A desktop whose platform names no scheme (#1006) — the
    offscreen host's own answer: the chrome keeps the palette
    the theme handed it and the grid keeps the library's own
    defaults, the fallback the startup path already took."""
    hub_ = hub()
    try:
        handed = qt.QtGui.QPalette(qt.app.palette())
        scheme = qt.scheme.Scheme(hub_, qt.app)
        assert scheme.standing is None  # the offscreen answer: Unknown
        assert window_color(qt) == handed.color(
            qt.QtGui.QPalette.ColorRole.Window
        )
        session = hub_.open_shell("ws")
        assert session.widget.color_scheme is None
        # The library's fallback: the render state paints black
        # on white — no default the window path wrote.
        assert grid_of(qt, session.widget) == ((0, 0, 0), (255, 255, 255))
    finally:
        hub_.shutdown()


def test_a_scheme_change_switches_the_window_live(qt):
    """A desktop switch mid-session (#1006), driven through the
    style hints' seam — ``colorSchemeChanged``'s slot: the
    chrome repaints and the live terminal's grid follows in
    the same step, and a withdrawal back to no scheme keeps
    the standing set."""
    hub_ = hub()
    try:
        scheme = qt.scheme.Scheme(hub_, qt.app)
        session = hub_.open_shell("ws")
        scheme.switched(qt.QtCore.Qt.ColorScheme.Dark)
        assert window_color(qt).name() == "#353535"
        assert grid_of(qt, session.widget) == ((29, 31, 33), (197, 200, 198))
        scheme.switched(qt.QtCore.Qt.ColorScheme.Light)
        assert window_color(qt) == qt.app.style().standardPalette().color(
            qt.QtGui.QPalette.ColorRole.Window
        )
        assert grid_of(qt, session.widget) == ((255, 255, 255), (0, 0, 0))
        # The desktop withdrew its answer: the standing light
        # set keeps painting.
        scheme.switched(qt.QtCore.Qt.ColorScheme.Unknown)
        assert window_color(qt) == qt.app.style().standardPalette().color(
            qt.QtGui.QPalette.ColorRole.Window
        )
        assert grid_of(qt, session.widget) == ((255, 255, 255), (0, 0, 0))
    finally:
        hub_.shutdown()


def test_the_connection_sits_on_the_style_hints(qt):
    """The wiring (#1006): the pairing connects its switch slot
    on the application's style hints — the seam the desktop's
    ``colorSchemeChanged`` lands on."""
    hub_ = hub()
    try:
        before = qt.app.styleHints().receivers(
            qt.QtCore.SIGNAL("colorSchemeChanged(Qt::ColorScheme)")
        )
        qt.scheme.Scheme(hub_, qt.app)
        after = qt.app.styleHints().receivers(
            qt.QtCore.SIGNAL("colorSchemeChanged(Qt::ColorScheme)")
        )
        assert after == before + 1
    finally:
        hub_.shutdown()


def test_a_shell_opened_after_the_scheme_joins_it(qt):
    """A shell opened while a scheme stands (#1006): the fresh
    terminal paints from the library's fallback until the
    pairing reaches it — the hub's own open signal carries the
    standing set, the zoom's join shape (#976)."""
    hub_ = hub()
    try:
        scheme = qt.scheme.Scheme(
            hub_, qt.app, initial=qt.QtCore.Qt.ColorScheme.Dark
        )
        session = hub_.open_shell("ws")
        assert grid_of(qt, session.widget) == ((29, 31, 33), (197, 200, 198))
    finally:
        hub_.shutdown()


def test_a_retarget_carries_the_scheme_to_a_new_hub(qt):
    """The pairing across a reconnect (#1009): the hub the old
    shell owned closes with its connection, the pairing points
    at the fresh tree's hub, and a shell it opens joins the
    standing scheme. A re-ask for the same hub changes
    nothing."""
    old = hub()
    try:
        scheme = qt.scheme.Scheme(
            old, qt.app, initial=qt.QtCore.Qt.ColorScheme.Light
        )
        fresh = hub()
        try:
            scheme.retarget(fresh)
            assert scheme.hub is fresh
            session = fresh.open_shell("ws-new")
            assert grid_of(qt, session.widget) == (
                (255, 255, 255),
                (0, 0, 0),
            )
            scheme.retarget(fresh)  # the same hub: the no-op arm
            assert scheme.hub is fresh
        finally:
            fresh.shutdown()
    finally:
        old.shutdown()


def test_a_programs_own_colors_survive_the_switch(qt):
    """A program's OSC 11 background (#1006): the scheme lands
    on the default layer, so the program's own override keeps
    winning across the switch — the library's two layers, the
    new set repainting beneath the program's colors."""
    hub_ = hub()
    try:
        scheme = qt.scheme.Scheme(
            hub_, qt.app, initial=qt.QtCore.Qt.ColorScheme.Dark
        )
        session = hub_.open_shell("ws")
        session.widget.lib.vt_write(
            session.widget.terminal, b"\x1b]11;rgb:ff/00/00\x1b\\"
        )
        session.widget.refresh()
        assert grid_of(qt, session.widget)[0] == (255, 0, 0)
        scheme.switched(qt.QtCore.Qt.ColorScheme.Light)
        assert grid_of(qt, session.widget)[0] == (255, 0, 0)
    finally:
        hub_.shutdown()


def test_the_scheme_query_answers_the_applied_scheme(qt):
    """The child's light/dark query, CSI ? 996 n (#1006): a
    screen the scheme reached answers it with the library's own
    constant, and one no scheme reached answers nothing — the
    fallback the no-scheme desktop keeps."""
    from msks.client.gui import ghostty as gt
    from msks.client.gui.terminal import callbacks

    hub_ = hub()
    try:
        scheme = qt.scheme.Scheme(
            hub_, qt.app, initial=qt.QtCore.Qt.ColorScheme.Dark
        )
        session = hub_.open_shell("ws")
        widget = session.widget
        asked = ctypes.c_int(99)
        assert (
            callbacks.color_scheme(
                None, ctypes.c_void_p(id(widget)), ctypes.pointer(asked)
            )
            is True
        )
        assert asked.value == gt.COLOR_SCHEME_DARK
        scheme.switched(qt.QtCore.Qt.ColorScheme.Light)
        assert (
            callbacks.color_scheme(
                None, ctypes.c_void_p(id(widget)), ctypes.pointer(asked)
            )
            is True
        )
        assert asked.value == gt.COLOR_SCHEME_LIGHT
    finally:
        hub_.shutdown()
    fresh = hub()
    try:
        # A screen no scheme reached answers nothing.
        widget = fresh.open_shell("ws").widget
        asked = ctypes.c_int(99)
        assert (
            callbacks.color_scheme(
                None, ctypes.c_void_p(id(widget)), ctypes.pointer(asked)
            )
            is False
        )
    finally:
        fresh.shutdown()


def test_the_two_chrome_palettes_read_where_it_counts(qt):
    """The two chrome sets (#1006): the dark formula turns the
    reading roles over — the window, the base, the text — dims
    its disabled roles, and carries the formula's own blue
    highlight."""
    light = qt.scheme.light_palette(qt.app)
    dark = qt.scheme.dark_palette()
    roles = qt.QtGui.QPalette.ColorRole
    disabled = qt.QtGui.QPalette.ColorGroup.Disabled
    for role in (
        roles.Window,
        roles.Base,
        roles.Text,
    ):
        assert dark.color(role) != light.color(role)
    assert dark.color(roles.Window).name() == "#353535"
    assert dark.color(roles.Base).name() == "#2a2a2a"
    assert dark.color(disabled, roles.Text).name() == "#7f7f7f"
    assert dark.color(roles.Highlight).name() == "#2a82da"
