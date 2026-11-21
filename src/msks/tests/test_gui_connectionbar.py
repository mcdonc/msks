"""The connection strip's suite (#1009, phase 5 of #924): the
standing connection's row and the swappable content it fronts —
offscreen widget tests on every host, over the presenter's own
plain preset rows. The strip itself holds no connection state;
the presenter's suite covers the presets and the picks' env
writes."""

import importlib.util
import os
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.connection import (  # noqa: E402
    ALIAS,
    DEV,
    ENVIRONMENT,
    ConnectionPreset,
)

SLEEPER = textwrap.dedent(
    """
    import time
    print("ready", flush=True)
    time.sleep(30)
    """
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the screens need, with the
    strip's module imported inside (the window path's
    contract, #927)."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import connectionbar as bar_mod
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        bar=bar_mod,
    )
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def presets() -> list[ConnectionPreset]:
    """Three plain preset rows: an alias, the dev daemon, and
    the environment, the dev one standing."""
    return [
        ConnectionPreset("farm", ALIAS, "https://10.0.0.9:9660"),
        ConnectionPreset(
            "dev daemon", DEV, "https://127.0.0.1:9661", active=True
        ),
        ConnectionPreset("environment", ENVIRONMENT, "the shell's exports"),
    ]


def test_the_strip_lists_the_presets_and_names_the_standing_one(
    qt,
) -> None:
    """The menu from the presenter's rows (#1009): one action
    per preset, the name beside the address, the standing one
    checked, and the button's label naming it."""
    strip = qt.bar.ConnectionStrip()
    strip.host(presets())
    assert strip.button.text() == "dev daemon"
    menu = strip.button.menu()
    names = [(action.text(), action.isChecked()) for action in menu.actions()]
    assert names == [
        ("farm — https://10.0.0.9:9660", False),
        ("dev daemon — https://127.0.0.1:9661", True),
        ("environment — the shell's exports", False),
    ]
    # A re-host over a fresh pick marks the new standing row and
    # re-labels the button.
    fresh = [
        ConnectionPreset("farm", ALIAS, "https://10.0.0.9:9660", active=True),
        ConnectionPreset("dev daemon", DEV, "https://127.0.0.1:9661"),
    ]
    strip.host(fresh)
    assert strip.button.text() == "farm"
    assert [a.isChecked() for a in strip.button.menu().actions()] == [
        True,
        False,
    ]


def test_no_standing_preset_labels_the_connection_plainly(qt) -> None:
    """Nothing marked (#1009): the label names the dial plainly
    and the menu still offers every preset."""
    strip = qt.bar.ConnectionStrip()
    assert strip.button.text() == "connection"
    strip.host(presets()[1:])
    strip.host([presets()[0]])
    assert strip.button.text() == "connection"
    assert len(strip.button.menu().actions()) == 1


def test_a_menu_pick_crosses_as_the_signal(qt) -> None:
    """The pick's forward (#1009): a menu action's trigger emits
    the preset it was built from, the widget holding no
    connection state of its own."""
    strip = qt.bar.ConnectionStrip()
    strip.host(presets())
    landed: list[ConnectionPreset] = []
    strip.picked.connect(landed.append)
    action = strip.button.menu().actions()[0]
    action.trigger()
    assert landed == [presets()[0]]


def test_the_content_swaps_whole(qt) -> None:
    """The content below the strip (#1009): a later tree takes
    the slot and the earlier one leaves with deleteLater — its
    bridge, hub, and terminals gone with the connection it
    belonged to."""
    strip = qt.bar.ConnectionStrip()
    # A non-widget item planted in the body (#1009): a stretch
    # the loop must clear the same way, never a widget to
    # delete.
    strip.body.addStretch(1)
    first = qt.QtWidgets.QLabel("first")
    strip.setContent(first)
    second = qt.QtWidgets.QLabel("second")
    strip.setContent(second)
    qt.app.processEvents()
    qt.QtCore.QCoreApplication.sendPostedEvents(
        None, qt.QtCore.QEvent.Type.DeferredDelete
    )
    qt.app.processEvents()
    # The earlier tree's C++ object went with its deleteLater,
    # and the fresh tree holds the slot.
    assert sip_deleted(first)
    item = strip.body.itemAt(0)
    assert item is not None and item.widget() is second


def sip_deleted(widget) -> bool:
    """Whether the widget's C++ object went with its
    deleteLater — the swap's own contract, pinned through
    PySide6's wrapper check."""
    import shiboken6

    return not shiboken6.isValid(widget)
