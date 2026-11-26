"""The create-workspace dialog (#962): the TUI create form's
counterpart — the plain-body arithmetic, the hint reads, the
submit and refusal paths — driven over a stub bridge, the
screens' own fixture shape. The widgets run under the offscreen
platform on every host.
"""

import importlib.util
import os
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.create import (  # noqa: E402
    CREATE_KEY,
    DEFAULTS_KEY,
    FORM_FIELDS,
    IMAGES_KEY,
    STATIC_DEFAULTS,
    CreateDialog,
    create_body,
    image_options,
    size_defaults,
    size_placeholders,
    summary_text,
)


class FakeSignal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once, in the GUI thread
    the real signal's queued delivery would land them in."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def disconnect(self, slot) -> None:
        self.slots.remove(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class StubBridge:
    """The bridge's surface for the dialog's tests: the signals
    it reads and the one verb it calls, every call recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.flights: list[Future] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> Future:
        self.calls.append((key, method, args))
        self.flights.append(Future())
        return self.flights[-1]


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the dialog needs, the
    screens' own fixture shape (#939)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(app=app, QtWidgets=QtWidgets, keep=[])
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


def make_dialog(qt) -> tuple:
    """The dialog over the stub bridge, shown — its hint reads
    issued — in the suite's own window-less shape: a dialog is a
    top-level of its own."""
    bridge = StubBridge()
    dialog = CreateDialog(bridge)
    dialog.show()
    qt.keep.append(dialog)
    return dialog, bridge


def fill(dialog, values: dict) -> None:
    """The fields' text set, the image picker's item picked by
    its data value."""
    for field, _label in FORM_FIELDS:
        widget = dialog.fields[field]
        if hasattr(widget, "setCurrentIndex"):
            for index in range(widget.count()):
                if widget.itemData(index) == values.get(field):
                    widget.setCurrentIndex(index)
        elif field in values:
            widget.setText(values[field])


def failure(message: str, *, reach: bool = True) -> object:
    """A refused call's shape: the failure class the bridge
    carries, built the suite's stub way."""
    from msks.client.data import RestFailure

    return RestFailure(message, reach=reach)


def test_blank_fields_stay_the_daemons_defaults() -> None:
    """The body's arithmetic (#962, #309's own): blank fields
    stay unset — the daemon's defaults answer — whole numbers
    land as integers, and a body without a name refuses
    locally."""
    body, error = create_body({"name": "alpha", "cpus": " 2 "})
    assert body == {"name": "alpha", "cpus": 2}
    assert error == ""
    body, error = create_body({"cpus": "2"})
    assert body is None
    assert error == "a workspace name is required"
    body, error = create_body({"name": "a", "root_mib": "half"})
    assert body is None
    assert (
        error
        == "root_mib: sizes read as MiB, or with a G or M suffix: 8G, 512M"
    )
    assert create_body({}) == (None, "a workspace name is required")


@pytest.mark.parametrize(
    "typed, mib",
    [
        ("8192", 8192),
        ("08192", 8192),
        ("8G", 8192),
        ("8GiB", 8192),
        ("8g", 8192),
        ("8gib", 8192),
        ("8192M", 8192),
        ("8192MiB", 8192),
        ("0.5G", 512),
        ("1.5G", 1536),
        ("0.25GiB", 256),
        ("1.5625G", 1600),
        ("1.0G", 1024),
        ("2.0GiB", 2048),
        ("111111111111111111.5G", 111111111111111111 * 1024 + 512),
        ("0", 0),
    ],
)
def test_the_size_forms_read(typed: str, mib: int) -> None:
    """The size forms (#1164): a whole number of MiB as the
    TUI's form reads it, the number with a G/GiB or M/MiB
    suffix in either case, and a decimal before the unit that
    resolves to a whole number of MiB — all landing as the
    body's MiB count, with the surrounding whitespace the
    field's text may carry."""
    body, error = create_body({"name": "a", "mem_mib": f" {typed} "})
    assert (body, error) == ({"name": "a", "mem_mib": mib}, "")


@pytest.mark.parametrize(
    "typed",
    [
        "8GB",
        "8GiBx",
        "eight",
        "-1",
        "+8192",
        "3.0",
        "1.5",
        "0.3G",
        "1.G",
        ".5G",
        "8 G",
        "8MB",
        "1" * 19,
        "1" * 19 + "G",
        "\u00b2",
        "\u0663",
        "4_096",
    ],
)
def test_the_size_forms_refuse(typed: str) -> None:
    """The refused size forms (#1164): `GB` and `MB` name
    decimal bytes, not the daemon's MiB count; a decimal needs
    its unit and its leading digit, and one whose product is
    not a whole number of MiB refuses rather than rounding; and
    every hazard the whole-number rule already refused refuses
    here too — unicode digits, a sign, an inner space."""
    body, error = create_body({"name": "a", "mem_mib": typed})
    assert body is None, typed
    assert (
        error
        == "mem_mib: sizes read as MiB, or with a G or M suffix: 8G, 512M"
    )


def test_only_ascii_digits_are_whole_numbers() -> None:
    """The whole-number rule (#962, the TUI's own hazard):
    unicode digits a paste can carry refuse locally — ``int``
    would crash on some of them and the daemon would take the
    rest as numbers the TUI's own form refuses."""
    for pasted in ("\u00b2", "\u0663", "+3", "-3", "3.0"):
        body, error = create_body({"name": "a", "cpus": pasted})
        assert body is None, pasted
        assert error == "cpus: a whole number, or leave it blank"


def test_the_image_options_mark_the_default() -> None:
    """The picker's options (#962, the TUI select's own
    arithmetic): distro ascending, newest import first within
    it, the designated default's asterisk on its label alone,
    and a reference two builds share riding the hash in every
    value but the daemon's own pick."""
    rows = [
        {
            "name": "msks",
            "version": "41",
            "hash": "h-old",
            "imported": "2026-01-01T00:00:00",
        },
        {
            "name": "msks",
            "version": "41",
            "hash": "h-new",
            "imported": "2026-02-01T00:00:00",
            "default": True,
        },
        {
            "name": "alt",
            "version": "9",
            "hash": "h-alt",
            "imported": "2026-03-01T00:00:00",
        },
    ]
    assert image_options(rows) == [
        ("alt:9", "alt:9"),
        # newest first within the group; the daemon's own
        # name:version resolution picks the oldest import
        # (h-old), so the newer build rides the hash
        ("msks@h-new", "msks:41 *"),
        ("msks:41", "msks:41"),
    ]
    assert image_options(None) == []


def test_a_name_the_charset_refuses_leaves_the_placeholder_empty(
    monkeypatch,
) -> None:
    """The placeholder's own refusal (#962): a login name the
    charset refuses — the CLI create's own SystemExit — leaves
    the field's hint empty instead of taking the window down;
    the create read itself surfaces the refusal as a failure."""
    from msks.client.gui import create as create_mod

    def refused() -> str:
        raise SystemExit("msks: your username")

    monkeypatch.setattr(create_mod, "invoking_user", refused)
    assert create_mod.user_placeholder() == ""


def test_the_size_placeholders_read_the_defaults() -> None:
    """The placeholders (#962, #1164): the default a blank
    field lands on, in the unit the field reads (`default 4G`);
    a defaults answer without the field, or without a whole
    number for it, leaves the field its standing hint."""
    assert size_placeholders({"root_mib": 4096}) == {"root_mib": "default 4G"}
    assert size_placeholders({"root_mib": 512, "home_mib": 1536}) == {
        "root_mib": "default 512M",
        "home_mib": "default 1.5G",
    }
    # A default the GiB spelling would round reads its MiB
    # count, the same rule as the summary line beside it.
    assert size_placeholders({"root_mib": 20000, "home_mib": 1075}) == {
        "root_mib": "default 20000M",
        "home_mib": "default 1075M",
    }
    assert size_placeholders({"root_mib": "4096"}) == {}
    assert size_placeholders({}) == {}
    assert size_placeholders(None) == {}
    assert size_defaults({"root_mib": 4096, "cpus": 2}) == {"root_mib": 4096}
    assert size_defaults({"root_mib": 4096.0}) == {}


def test_the_summary_restates_the_sizes() -> None:
    """The summary line (#1164): the four sizes in the form's
    order, a size in GiB (MiB under one GiB), the defaults
    filling the blanks and a dash standing until the daemon's
    answer names a default. A typed value reads as it lands
    (`0.5G` is `512 MiB`), and a value mid-typing that does not
    read yet shows as typed, so the line follows the keys."""
    static = dict(STATIC_DEFAULTS)
    assert summary_text({}, static) == ("4 vCPU · 8 GiB RAM · — root · — home")
    answered = {**static, "root_mib": 4096, "home_mib": 8192}
    assert summary_text({}, answered) == (
        "4 vCPU · 8 GiB RAM · 4 GiB root · 8 GiB home"
    )
    assert (
        summary_text(
            {
                "cpus": " 2 ",
                "mem_mib": "0.5G",
                "root_mib": "1536M",
                "home_mib": "8192",
            },
            answered,
        )
        == "2 vCPU · 512 MiB RAM · 1.5 GiB root · 8 GiB home"
    )
    assert summary_text({"cpus": "many", "root_mib": "0."}, answered) == (
        "many vCPU · 8 GiB RAM · 0. root · 8 GiB home"
    )
    assert summary_text({"mem_mib": "0"}, answered).split(" · ")[1] == (
        "0 MiB RAM"
    )
    # A size the GiB spelling would round shows its MiB count:
    # the line confirms what was typed, never a MiB off.
    assert summary_text({"mem_mib": "8193"}, answered).split(" · ")[1] == (
        "8193 MiB RAM"
    )
    assert summary_text({"mem_mib": "1075"}, answered).split(" · ")[1] == (
        "1075 MiB RAM"
    )


def test_the_dialog_loads_its_hints_on_show(qt) -> None:
    """The hint reads (#962, #1164): the first show asks for the
    image catalog and the create defaults; the catalog fills the
    picker behind its default stand-in, the defaults the size
    placeholders and the summary's blanks. An untouched picker
    stays on the stand-in — the create body carries no image,
    and the daemon answers its designated default."""
    dialog, bridge = make_dialog(qt)
    assert bridge.calls == [
        (IMAGES_KEY, "images", ()),
        (DEFAULTS_KEY, "create_defaults", ()),
    ]
    picker = dialog.fields["image"]
    assert picker.itemText(0) == "the daemon's default"
    assert picker.currentData() is None
    bridge.succeeded.emit(
        IMAGES_KEY,
        [
            {"name": "msks", "version": "41", "hash": "h1", "default": True},
        ],
    )
    assert picker.count() == 2
    assert picker.itemText(1) == "msks:41 *"
    assert picker.currentIndex() == 0
    # Before the defaults answer: the static defaults fill what
    # they name, and the root and home wait on the answer.
    assert dialog.fields["cpus"].placeholderText() == "default 4"
    assert dialog.fields["mem_mib"].placeholderText() == "default 8G"
    assert dialog.fields["root_mib"].placeholderText() == ""
    assert dialog.summary.text() == "4 vCPU · 8 GiB RAM · — root · — home"
    bridge.succeeded.emit(DEFAULTS_KEY, {"root_mib": 4096})
    assert dialog.fields["root_mib"].placeholderText() == "default 4G"
    assert dialog.summary.text() == "4 vCPU · 8 GiB RAM · 4 GiB root · — home"
    # A second show loads the hints once only: the reads the
    # first show issued stand.
    reads = list(bridge.calls)
    dialog.hide()
    dialog.show()
    assert bridge.calls == reads
    # An untouched picker submits no image: the daemon answers
    # its designated default.
    fill(dialog, {"name": "alpha"})
    dialog.submit()
    assert dialog.bridge.calls[-1] == (
        CREATE_KEY,
        "create",
        ({"name": "alpha"},),
    )


def test_the_summary_follows_every_keystroke(qt) -> None:
    """The live summary (#1164): the fields carry the names
    people think in, the line under them restates each edit as
    it is typed — a decimal entry in its resolved size, a size
    the GiB spelling would round in its MiB count — and a
    defaults answer landing late fills the blanks that waited.
    The line reads in the placeholder's muted gray, apart from
    the note line's own job."""
    dialog, bridge = make_dialog(qt)
    assert [label for _field, label in FORM_FIELDS][3:6] == [
        "Memory",
        "Root disk",
        "Home disk",
    ]
    from PySide6 import QtGui

    assert (
        dialog.summary.foregroundRole()
        == QtGui.QPalette.ColorRole.PlaceholderText
    )
    memory = dialog.fields["mem_mib"]
    for typed, shown in [
        ("", "8 GiB RAM"),
        ("0", "0 MiB RAM"),
        ("0.", "0. RAM"),
        ("0.5", "0.5 RAM"),
        ("0.5G", "512 MiB RAM"),
        ("0.5Gi", "0.5Gi RAM"),
        ("0.5GiB", "512 MiB RAM"),
        ("8193", "8193 MiB RAM"),
        ("1075", "1075 MiB RAM"),
        ("1540", "1540 MiB RAM"),
        ("8GB", "8GB RAM"),
        ("8G", "8 GiB RAM"),
    ]:
        memory.setText(typed)
        assert dialog.summary.text().split(" · ")[1] == shown, typed
    # A late defaults answer fills the root and home blanks
    # around whatever the operator typed.
    memory.setText("8G")
    dialog.fields["cpus"].setText("many")
    assert dialog.summary.text().startswith("many vCPU · ")
    dialog.fields["home_mib"].setText("2G")
    bridge.succeeded.emit(DEFAULTS_KEY, {"root_mib": 20480, "home_mib": 20480})
    assert dialog.summary.text() == (
        "many vCPU · 8 GiB RAM · 20 GiB root · 2 GiB home"
    )
    # A refused defaults read leaves the dashes standing and
    # the note carries the failure.
    second, _bridge2 = make_dialog(qt)
    _bridge2.failed.emit(DEFAULTS_KEY, failure("no defaults"))
    assert "defaults failed" in second.note.text()
    assert second.summary.text().endswith("— root · — home")


def test_a_submit_posts_the_body_and_a_landed_create_closes(
    qt,
) -> None:
    """The submit path (#962, #1164): a whole body posts through
    the bridge — a size in any of its forms as its MiB count —
    with the create button held; the daemon's answer closes the
    dialog."""
    dialog, bridge = make_dialog(qt)
    fill(
        dialog,
        {"name": "alpha", "mem_mib": "4G", "root_mib": "0.5G"},
    )
    dialog.submit()
    assert bridge.calls[-1] == (
        CREATE_KEY,
        "create",
        ({"name": "alpha", "mem_mib": 4096, "root_mib": 512},),
    )
    assert not dialog.create_button.isEnabled()
    closed: list[bool] = []
    dialog.accepted.connect(lambda: closed.append(True))
    bridge.succeeded.emit(CREATE_KEY, {"id": "ws-a"})
    assert closed == [True]


def test_a_local_refusal_sends_nothing(qt) -> None:
    """A local refusal (#962, #1164): the note carries it and the
    bridge hears no call — the daemon's validation stays the
    authority, but its surface sees only whole bodies. A vcpu
    count keeps the whole-number note; a size names its forms."""
    dialog, bridge = make_dialog(qt)
    bridge.calls.clear()
    fill(dialog, {"name": "alpha", "cpus": "many"})
    dialog.submit()
    assert dialog.note.text() == "cpus: a whole number, or leave it blank"
    assert bridge.calls == []
    fill(dialog, {"cpus": "", "mem_mib": "8GB"})
    dialog.submit()
    assert dialog.note.text() == (
        "mem_mib: sizes read as MiB, or with a G or M suffix: 8G, 512M"
    )
    assert bridge.calls == []
    # The summary line stays the typed text; the refusal is the
    # note line's to carry.
    assert dialog.summary.text().split(" · ")[1] == "8GB RAM"


def test_another_keys_answer_is_not_the_dialogs_to_take(qt) -> None:
    """The bridge is shared (#962): a foreign key's answer and
    a foreign key's failure change nothing — the screens that
    own those keys take them."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"name": "alpha"})
    note = dialog.note.text()
    bridge.succeeded.emit("workspaces-list", [])
    bridge.failed.emit("detail-health", failure("not this dialog's"))
    assert dialog.note.text() == note
    assert dialog.create_button.isEnabled()


def test_a_dialog_closed_mid_flight_cancels_its_own_call(qt) -> None:
    """The closed dialog hears nothing (#962): a cancel while
    the create is in flight cancels the call through its
    handle, and the late answer — had it raced the cancel —
    reaches no dialog: the bridge's hands are off, and the
    next dialog opened takes only its own answers."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"name": "alpha"})
    dialog.submit()
    flight = dialog.flight
    assert flight is not None
    dialog.reject()
    assert flight.cancelled()
    # A late answer on the shared key reaches the closed
    # dialog's unhooked slots: nothing.
    second, _bridge2 = make_dialog(qt)
    landed: list[object] = []
    second.accepted.connect(lambda: landed.append(True))
    bridge.succeeded.emit(CREATE_KEY, {"id": "ws-a"})
    assert landed == []  # the closed dialog's answer is no one's
    """A refused create (#962): the failure lands on the note
    line, the create button returns, and the dialog stands —
    the operator keeps the fields they filled."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"name": "alpha"})
    dialog.submit()
    bridge.failed.emit(CREATE_KEY, failure("name taken"))
    assert dialog.note.text() == "name taken"
    assert dialog.create_button.isEnabled()


def test_a_refused_hint_notes_it_and_the_form_stands(qt) -> None:
    """A refused hint (#962): the note carries the failure and
    the fields keep their static placeholders — a blank image
    still creates against the daemon's default."""
    dialog, bridge = make_dialog(qt)
    bridge.failed.emit(IMAGES_KEY, failure("the daemon is down"))
    assert dialog.note.text() == "image list failed: the daemon is down"
    bridge.failed.emit(DEFAULTS_KEY, failure("no defaults"))
    assert "defaults failed" in dialog.note.text()
