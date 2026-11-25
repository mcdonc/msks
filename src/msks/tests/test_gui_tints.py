"""The state tints' suite (#1160): the three semantic colors, one
set per desktop scheme, each passing the contrast floor on every
base it paints on; the kind read off the palette in hand; the
delegate that tints an unselected, enabled cell and leaves the
highlight and the dimmed text alone; and the tinted label that
follows an application palette switch and takes its color again
— offscreen, the same ``QApplication`` seam the pairing's suite
uses."""

import os
from types import SimpleNamespace

import pytest


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` with the tints module
    imported inside (the window path's contract, #927) and the
    handed palette restored at teardown."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import scheme as scheme_mod
    from msks.client.gui import tints as tints_mod
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
        tints=tints_mod,
    )
    app.setPalette(handed)
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def test_every_tint_reads_on_its_bases(qt):
    """The three tints (#1160) pass 4.5:1 on their scheme's base
    and alternate base alike: the floor text reads by, the one
    a color-blind reader and a grayscale screenshot keep."""
    floor = 4.5
    for palette, tints in (
        (qt.scheme.dark_palette(), qt.tints._DARK_SEMANTIC),
        (qt.scheme.light_palette(), qt.tints._LIGHT_SEMANTIC),
    ):
        roles = qt.QtGui.QPalette.ColorRole
        # The window the tinted labels sit on, beside the table
        # bases the delegate paints on.
        for base_role in (roles.Base, roles.AlternateBase, roles.Window):
            base = palette.color(base_role)
            for kind, values in tints.items():
                ratio = qt.tints.contrast(qt.QtGui.QColor(*values), base)
                assert ratio >= floor, (kind, base_role, ratio)


def test_the_tints_read_the_scheme_off_the_base(qt):
    """A kind's color comes from the base the palette paints on
    (#1160): the dark set on the dark chrome, the light set on
    the light one, None for no kind at all."""
    dark = qt.scheme.dark_palette()
    light = qt.scheme.light_palette()
    assert qt.tints.is_dark(dark) and not qt.tints.is_dark(light)

    def hexed(kind, tints):
        red, green, blue = tints[kind]
        return f"#{red:02x}{green:02x}{blue:02x}"

    for kind in (qt.tints.OK, qt.tints.WARN, qt.tints.ERROR):
        assert qt.tints.semantic(kind, dark).name() == hexed(
            kind, qt.tints._DARK_SEMANTIC
        )
        assert qt.tints.semantic(kind, light).name() == hexed(
            kind, qt.tints._LIGHT_SEMANTIC
        )
    assert qt.tints.semantic(None, dark) is None
    assert qt.tints.semantic("else", light) is None


def test_the_delegate_tints_an_unselected_cell_alone(qt):
    """The delegate (#1160) writes the kind's color into the
    option's text roles for an untagged-by-selection cell, and
    leaves a selected cell's palette alone: the highlight's own
    text keeps its contrast. An untagged cell paints as the
    style would."""
    table = qt.QtWidgets.QTableWidget(2, 1)
    delegate = qt.tints.SemanticDelegate(table)
    table.setItemDelegate(delegate)
    failed = qt.QtWidgets.QTableWidgetItem("failed (bind refused)")
    failed.setData(qt.tints.SEMANTIC_ROLE, qt.tints.ERROR)
    plain = qt.QtWidgets.QTableWidgetItem("stopped")
    table.setItem(0, 0, failed)
    table.setItem(1, 0, plain)
    State = qt.QtWidgets.QStyle.StateFlag
    roles = qt.QtGui.QPalette.ColorRole
    base = qt.QtGui.QPalette(qt.scheme.dark_palette())
    error = qt.tints.semantic(qt.tints.ERROR, base)

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
    # A disabled cell keeps the style's dimmed text (#1160 review):
    # a state without the enabled flag, and without the selected
    # one, so the exclusion is the disabled path's alone.
    assert text_color(index, State.State_Enabled) == error
    assert text_color(index, State.State_None) == base.color(roles.Text)
    assert text_color(index, State.State_Active) == base.color(roles.Text)


def test_a_tinted_label_follows_the_scheme_switch(qt):
    """A tinted label (#1160) wears its kind on the base it sits
    on, takes the kind's other color when the application
    palette switches, and with no kind keeps following the
    application palette's own text — the resolve mask names the
    text roles alone."""
    roles = qt.QtGui.QPalette.ColorRole
    label = qt.tints.TintedLabel("daemon back")
    label.show()
    qt.app.processEvents()
    label.set_kind(qt.tints.ERROR)
    dark = qt.scheme.dark_palette()
    qt.app.setPalette(dark)
    qt.app.processEvents()
    assert label.palette().color(roles.Text) == qt.tints.semantic(
        qt.tints.ERROR, dark
    )
    assert label.palette().color(roles.WindowText) == label.palette().color(
        roles.Text
    )
    # The base follows the application, not the label's own palette.
    assert label.palette().color(roles.Base) == dark.color(roles.Base)
    light = qt.scheme.light_palette()
    qt.app.setPalette(light)
    qt.app.processEvents()
    assert label.palette().color(roles.Text) == qt.tints.semantic(
        qt.tints.ERROR, light
    )
    label.set_kind(None)
    assert label.palette().color(roles.Text) == light.color(roles.Text)
    dark_text = qt.QtGui.QPalette(dark)
    qt.app.setPalette(dark_text)
    qt.app.processEvents()
    assert label.palette().color(roles.Text) == dark.color(roles.Text)
