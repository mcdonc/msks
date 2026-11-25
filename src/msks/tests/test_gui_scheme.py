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
    from msks.client.gui import scheme as scheme_mod
    from msks.client.gui.terminal import paint
    from PySide6 import QtCore, QtGui, QtWidgets

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
    return qt.app.palette().color(qt.QtGui.QPalette.ColorRole.Window)


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
        # The light chrome's window (#1006): the constant light
        # set, Fusion's own #efefef — not the style's live
        # scheme-aware standardPalette read.
        assert window_color(qt).name() == "#efefef"
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
        # The library's fallback: the render state paints a
        # black page with a white face — no default the window
        # path wrote.
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
        assert window_color(qt).name() == "#efefef"
        assert grid_of(qt, session.widget) == ((255, 255, 255), (0, 0, 0))
        # The desktop withdrew its answer: the standing light
        # set keeps painting.
        scheme.switched(qt.QtCore.Qt.ColorScheme.Unknown)
        assert window_color(qt).name() == "#efefef"
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


def test_a_real_scheme_signal_lands_the_switch(qt):
    """The slot the hints feed (#1006): a signal carrying the
    scheme enum — ``colorSchemeChanged``'s own payload — lands
    the switch through the connection, not only a direct call.
    The offscreen platform never switches on its own, so the
    suite drives a same-typed signal into the slot."""

    class Desktop(qt.QtCore.QObject):
        switched = qt.QtCore.Signal(qt.QtCore.Qt.ColorScheme)

    hub_ = hub()
    try:
        scheme = qt.scheme.Scheme(hub_, qt.app)
        desktop = Desktop()
        desktop.switched.connect(scheme.switched)
        desktop.switched.emit(qt.QtCore.Qt.ColorScheme.Dark)
        assert window_color(qt).name() == "#353535"
        desktop.switched.emit(qt.QtCore.Qt.ColorScheme.Light)
        assert window_color(qt).name() == "#efefef"
    finally:
        hub_.shutdown()


def test_a_shell_opened_after_the_scheme_joins_it(qt, monkeypatch):
    """A shell opened while a scheme stands (#1006): the fresh
    terminal paints from the library's fallback until the
    pairing reaches it — the hub's own open signal carries the
    standing set, the zoom's join shape (#976), and the walk
    skips a terminal the standing set already reached."""
    from msks.client.gui.terminal import colors

    hub_ = hub()
    try:
        qt.scheme.Scheme(hub_, qt.app, initial=qt.QtCore.Qt.ColorScheme.Dark)
        first = hub_.open_shell("ws")
        assert grid_of(qt, first.widget) == ((29, 31, 33), (197, 200, 198))
        applied = []
        real = colors.apply
        monkeypatch.setattr(
            colors,
            "apply",
            lambda screen, scheme: (
                applied.append(screen) or real(screen, scheme)
            ),
        )
        second = hub_.open_shell("ws")
        assert applied == [second.widget]  # the walk skipped the first
        assert grid_of(qt, second.widget) == ((29, 31, 33), (197, 200, 198))
    finally:
        hub_.shutdown()


def test_one_scheme_builds_one_shared_color_set(qt):
    """The built sets are shared (#1006): the library's answers
    are pure functions of the scheme, so a second ask for the
    same scheme takes the set the first built — every terminal
    a switch reaches writes the same values."""
    from msks.client.gui import ghostty as gt
    from msks.client.gui.terminal import colors

    lib = gt.Lib()
    first = colors.colors_for(gt.COLOR_SCHEME_DARK, lib)
    second = colors.colors_for(gt.COLOR_SCHEME_DARK, lib)
    assert first is second
    assert first.background == (29, 31, 33)
    light = colors.colors_for(gt.COLOR_SCHEME_LIGHT, lib)
    assert light.background == colors.LIGHT_BACKGROUND
    assert light.palette[250] != first.palette[250]  # the ramp turned over


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
    light = qt.scheme.light_palette()
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
    # The roles Qt's own dark derivation adds (#1006 review):
    # the placeholder the forms and the find bar lean on dims
    # to the gray, not the black a fresh palette carries — and
    # the accent names the highlight's blue.
    assert dark.color(roles.PlaceholderText).name() == "#7f7f7f"
    assert dark.color(roles.Accent).name() == "#2a82da"


def test_the_light_base_sixteen_reads_on_the_white_page(qt):
    """The light set's base sixteen (#1006): every color the
    generator would keep from the dark family reads as text on
    the white page — the library's own contrast measure — with
    bright yellow the one documented exception (the day
    family's own yellow, every light theme's weakest text).
    """
    from msks.client.gui import ghostty as gt
    from msks.client.gui.terminal import colors

    lib = gt.Lib()

    def contrast(a, b):
        return lib.color_contrast(
            ctypes.byref(colors.rgb(a)), ctypes.byref(colors.rgb(b))
        )

    for index, values in enumerate(colors.LIGHT_BASE_16):
        ratio = contrast(values, colors.LIGHT_BACKGROUND)
        if index == 11:  # bright yellow, the documented caveat
            assert ratio >= 1.8
        else:
            assert ratio >= 2.4, (index, values, ratio)
    # The two ends the review probed — white and bright white —
    # now the grays, at the ratios a light theme ships.
    assert contrast(colors.LIGHT_BASE_16[7], colors.LIGHT_BACKGROUND) >= 5
    assert contrast(colors.LIGHT_BASE_16[15], colors.LIGHT_BACKGROUND) >= 2.8


def test_every_tint_reads_on_its_bases(qt):
    """The three tints (#1160) pass 4.5:1 on their scheme's base
    and alternate base alike: the floor text reads by, the one
    a color-blind reader and a grayscale screenshot keep."""
    floor = 4.5
    for palette, tints in (
        (qt.scheme.dark_palette(), qt.scheme._DARK_SEMANTIC),
        (qt.scheme.light_palette(), qt.scheme._LIGHT_SEMANTIC),
    ):
        roles = qt.QtGui.QPalette.ColorRole
        for base_role in (roles.Base, roles.AlternateBase):
            base = palette.color(base_role)
            for kind, values in tints.items():
                ratio = qt.scheme.contrast(qt.QtGui.QColor(*values), base)
                assert ratio >= floor, (kind, base_role, ratio)


def test_the_tints_read_the_scheme_off_the_base(qt):
    """A kind's color comes from the base the palette paints on
    (#1160): the dark set on the dark chrome, the light set on
    the light one, None for no kind at all."""
    dark = qt.scheme.dark_palette()
    light = qt.scheme.light_palette()
    assert qt.scheme.is_dark(dark) and not qt.scheme.is_dark(light)

    def hexed(kind, tints):
        red, green, blue = tints[kind]
        return f"#{red:02x}{green:02x}{blue:02x}"

    for kind in (qt.scheme.OK, qt.scheme.WARN, qt.scheme.ERROR):
        assert qt.scheme.semantic(kind, dark).name() == hexed(
            kind, qt.scheme._DARK_SEMANTIC
        )
        assert qt.scheme.semantic(kind, light).name() == hexed(
            kind, qt.scheme._LIGHT_SEMANTIC
        )
    assert qt.scheme.semantic(None, dark) is None
    assert qt.scheme.semantic("else", light) is None


def test_the_delegate_tints_an_unselected_cell_alone(qt):
    """The delegate (#1160) writes the kind's color into the
    option's text roles for an untagged-by-selection cell, and
    leaves a selected cell's palette alone: the highlight's own
    text keeps its contrast. An untagged cell paints as the
    style would."""
    table = qt.QtWidgets.QTableWidget(2, 1)
    delegate = qt.scheme.SemanticDelegate(table)
    table.setItemDelegate(delegate)
    failed = qt.QtWidgets.QTableWidgetItem("failed (bind refused)")
    failed.setData(qt.scheme.SEMANTIC_ROLE, qt.scheme.ERROR)
    plain = qt.QtWidgets.QTableWidgetItem("stopped")
    table.setItem(0, 0, failed)
    table.setItem(1, 0, plain)
    State = qt.QtWidgets.QStyle.StateFlag
    roles = qt.QtGui.QPalette.ColorRole
    base = qt.QtGui.QPalette(qt.scheme.dark_palette())
    error = qt.scheme.semantic(qt.scheme.ERROR, base)

    def text_color(index, state):
        option = qt.QtWidgets.QStyleOptionViewItem()
        option.palette = qt.QtGui.QPalette(base)
        option.state = state
        delegate.initStyleOption(option, index)
        return option.palette.brush(roles.Text).color()

    index = table.model().index(0, 0)
    assert text_color(index, State.State_Enabled) == error
    assert text_color(index, State.State_Enabled | State.State_Selected) == (
        base.color(roles.Text)
    )
    assert text_color(table.model().index(1, 0), State.State_Enabled) == (
        base.color(roles.Text)
    )


def test_a_tinted_label_follows_the_scheme_switch(qt):
    """A tinted label (#1160) wears its kind on the base it sits
    on, takes the kind's other color when the application
    palette switches, and with no kind keeps following the
    application palette's own text — the resolve mask names the
    text roles alone."""
    roles = qt.QtGui.QPalette.ColorRole
    label = qt.scheme.TintedLabel("daemon back")
    label.show()
    qt.app.processEvents()
    label.set_kind(qt.scheme.ERROR)
    dark = qt.scheme.dark_palette()
    qt.app.setPalette(dark)
    qt.app.processEvents()
    assert label.palette().color(roles.Text) == qt.scheme.semantic(
        qt.scheme.ERROR, dark
    )
    assert label.palette().color(roles.WindowText) == label.palette().color(
        roles.Text
    )
    # The base follows the application, not the label's own palette.
    assert label.palette().color(roles.Base) == dark.color(roles.Base)
    light = qt.scheme.light_palette()
    qt.app.setPalette(light)
    qt.app.processEvents()
    assert label.palette().color(roles.Text) == qt.scheme.semantic(
        qt.scheme.ERROR, light
    )
    label.set_kind(None)
    assert label.palette().color(roles.Text) == light.color(roles.Text)
    dark_text = qt.QtGui.QPalette(dark)
    qt.app.setPalette(dark_text)
    qt.app.processEvents()
    assert label.palette().color(roles.Text) == dark.color(roles.Text)
