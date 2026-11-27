"""The dictate pill's suite (#1214): the pill on every top-level
window — the main window, a torn-off shell window, a dialog — one
recording behind them all, the faces each state wears, the
discard button, the corner it holds, and the terminal-only corner
parking clear of it; the settings page's form over the keys'
precedence, the ``gui.yaml`` write, the model download with its
progress and cancel. Offscreen, over the controller's recorded
seams, with no capture child: the pill's suite drives the
controller through its states by hand."""

import os
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from msks.client import dictate
from msks.client.gui.dictatekeys import DictateSettings, resolve
from msks.client.gui.guiprefs import (
    dump_prefs,
    load_prefs,
    parse_prefs,
    prefs_path,
    save_prefs,
)

SLEEPER = textwrap.dedent(
    """
    import time
    print("ready", flush=True)
    time.sleep(30)
    """
)

DICTATE_VARS = (
    "MSKSC_DICTATE_MODEL",
    "MSKSC_DICTATE_DEVICE",
    "MSKSC_DICTATE_THRESHOLD",
    "MSKSC_DICTATE_MARGIN",
    "MSKSC_DICTATE_PROMPT",
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)


class Fake:
    """The controller's recorded seams (#1214): no child runs —
    the capture command names a binary that fails to start at
    once, so a click runs the whole arc without a stream; the
    transcriber is never reached; the tone, the notifier, and the
    clipboard record."""

    def __init__(self) -> None:
        self.tones: list[int] = []
        self.notes: list[str] = []
        self.copied: list[str] = []
        self.commands: list[list[str]] = []

    def command(self, device: str) -> list[str]:
        self.commands.append(["pw-record", device])
        return ["msks-no-such-record", "-"]

    def transcribe(self, wav: Path, settings: DictateSettings) -> str:
        raise AssertionError("the pill suite never transcribes")

    def tone(self, hz: int) -> None:
        self.tones.append(hz)

    def notify(self, text: str) -> None:
        self.notes.append(text)

    def clipboard(self, text: str) -> None:
        self.copied.append(text)


@pytest.fixture()
def qt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The offscreen ``QApplication``, the config and cache roots
    pointed at the temporary tree, every dictation variable
    cleared, and a model on disk. The terminal-only test reuses
    the terminal suites' fixture shape (the silent fontconfig and
    the seated faces), set up here for the one test that needs
    them."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("MSKSC_CACHE_DIR", raising=False)
    for key in DICTATE_VARS:
        monkeypatch.delenv(key, raising=False)
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    model = tmp_path / "cache" / "msks" / "whisper" / "ggml-small.bin"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"ggml")
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        tmp=tmp_path,
        hubs=[],
        keep=[],
    )
    for hub in list(getattr(qt, "hubs", [])):
        hub.shutdown()
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def controller(qt, operator=None):
    """A real controller over the recorded seams and a present
    model, its settings resolved the way the window resolves them
    (#1214: the environment, gui.yaml, the operator's file), with
    no host installed — the pill suite adds pills by hand or
    through the host, per test."""
    from msks.client.gui.dictation import Dictation

    fake = Fake()
    held = Dictation(
        resolve(os.environ, load_prefs(), operator or {}),
        qt.tmp / "cache" / "msks",
        command_for=fake.command,
        transcriber=fake.transcribe,
        tone=fake.tone,
        notifier=fake.notify,
        clipboard=fake.clipboard,
    )
    return held, fake


def spin(qt, ms: int = 50) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        qt.app.processEvents()
        time.sleep(0.005)
    qt.app.processEvents()


# --- the pill and the host -----------------------------------------------


def test_every_page_seats_its_pill_and_the_floating_one_serves_terminal_only(
    qt,
):
    """The pill's placings (#1214): every main page seats the pill in
    its own bottom row at the right end — the list's quick facts,
    the detail page's footer, the Images and Secrets counts, the
    Settings status — in the column the connection picker stands
    in; the main window floats one too, standing down while a
    seated pill shows, and the terminal-only page, which hides its
    footer, brings the floating one back at the tab row's corner. A
    page built without a controller seats none. A shell window torn
    off the strip floats one; no dialog, message box, progress box,
    or popup carries one."""
    from msks.client.gui.dictatepill import (
        MARGIN,
        PILL_NAME,
        PillHost,
        pill_in,
    )
    from msks.client.gui.shellwindow import ShellWindow
    from msks.client.gui.workspaces import WorkspaceList
    from PySide6 import QtWidgets
    from test_gui_workspaces import StubBridge

    held, _fake = controller(qt)
    host = PillHost(held, qt.app)
    window = QtWidgets.QMainWindow()
    strip = QtWidgets.QStackedWidget()
    listing = WorkspaceList(StubBridge(), held)
    other = QtWidgets.QWidget()
    strip.addWidget(listing)
    strip.addWidget(other)
    window.setCentralWidget(strip)
    window.resize(600, 400)
    window.show()
    spin(qt)
    floating = pill_in(window)
    assert floating is not None and floating.floating is True
    # The list's pill: in the detail row, under the picker, laid out.
    pill = listing.pill
    assert pill.floating is False and pill.isVisible()
    assert pill.geometry().bottom() <= listing.height()
    assert pill.y() >= listing.detail.y() - 2
    # The picker's and the pill's right edges, both mapped to the
    # page: the same column.
    picker = listing.connection
    picker_right = (
        picker.mapTo(listing, qt.QtCore.QPoint(0, 0)).x() + picker.width()
    )
    pill_right = pill.mapTo(listing, qt.QtCore.QPoint(0, 0)).x() + pill.width()
    assert abs(picker_right - pill_right) <= 1
    # The list shows its own: the floating pill stands down.
    assert not floating.isVisible()
    # Another page without a seated pill: the floating one returns.
    strip.setCurrentIndex(1)
    spin(qt, 50)
    assert floating.isVisible()
    assert floating.x() + floating.width() == 600 - MARGIN
    assert floating.y() == MARGIN
    strip.setCurrentIndex(0)
    spin(qt, 50)
    assert not floating.isVisible()
    # Linked once: a re-show asks the link no more.
    assert pill.linked
    # A page without a controller seats none.
    bare = WorkspaceList(StubBridge())
    assert bare.pill is None
    bare.show()
    # A controller, but under a window the host never equips (a
    # plain top-level widget, not a main window): the seated pill's
    # show finds no floating pill and links nothing.
    unlinked = WorkspaceList(StubBridge(), held)
    plain = QtWidgets.QWidget(None, qt.QtCore.Qt.WindowType.Window)
    plain.setLayout(QtWidgets.QVBoxLayout())
    plain.layout().addWidget(unlinked)
    plain.show()
    spin(qt, 20)
    assert unlinked.pill.isVisible() and not getattr(
        unlinked.pill, "linked", False
    )
    plain.close()
    spin(qt, 20)
    # A seated pill under another window is not this floating
    # pill's to stand down for: the link refuses.
    other_window = QtWidgets.QMainWindow()
    other_list = WorkspaceList(StubBridge(), held)
    other_window.setCentralWidget(other_list)
    other_window.show()
    spin(qt, 20)
    assert floating.stands_down_for(other_list.pill) is False
    other_window.close()
    # A torn-off shell window floats one at the corner.
    torn = ShellWindow(None, "ws")
    torn.resize(500, 300)
    torn.show()
    spin(qt)
    shell_pill = pill_in(torn)
    assert shell_pill is not None and shell_pill.floating is True
    assert shell_pill.dictation is held
    assert shell_pill.x() + shell_pill.width() == 500 - MARGIN
    # Dialogs, boxes, and popups carry none, whatever their parent.
    dialog = QtWidgets.QDialog(window)
    dialog.show()
    box = QtWidgets.QMessageBox(window)
    progress = QtWidgets.QProgressDialog("Importing…", None, 0, 100, window)
    progress.show()
    popup = QtWidgets.QFrame(None, qt.QtCore.Qt.WindowType.Popup)
    popup.show()
    spin(qt)
    for bare_window in (dialog, box, progress, popup):
        assert pill_in(bare_window) is None
    assert len(window.findChildren(QtWidgets.QFrame, PILL_NAME)) == 2
    host.remove()


def test_the_pill_wears_each_state(qt):
    """The faces (#1214): idle's bare mic in the chrome's text,
    recording's red mic with a timer and the discard button,
    transcribing's amber mic with an ellipsis, done's ``copied``,
    failed's reason, and nomodel's download glyph — one width
    through them all."""
    from msks.client.gui.dictatepill import DictationPill
    from msks.client.gui.dictation import (
        DONE,
        FAILED,
        IDLE,
        NOMODEL,
        RECORDING,
        TRANSCRIBING,
    )
    from msks.client.gui.kinds import ERROR, WARN
    from PySide6 import QtWidgets

    held, _fake = controller(qt)
    window = QtWidgets.QMainWindow()
    window.show()
    pill = DictationPill(held, window)
    width = pill.width()

    def face() -> SimpleNamespace:
        return SimpleNamespace(
            text=pill.text.text(),
            glyph=pill.glyph.shape,
            kind=pill.glyph.kind,
            close=pill.discard_button.isHidden(),
            tip=pill.toolTip(),
        )

    idle = face()
    assert idle.glyph == "mic" and idle.kind is None and idle.text == ""
    assert (
        idle.close is True and idle.tip == "Dictate: click to start recording"
    )
    held.show(RECORDING)
    held.elapsed = 72.0
    held.tocked()
    recording = face()
    assert recording.text == "● 1:12"
    assert recording.kind is ERROR and recording.close is False
    assert recording.tip == "Click to finish recording"
    held.show(TRANSCRIBING)
    transcribing = face()
    assert transcribing.text == "…" and transcribing.kind is WARN
    assert transcribing.close is False
    held.show(DONE, "copied")
    assert face().text == "copied" and face().close is True
    held.show(FAILED, "whisper-cli exited 1")
    assert face().text == "whisper-cli exited 1" and face().close is True
    held.show(NOMODEL)
    assert face().glyph == "download" and "settings" in face().tip
    assert pill.width() == width  # one width in every state
    held.show(IDLE)
    assert face().text == ""
    # The words table.
    assert DictationPill.words(RECORDING, 12.9, "") == "● 0:12"
    assert DictationPill.words(RECORDING, 180.0, "") == "● 3:00"
    assert DictationPill.words(FAILED, 0.0, "") == "failed"
    assert DictationPill.words(IDLE, 0.0, "stale") == ""


def test_a_click_on_any_pill_toggles_the_one_recording(qt):
    """The click (#1214): the panel toggles the controller — a
    press and release inside is a click, a release outside is
    none — and the discard button discards. Every pill reads the
    one controller's state."""
    from msks.client.gui.dictatepill import PillHost, pill_in
    from msks.client.gui.shellwindow import ShellWindow
    from PySide6 import QtTest, QtWidgets

    Left = qt.QtCore.Qt.MouseButton.LeftButton
    held, fake = controller(qt)
    host = PillHost(held, qt.app)
    window = QtWidgets.QMainWindow()
    window.show()  # no page seats one here: the floating pill serves
    torn = ShellWindow(None, "ws")
    torn.show()
    spin(qt)
    first, second = pill_in(window), pill_in(torn)
    # The capture fails to start at once under the stand-in
    # command, so a click runs the whole arc: recording, then
    # failed with the binary named — on both pills alike.
    QtTest.QTest.mousePress(first, Left)
    QtTest.QTest.mouseRelease(first, Left)
    spin(qt, 200)
    assert fake.commands == [["pw-record", ""]]
    assert first.text.text() == second.text.text()
    assert "msks-no-such-record not found" in first.text.text()
    held.show("idle")
    # A press that releases outside is no click.
    QtTest.QTest.mousePress(first, Left)
    QtTest.QTest.mouseRelease(
        first, Left, pos=first.rect().bottomRight() + qt.QtCore.QPoint(4, 4)
    )
    assert fake.commands == [["pw-record", ""]]
    # The discard button reaches the controller's discard.
    held.show("recording")
    discards: list = []
    held.discard = lambda: discards.append(True)
    QtTest.QTest.mouseClick(first.discard_button, Left)
    assert discards == [True]
    host.remove()


def test_the_pill_takes_no_focus_and_joins_no_tab_chain(qt):
    """Mouse only (#1214's scope): the pill and its button take no
    focus, so the page's spatial navigation never lands on them."""
    from msks.client.gui.dictatepill import DictationPill
    from PySide6 import QtWidgets

    held, _fake = controller(qt)
    window = QtWidgets.QMainWindow()
    edit = QtWidgets.QLineEdit(window)
    window.setCentralWidget(edit)
    window.show()
    pill = DictationPill(held, window)
    NoFocus = qt.QtCore.Qt.FocusPolicy.NoFocus
    assert pill.focusPolicy() == NoFocus
    assert pill.discard_button.focusPolicy() == NoFocus
    # Qt's focus chain lists every widget; the tab order skips the
    # NoFocus ones, so a Tab never lands on the pill or its button.
    chain = []
    node = edit
    while True:
        chain.append(node)
        node = node.nextInFocusChain()
        if node is edit or len(chain) > 50:
            break
    TabFocus = qt.QtCore.Qt.FocusPolicy.TabFocus
    stops = [w for w in chain if w.focusPolicy() & TabFocus]
    assert pill not in stops and pill.discard_button not in stops
    assert edit in stops


def test_the_terminal_only_corner_parks_clear_of_the_pill(qt):
    """The terminal-only mode's corner (#1152, #1214): with a pill
    on the window, the corner's row — the workspace's name, the
    status seat, the restore button — ends left of the pill, and
    the page keeps the mode's own margins so the terminal runs to
    the window's edge; the pill never forms a column of its own.
    Without a pill the corner takes the row's whole width."""
    from msks.client.gui import workspace as workspace_mod
    from msks.client.gui.dictatepill import DictationPill, pill_in
    from msks.client.gui.terminal import fonts as faces
    from PySide6 import QtWidgets
    from test_gui_terminalonly import StubBridge, enter

    faces.register_faces()
    qt.page_mod = workspace_mod
    held, _fake = controller(qt)
    hub = workspace_mod.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    qt.hubs.append(hub)
    page = workspace_mod.WorkspaceDetail(StubBridge(), hub)
    window = QtWidgets.QMainWindow()
    window.setCentralWidget(page)
    window.resize(1200, 600)
    window.show()
    qt.keep.append(window)
    page.open("ws-a", "alpha")
    qt.app.processEvents()
    enter(page)
    spin(qt, 100)  # the mode's margins land a tick after the enter
    # The pill stands, as the window path stands it before any page;
    # the mode's re-park then clears the corner of it.
    pill = DictationPill(held, window)
    spin(qt, 100)  # the strip lays the corner out around the pill
    page.terminal_only.park_restore()
    spin(qt, 50)
    assert pill_in(window) is pill
    mode = page.terminal_only
    assert mode.restore.width() > 0
    landed = mode.restore.mapTo(window, qt.QtCore.QPoint(0, 0))
    assert landed.x() + mode.restore.width() <= pill.x()
    assert (
        mode.name.mapTo(window, qt.QtCore.QPoint(0, 0)).x()
        + (mode.name.width())
        <= pill.x()
    )
    # The page's right margin is the mode's own, whatever the pill:
    # the terminal still runs to the window's right edge, and no
    # column forms beside it.
    assert page.layout().contentsMargins().right() == 0
    strip_right = page.shells.mapTo(window, qt.QtCore.QPoint(0, 0)).x() + (
        page.shells.width()
    )
    assert strip_right == window.width()
    # Leaving the mode: the corner's margin goes.
    mode.leave()
    qt.app.processEvents()
    assert mode.corner_row.contentsMargins().right() == 0


# --- the settings page ----------------------------------------------------


def test_the_page_seeds_from_the_effective_settings_and_saves_gui_yaml(qt):
    """The form reads the effective settings (#1214) — gui.yaml
    over the operator's file over the defaults — Apply writes
    the fields into ``gui.yaml`` and never the operator's file,
    and the controller takes the new settings."""
    from msks.client.gui.settingspage import SettingsPage

    operator = {"dictate_model": "base.en", "dictate_device": "mic-a"}
    save_prefs({"dictate_device": "mic-b"})
    held, _fake = controller(qt, operator)
    assert held.settings.model == "base.en"  # the operator's file
    assert held.settings.device == "mic-b"  # gui.yaml wins over it
    page = SettingsPage(held, operator)
    assert page.model.currentText() == "base.en (142 MB)"  # its label
    assert page.model_value() == "base.en"
    assert page.device_value() == "mic-b"  # gui.yaml's, over the file's
    # Edit, apply: gui.yaml carries the fields, the operator's
    # dict stands as it was, the controller re-resolves.
    page.pick_model("small")  # a listed name: its label shows
    assert page.model.currentText() == "small (466 MB)"
    page.device.setEditText("mic-c")
    page.prompt.setText("msks, devenv")
    page.apply()
    assert page.status.text() == "saved to gui.yaml"
    saved = load_prefs()
    assert saved == {
        "dictate_model": "small",
        "dictate_device": "mic-c",
        "dictate_prompt": "msks, devenv",
    }
    assert operator == {"dictate_model": "base.en", "dictate_device": "mic-a"}
    assert held.settings.model == "small"
    assert held.settings.device == "mic-c"
    assert held.settings.prompt == "msks, devenv"
    assert prefs_path() == qt.tmp / "config" / "gui.yaml"


def test_a_pinned_key_reads_read_only_and_apply_leaves_it(qt, monkeypatch):
    """A key the environment pins (#1214): its field shows the
    variable's value, stands read-only with the variable named,
    and Apply leaves it out of gui.yaml — the variable wins it
    anyway. A blank variable is the unset form, not a pin."""
    from msks.client.gui.settingspage import SettingsPage

    monkeypatch.setenv("MSKSC_DICTATE_MODEL", "large-v3-turbo")
    monkeypatch.setenv("MSKSC_DICTATE_DEVICE", "  ")  # blank: unset
    held, _fake = controller(qt)
    assert held.settings.model == "large-v3-turbo"
    assert held.state == "nomodel"  # the pinned name is not on disk
    assert held.settings.pinned == frozenset({"dictate_model"})
    assert held.settings.problems == ()
    page = SettingsPage(held, {})
    assert page.model.isEnabled() is False
    assert page.model.toolTip().endswith(
        "MSKSC_DICTATE_MODEL pins this setting; the page cannot change it."
    )
    assert page.model.toolTip().startswith("The whisper.cpp speech model")
    assert page.device.isEnabled() is True
    assert "MSKSC_DICTATE_MODEL pins this setting" in page.pinned_note.text()
    page.device.setEditText("mic-z")
    page.apply()
    assert "dictate_model" not in load_prefs()
    assert load_prefs()["dictate_device"] == "mic-z"
    assert held.settings.model == "large-v3-turbo"
    # A pinned device: read-only, the variable named on both the
    # field and its label.
    monkeypatch.setenv("MSKSC_DICTATE_DEVICE", "mic-env")
    page.revert()
    assert held.settings.device == "mic-env"
    assert page.device.isEnabled() is False
    notice = "MSKSC_DICTATE_DEVICE pins this setting"
    assert page.device.toolTip().endswith(
        notice + "; the page cannot change it."
    )
    assert page.labels["dictate_device"].toolTip() == page.device.toolTip()
    assert notice in page.pinned_note.text()


def test_the_model_download_shows_progress_and_lands(qt):
    """The download (#1214): the bar follows the chunks, the
    landing re-reads the model's presence for the page and the
    controller, and a cancel or a failure flashes its reason."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    model = qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin"
    model.unlink()
    held.check_model()
    assert held.state == "nomodel"
    page = SettingsPage(held, {})
    assert page.download_button.isEnabled()
    assert "missing" in page.model_state.text()
    landed: list = []

    def fake_download(name, cache_root, progress=None, cancel=None):
        assert name == "small"
        for done in (25, 50, 75, 100):
            progress(done, 100)
        path = cache_root / "whisper" / "ggml-small.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ggml")
        landed.append(path)
        return path

    page.downloader = fake_download
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert landed and page.bar.isHidden()
    assert page.status.text() == "saved ggml-small.bin"
    assert "present" in page.model_state.text()
    assert page.download_button.isEnabled() is False
    assert held.state == "idle"
    # The download saved the field's values as Apply would: the
    # model on disk is now the configured one (#1216 review).
    assert load_prefs()["dictate_model"] == "small"
    assert held.settings.model == "small"

    # A failure flashes its reason and the page stands on nomodel.
    model.unlink()
    held.check_model()
    page.refresh_model()

    def broken(name, cache_root, progress=None, cancel=None):
        raise dictate.DictateFailure("download failed: reset")

    page.downloader = broken
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert page.status.text() == "download failed: reset"
    assert page.bar.isHidden() and page.cancel_button.isHidden()
    assert held.state == "nomodel"
    # A failed download over a gui.yaml that will not read: the
    # layers' refusal flashes in the failure's place, the
    # controller keeping its standing settings.
    broken = prefs_path()
    broken.write_text("dictate_model: [")
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert "invalid YAML" in page.status.text()
    assert held.state == "nomodel"
    broken.unlink()

    # Cancel: the flag the worker reads; the landing hides the bar.
    def stalled(name, cache_root, progress=None, cancel=None):
        while not cancel():
            time.sleep(0.01)
        raise dictate.DictateFailure("download cancelled")

    page.downloader = stalled
    page.download()
    spin(qt, 50)
    # The page is not shown, so ``isVisible`` reads False for the
    # bar; the widget's own hidden flag is the pin.
    assert not page.bar.isHidden() and not page.cancel_button.isHidden()
    page.abandon_download()
    page.flight.join(5)
    spin(qt, 100)
    assert page.status.text() == "download cancelled"
    assert page.bar.isHidden()


def test_a_download_thread_that_raises_anything_lands(qt):
    """The worker's catch (#1216 review): a downloader raising
    anything at all lands as a failure — the bar goes, Cancel
    hides, the button comes back — never a dead thread with the
    page's download wedged."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    (qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin").unlink()
    held.check_model()
    page = SettingsPage(held, {})

    def explode(name, cache_root, progress=None, cancel=None):
        raise RuntimeError("socket gone")

    page.downloader = explode
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert page.status.text() == "RuntimeError: socket gone"
    assert page.flight is None
    assert page.bar.isHidden() and page.cancel_button.isHidden()
    assert page.download_button.isEnabled()


def test_apply_and_revert_light_only_while_a_field_differs(qt):
    """The dirty flag (#1214): Apply and Revert stand dark over saved
    fields, light at the first change to any field, and darken
    again at a save or a revert — a failed save leaves them lit,
    since the fields still differ from the file."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, {})
    assert not page.apply_button.isEnabled()
    assert not page.reload_button.isEnabled()
    page.device.setEditText("mic-a")  # a typed edit
    assert page.dirty and page.apply_button.isEnabled()
    page.revert()
    assert page.dirty is False and not page.apply_button.isEnabled()
    page.pick_model("base.en")  # a pick
    assert page.apply_button.isEnabled()
    page.prompt.setText("msks")  # another field
    page.apply()
    assert not page.apply_button.isEnabled()
    assert load_prefs()["dictate_model"] == "base.en"
    # A save that cannot land leaves the fields unsaved: lit.
    page.device.setEditText("mic-b")
    (qt.tmp / "config" / "gui.yaml").unlink()
    (qt.tmp / "config").chmod(0o500)
    try:
        page.apply()
    finally:
        (qt.tmp / "config").chmod(0o700)
    assert page.apply_button.isEnabled() and page.dirty
    # A download's landing seeds the fields: dark.
    page.reload_layers()
    assert not page.apply_button.isEnabled()


def test_every_field_carries_its_description_and_the_pin_notice(
    qt, monkeypatch
):
    """Each field's tooltip (#1214) is the setting's long
    description, its label carries the same, and a key the
    environment pins appends its variable's notice to both."""
    from msks.client.gui import settingspage as page_mod
    from msks.client.gui.dictatekeys import resolve
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, {})
    assert sorted(page.fields) == [
        "dictate_device",
        "dictate_model",
        "dictate_prompt",
    ]
    for key, field in page.fields.items():
        assert field.toolTip() == page_mod.TIPS[key]
        assert len(page_mod.TIPS[key]) > 80  # a description, not a label
        assert page.labels[key].toolTip() == page_mod.TIPS[key]
    assert "PipeWire" in page.device.toolTip()
    assert "~/.cache/msks/whisper/" in page.model.toolTip()
    assert page.labels["dictate_device"].text() == "Device"
    assert page.refresh_sources.toolTip() == page_mod.REFRESH_TIP
    # A pinned key: the variable's notice follows on both.
    monkeypatch.setenv("MSKSC_DICTATE_DEVICE", "mic-env")
    held.reload(resolve(os.environ, load_prefs(), {}))
    page.load(held.settings)
    notice = "MSKSC_DICTATE_DEVICE pins this setting"
    assert page.device.toolTip().endswith(
        notice + "; the page cannot change it."
    )
    assert page.labels["dictate_device"].toolTip() == page.device.toolTip()


def test_the_picker_drives_the_model_line_live(qt):
    """The model picker (#1214): a pick changes the model line and
    the Download button at once, before anything is saved — the
    line reads the picked model's file, and the button answers to
    whether it stands."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)  # small on disk, the setting
    page = SettingsPage(held, {})
    assert "present" in page.model_state.text()
    assert page.download_button.isEnabled() is False
    page.model.setCurrentIndex(page.model.findData("large-v3-turbo"))
    assert (
        "ggml-large-v3-turbo.bin — missing (1.5 GB)" in page.model_state.text()
    )
    assert page.download_button.isEnabled() is True
    # A typed path: nothing to download, the line says where it points.
    page.model.setEditText("/nix/store/x-ggml-small.bin")
    assert "a path; nothing to download" in page.model_state.text()
    assert page.download_button.isEnabled() is False
    page.model.setEditText("small")
    assert "present" in page.model_state.text()
    assert held.settings.model == "small"  # nothing saved by a pick


def test_a_path_model_downloads_nothing(qt):
    """A model setting that points at a path (#1214): a NixOS
    store path downloads nothing, the page says where it points,
    and the button stands disabled."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(
        qt, {"dictate_model": "/nix/store/x-ggml-small.bin"}
    )
    assert held.state == "nomodel"  # the path names nothing on disk
    page = SettingsPage(held, {})
    assert page.model_name() is None
    assert "a path; nothing to download" in page.model_state.text()
    assert page.download_button.isEnabled() is False


def test_the_device_list_comes_from_pw_dump(qt):
    """The device picker (#1214): the listed sources beside the
    default, the standing pick kept, and a missing tool noted."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(
        qt, {"dictate_device": "alsa_input.usb-scarlett.mic1"}
    )
    listed = [
        ("alsa_input.usb-scarlett.mic1", "Scarlett Mic1"),
        ("alsa_input.usb-scarlett.mic2", "Scarlett Mic2"),
    ]
    page = SettingsPage(held, {}, sources=lambda: list(listed))
    page.list_devices()
    assert page.status.text() == "2 input(s) found"
    assert [page.device.itemText(i) for i in range(1, 3)] == [
        "Scarlett Mic1  ·  alsa_input.usb-scarlett.mic1",
        "Scarlett Mic2  ·  alsa_input.usb-scarlett.mic2",
    ]
    assert page.values()["dictate_device"] == "alsa_input.usb-scarlett.mic1"
    page.device.setCurrentIndex(2)
    assert page.values()["dictate_device"] == "alsa_input.usb-scarlett.mic2"
    page.device.setCurrentIndex(0)
    assert page.values()["dictate_device"] == ""
    page.device.setEditText("serial-64")
    assert page.values()["dictate_device"] == "serial-64"

    def no_tool():
        raise dictate.DictateFailure("pw-dump not found")

    page.sources = no_tool
    page.list_devices()
    assert page.status.text() == "pw-dump not found"


# --- the keys and the file -----------------------------------------------


def test_the_keys_resolve_under_the_precedence(qt):
    """``MSKSC_DICTATE_*`` > gui.yaml > the operator's file > the
    defaults (#1214), each value through the reader, a blank at a
    layer the unset form, and a refusal recording a problem with
    the key defaulting."""
    environ = {
        "MSKSC_DICTATE_PROMPT": "  msks  ",
        "MSKSC_DICTATE_DEVICE": "",
    }
    prefs = {"dictate_device": "mic-prefs", "dictate_prompt": "from prefs"}
    operator = {"dictate_model": "base.en", "dictate_device": "mic-file"}
    settings = resolve(environ, prefs, operator)
    assert settings.prompt == "msks"
    assert settings.device == "mic-prefs"  # the blank variable is unset
    assert settings.model == "base.en"
    assert settings.pinned == frozenset({"dictate_prompt"})
    assert settings.problems == ()
    assert settings.value("dictate_prompt") == "msks"
    assert settings.value("dictate_model") == "base.en"
    # Defaults alone.
    bare = resolve({}, {}, {})
    assert bare == DictateSettings()
    assert bare.model_file(Path("/cache")) == Path(
        "/cache/whisper/ggml-small.bin"
    )
    # A refusal at the top layer: the problem names the variable.
    bad = resolve({"MSKSC_DICTATE_PROMPT": 7}, {}, {})
    assert bad.prompt == ""
    assert bad.problems == (
        (
            "dictate_prompt",
            "MSKSC_DICTATE_PROMPT: config key 'dictate_prompt' must be "
            "a string, got int",
        ),
    )
    # A refusal lower down names the file, and the layer under it
    # does not fill in behind the bad pin.
    worse = resolve({}, {"dictate_device": ["x"]}, {"dictate_device": "y"})
    assert worse.device == ""
    assert worse.problems[0][1].startswith("gui.yaml: config key")


def test_the_preferences_file_round_trips(qt):
    """``gui.yaml`` (#1214): both spellings one key, duplicates
    and unknown keys refused, the dump in key order with the
    unset forms dropped, and the atomic write over a standing
    file."""
    doc = parse_prefs(
        "dictate-model: small\ndictate_device: mic-a\n", "gui.yaml"
    )
    assert doc == {"dictate_model": "small", "dictate_device": "mic-a"}
    assert parse_prefs("# nothing\n", "gui.yaml") == {}
    assert parse_prefs("dictate_device:\n", "gui.yaml") == {}
    # The gate's numbers are not preferences keys (#1214).
    for key in ("dictate_silence", "dictate_threshold", "dictate_margin"):
        with pytest.raises(ValueError, match="unknown preferences key"):
            parse_prefs(f"{key}: 1.5\n", "gui.yaml")
    for text, message in (
        ("dictate_model: a\ndictate-model: b\n", "duplicate"),
        ("url: https://x\n", "unknown preferences key 'url'"),
        ("- a\n- b\n", "must be a mapping"),
        ("dictate_model: [a]\n", "must be a string"),
    ):
        with pytest.raises(ValueError, match=message):
            parse_prefs(text, "gui.yaml")
    assert (
        dump_prefs(
            {
                "dictate_device": "mic-a",
                "dictate_model": "small",
                "dictate_prompt": None,
            }
        )
        == "dictate_model: small\ndictate_device: mic-a\n"
    )
    assert load_prefs() == {}  # nothing saved yet
    save_prefs({"dictate_model": "small", "dictate_prompt": "msks"})
    assert load_prefs() == {"dictate_model": "small", "dictate_prompt": "msks"}
    save_prefs({"dictate_model": "base.en"})
    assert load_prefs() == {"dictate_model": "base.en"}  # whole, not merged
    assert [p.name for p in prefs_path().parent.iterdir()] == ["gui.yaml"]


def test_an_unreadable_preferences_file_names_the_path(qt, monkeypatch):
    target = prefs_path()
    target.parent.mkdir(parents=True)
    target.write_text("dictate_model: [")
    with pytest.raises(ValueError, match="gui.yaml: invalid YAML"):
        load_prefs()
    target.chmod(0o000)
    if os.getuid() == 0:  # root reads anything
        target.unlink()
        pytest.skip("root reads a mode-000 file")
    with pytest.raises(ValueError, match="cannot read preferences file"):
        load_prefs()


# --- the branches the first pass left dark -------------------------------


def test_the_glyph_paints_both_shapes(qt, monkeypatch):
    """The glyph's two shapes (#1214) paint through the palette:
    the mic's capsule, arc, and stem lines, and the download
    arrow's four lines."""
    from msks.client.gui.dictatepill import DOWNLOAD, MIC, Glyph
    from msks.client.gui.kinds import ERROR
    from PySide6 import QtGui

    drawn: list = []
    monkeypatch.setattr(
        QtGui.QPainter,
        "drawLine",
        lambda self, *args: drawn.append("line"),
    )
    monkeypatch.setattr(
        QtGui.QPainter,
        "drawRoundedRect",
        lambda self, *args: drawn.append("capsule"),
    )
    monkeypatch.setattr(
        QtGui.QPainter, "drawArc", lambda self, *args: drawn.append("arc")
    )
    glyph = Glyph()
    glyph.shape = MIC
    glyph.kind = ERROR
    pixmap = QtGui.QPixmap(glyph.size())
    pixmap.fill(QtGui.QColor("transparent"))
    glyph.render(pixmap)
    assert "capsule" in drawn and "arc" in drawn
    assert drawn.count("line") == 2
    drawn.clear()
    glyph.shape = DOWNLOAD
    glyph.render(pixmap)
    assert drawn == ["line"] * 4  # the stem, two head strokes, the base


def test_an_anchor_with_no_window_moves_nothing(qt):
    """A pill whose parent is gone (#1214) anchors nowhere."""
    from msks.client.gui.dictatepill import DictationPill
    from PySide6 import QtWidgets

    held, _fake = controller(qt)
    window = QtWidgets.QMainWindow()
    pill = DictationPill(held, window)
    pill.setParent(None)
    pill.anchor()  # no window: no move, no raise


def test_a_non_string_preferences_key_is_refused(qt):
    with pytest.raises(ValueError, match="keys must be strings"):
        parse_prefs("1: small\n", "gui.yaml")


def test_apply_and_revert_flash_a_file_that_will_not_read(qt, monkeypatch):
    """The page's reads of the layers (#1214): a gui.yaml that
    will not parse flashes the reason on Revert and on a
    download's landing, and a tree that will not write flashes
    on Apply — the fields standing as they were."""
    from msks.client.gui import settingspage as page_mod
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, {})
    broken = prefs_path()
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("dictate_model: [")
    page.revert()
    assert "gui.yaml: invalid YAML" in page.status.text()
    # A download's landing over a file that will not read: Apply's
    # save replaces the broken file whole, then the read stands.
    page.downloaded.emit("", "")
    assert page.status.text() == "saved "
    # A tree the atomic write cannot land in: Apply's save fails
    # before any read, and the page keeps the standing settings.
    held.settings = DictateSettings(model="small")
    page.load(held.settings)
    monkeypatch.setattr(page_mod, "load_prefs", lambda: {})

    def refuse(path: Path) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(page_mod, "save_prefs", refuse)
    page.apply()
    assert page.status.text() == "cannot save gui.yaml: read-only"
    assert held.settings.model == "small"


def test_download_asks_once_and_the_busy_bar_runs(qt):
    """The download button (#1214) asks nothing for a path or
    while one flies; the bar runs busy when the server names no
    total."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(
        qt, {"dictate_model": "/nix/store/x-ggml-small.bin"}
    )
    page = SettingsPage(held, {})
    page.download()
    assert page.flight is None  # a path: nothing to download
    page.progress(50, 0)
    assert page.bar.minimum() == 0 and page.bar.maximum() == 0
    page.progress(50, 100)
    assert page.bar.value() == 50
    page.abandon_download()  # no flight: the flag stands unset
    assert not page.cancel.is_set()
    # A second ask while one flies is refused.
    started = threading.Event()

    def stalled(name, cache_root, progress=None, cancel=None):
        started.set()
        while not cancel():
            time.sleep(0.01)
        raise dictate.DictateFailure("download cancelled")

    page.downloader = stalled
    page.pick_model("small")
    page.refresh_model()
    (qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin").unlink()
    page.refresh_model()
    page.download()
    assert started.wait(5)
    page.download()  # a flight stands: refused
    page.abandon_download()
    page.flight.join(5)
    spin(qt, 50)


def test_read_value_refuses_a_non_string(qt):
    from msks.client.gui.dictatekeys import read_value

    with pytest.raises(ValueError, match="must be a string, got list"):
        read_value("dictate_model", ["small"], "gui.yaml")
    assert read_value("dictate_model", None, "gui.yaml") is None
    assert read_value("dictate_model", "  ", "gui.yaml") is None


def test_apply_flashes_a_layers_read_that_refuses(qt, monkeypatch):
    """Apply's own re-read (#1214): a save that lands whole and a
    layers read that then refuses — the file changed under the
    page — flashes the reason and the controller keeps its
    standing settings."""
    from msks.client.gui import settingspage as page_mod
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, {})

    def refuses():
        raise ValueError("gui.yaml: invalid YAML: bad")

    monkeypatch.setattr(page_mod, "resolve", lambda *a: refuses())
    page.model.setEditText("base.en")
    page.apply()
    assert page.status.text() == "gui.yaml: invalid YAML: bad"
    assert held.settings.model == "small"
