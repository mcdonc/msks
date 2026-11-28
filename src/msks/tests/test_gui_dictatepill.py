"""The dictate pill's suite (#1214): the pill on every top-level
window — the main window, a torn-off shell window, a dialog — one
recording behind them all, the faces each state wears, the
discard button, the corner it holds, and the terminal-only corner
parking clear of it; the settings page's form over the keys'
precedence, the in-place ``msks.yaml`` write (#1224), the model
download with its progress and cancel. Offscreen, over the
controller's recorded seams, with no capture child: the pill's
suite drives the controller through its states by hand."""

import os
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from msks.client import config as client_config
from msks.client import dictate
from msks.client.gui.dictatekeys import DictateSettings, resolve
from msks.client.validate import parse_config_doc

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
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", "")  # no system file
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
    (#1214, #1224: the environment and the merged config document),
    with no host installed — the pill suite adds pills by hand or
    through the host, per test."""
    from msks.client.gui.dictation import Dictation

    fake = Fake()
    held = Dictation(
        resolve(os.environ, operator or {}),
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


def user_file(qt) -> Path:
    """The user's ``msks.yaml`` under the fixture's config dir
    (#1224): the file the settings page writes."""
    return qt.tmp / "config" / "msks.yaml"


def file_doc(qt) -> dict:
    """The user file's validated document, empty while the file is
    the all-comment template or missing."""
    path = user_file(qt)
    if not path.exists():
        return {}
    return parse_config_doc(path.read_text(), str(path))


def launched(qt, doc=None, *, text: str | None = None):
    """A resolved config the way a bare ``msks gui`` launch gets one
    (#1224): *doc* (a dict, or yaml text through *text*) written
    into the user file first when given, then
    :func:`msks.client.config.resolve` — the template generated on
    a missing file, no system file under the fixture."""
    if text is not None:
        user_file(qt).parent.mkdir(parents=True, exist_ok=True)
        user_file(qt).write_text(text)
    elif doc:
        user_file(qt).parent.mkdir(parents=True, exist_ok=True)
        user_file(qt).write_text(yaml.safe_dump(doc))
    return client_config.resolve()


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


def test_the_page_seeds_from_the_effective_settings_and_merges_in_place(
    qt,
):
    """The form reads the effective settings (#1214, #1224) — the
    environment over the user's file over the defaults — Apply
    merges the three keys into the user's ``msks.yaml`` in place
    (every other byte of the file as the operator wrote it), and
    the controller takes the new settings."""
    from msks.client.gui.settingspage import SettingsPage

    operator = {"dictate_model": "base.en", "dictate_device": "mic-a"}
    conf = launched(
        qt,
        text="# my config\n" + yaml.safe_dump(operator),
    )
    held, _fake = controller(qt, conf.doc)
    assert held.settings.model == "base.en"  # the user's file
    assert held.settings.device == "mic-a"
    page = SettingsPage(held, conf)
    assert page.model.currentText() == "base.en (142 MB)"  # its label
    assert page.model_value() == "base.en"
    assert page.device_value() == "mic-a"
    # Edit, apply: the file carries the three keys, its comment and
    # the other keys as they were, and the controller re-resolves.
    page.pick_model("small")  # a listed name: its label shows
    assert page.model.currentText() == "small (466 MB)"
    page.device.setEditText("mic-c")
    page.prompt.setText("msks, devenv")
    page.apply()
    assert page.status.text() == "saved to msks.yaml"
    assert file_doc(qt) == {
        "dictate_model": "small",
        "dictate_device": "mic-c",
        "dictate_prompt": "msks, devenv",
    }
    assert user_file(qt).read_text().startswith("# my config\n")
    assert held.settings.model == "small"
    assert held.settings.device == "mic-c"
    assert held.settings.prompt == "msks, devenv"
    assert conf.config_path == str(user_file(qt))


def test_apply_without_a_config_file_applies_for_the_session(qt):
    """A launch under ``--config=none`` (#1224): no file to write,
    so Apply applies the fields for the session and says so."""
    from msks.client.gui.settingspage import SESSION_ONLY, SettingsPage

    held, _fake = controller(qt)
    conf = client_config.resolve(config="none")
    assert conf.config_path == "none" and conf.config_paths == []
    page = SettingsPage(held, conf)
    page.device.setEditText("mic-session")
    page.apply()
    assert page.status.text() == SESSION_ONLY
    assert held.settings.device == "mic-session"
    assert not user_file(qt).exists()  # nothing was written
    # Revert reads no file either: the fields reseed from the
    # session's standing settings.
    page.device.setEditText("mic-other")
    page.revert()
    assert page.device_value() == "mic-session"
    assert held.settings.device == "mic-session"
    # Clearing a field clears it for the session too (#1224 review):
    # a blank deletes the key the way the file path does.
    page.device.setEditText("")
    page.apply()
    assert held.settings.device == ""
    assert page.status.text() == SESSION_ONLY


def test_a_pinned_key_reads_read_only_and_apply_leaves_it(qt, monkeypatch):
    """A key the environment pins (#1214): its field shows the
    variable's value, stands read-only with the variable named,
    and Apply leaves it out of the file — the variable wins it
    anyway. A blank variable is the unset form, not a pin."""
    from msks.client.gui.settingspage import SettingsPage

    monkeypatch.setenv("MSKSC_DICTATE_MODEL", "large-v3-turbo")
    monkeypatch.setenv("MSKSC_DICTATE_DEVICE", "  ")  # blank: unset
    held, _fake = controller(qt)
    assert held.settings.model == "large-v3-turbo"
    assert held.state == "nomodel"  # the pinned name is not on disk
    assert held.settings.pinned == frozenset({"dictate_model"})
    page = SettingsPage(held, launched(qt))
    assert page.model.isEnabled() is False
    assert page.model.toolTip().endswith(
        "MSKSC_DICTATE_MODEL pins this setting; the page cannot change it."
    )
    assert page.model.toolTip().startswith("The whisper.cpp speech model")
    assert page.device.isEnabled() is True
    assert "MSKSC_DICTATE_MODEL pins this setting" in page.pinned_note.text()
    page.device.setEditText("mic-z")
    page.apply()
    assert "dictate_model" not in file_doc(qt)
    assert file_doc(qt)["dictate_device"] == "mic-z"
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
    page = SettingsPage(held, launched(qt))
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
    assert file_doc(qt)["dictate_model"] == "small"
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
    # A failed download reads the file not at all: the failure
    # stands alone, the fields and the controller as they were.
    broken_file = user_file(qt)
    broken_file.write_text("dictate_model: [")
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert page.status.text() == "download failed: reset"
    assert held.state == "nomodel"
    assert broken_file.read_text() == "dictate_model: ["
    broken_file.unlink()

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
    page = SettingsPage(held, launched(qt))

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
    page = SettingsPage(held, launched(qt))
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
    assert file_doc(qt)["dictate_model"] == "base.en"
    # A save that cannot land leaves the fields unsaved: lit.
    page.device.setEditText("mic-b")
    (qt.tmp / "config").chmod(0o500)
    try:
        page.apply()
    finally:
        (qt.tmp / "config").chmod(0o700)
    assert page.status.text().startswith("cannot save msks.yaml: ")
    assert page.apply_button.isEnabled() and page.dirty
    # The file stands as the last good save left it.
    assert file_doc(qt) == {
        "dictate_model": "base.en",
        "dictate_prompt": "msks",
    }
    # A revert after the failed save: dark, the file's values back.
    page.revert()
    assert not page.apply_button.isEnabled()
    assert page.device_value() == ""


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
    page = SettingsPage(held, launched(qt))
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
    held.reload(resolve(os.environ, {}))
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
    page = SettingsPage(held, launched(qt))
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
    page = SettingsPage(held, launched(qt))
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
    page = SettingsPage(held, launched(qt), sources=lambda: list(listed))
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
    """``MSKSC_DICTATE_*`` > the merged ``msks.yaml`` document > the
    defaults (#1214, #1224), each value through the reader and a
    blank at a layer the unset form. A bad value never reaches the
    resolver: the loader refuses the file at load, with the path
    in the refusal."""
    environ = {
        "MSKSC_DICTATE_PROMPT": "  msks  ",
        "MSKSC_DICTATE_DEVICE": "",
    }
    operator = {"dictate_model": "base.en", "dictate_device": "  mic-file  "}
    settings = resolve(environ, operator)
    assert settings.prompt == "msks"
    assert (
        settings.device == "mic-file"
    )  # the blank variable is unset, stripped
    assert settings.model == "base.en"
    assert settings.pinned == frozenset({"dictate_prompt"})
    assert settings.value("dictate_prompt") == "msks"
    assert settings.value("dictate_model") == "base.en"
    # Defaults alone.
    bare = resolve({}, {})
    assert bare == DictateSettings()
    assert bare.model_file(Path("/cache")) == Path(
        "/cache/whisper/ggml-small.bin"
    )
    # A non-string in the file is the loader's refusal, not a
    # problem the page shows: the client refuses at bootstrap.
    with pytest.raises(
        ValueError,
        match=(
            r"msks\.yaml: config key 'dictate_prompt' must be a string, "
            r"got int"
        ),
    ):
        parse_config_doc("dictate_prompt: 7\n", "msks.yaml")
    # A blank file value is the unset form, and the default stands.
    blank = resolve({}, {"dictate_device": ""})
    assert blank.device == ""


def test_read_value_strips_and_reads_blank_as_unset(qt):
    """The reader (#1224): a stripped string, None for None and for
    a blank — both layers carry strings by construction."""
    from msks.client.gui.dictatekeys import read_value

    assert read_value(None) is None
    assert read_value("  ") is None
    assert read_value(" small ") == "small"


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
    raised = []
    pill.raise_ = lambda: raised.append(True)  # the method, shadowed
    before = pill.pos()
    pill.anchor()  # no window: no move, no raise
    assert pill.pos() == before  # no move
    assert raised == []  # no raise


def test_a_non_string_preferences_key_is_refused(qt):
    """A key that is not a string is the loader's refusal (#1224
    moved the keys into ``msks.yaml``, whose loader carries the
    rule), with the path in the message."""
    with pytest.raises(ValueError, match="keys must be strings"):
        parse_config_doc("1: small\n", "msks.yaml")


def test_apply_and_revert_flash_a_file_that_will_not_read(qt, monkeypatch):
    """The page's reads of the file (#1214, #1224): a user file
    broken outside the three keys will not parse, and Revert
    flashes the reason; a save over it cannot produce a file the
    loader takes, and Apply and a download's landing flash on
    Apply's line, the broken file left as it is — the fields
    standing as they were."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, launched(qt))
    broken = user_file(qt)
    broken.write_text("url: [\n")
    page.revert()
    assert "invalid YAML" in page.status.text()
    assert str(broken) in page.status.text()
    # A download's landing over a file that will not load: the
    # merge lands its key, the self-check refuses the whole, and
    # the broken file is left as it is.
    page.downloaded.emit("small", "", "")
    assert page.status.text().startswith("cannot save msks.yaml: ")
    assert broken.read_text() == "url: [\n"
    assert held.settings.model == "small"  # the standing settings
    page.device.setEditText("mic-x")
    page.apply()
    assert page.status.text().startswith("cannot save msks.yaml: ")
    assert held.settings.model == "small"
    assert page.apply_button.isEnabled()  # the edit stands unsaved


def test_download_asks_once_and_the_busy_bar_runs(qt):
    """The download button (#1214) asks nothing for a path or
    while one flies; the bar runs busy when the server names no
    total."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(
        qt, {"dictate_model": "/nix/store/x-ggml-small.bin"}
    )
    page = SettingsPage(held, launched(qt))
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


def test_apply_flashes_a_layers_read_that_refuses(qt, monkeypatch):
    """Apply's own re-read (#1224): a save that lands whole and a
    re-read that then refuses says both things — the file was
    written, the re-read refused — and the controller keeps its
    standing settings."""
    from msks.client.gui import settingspage as page_mod
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, launched(qt))

    def refuses(_conf):
        raise ValueError("msks.yaml: invalid YAML: bad")

    monkeypatch.setattr(page_mod, "reload_doc", refuses)
    page.model.setEditText("base.en")
    page.apply()
    assert page.status.text() == (
        "saved to msks.yaml, but the re-read refused: "
        "msks.yaml: invalid YAML: bad"
    )
    assert held.settings.model == "small"  # the standing settings
    assert file_doc(qt)["dictate_model"] == "base.en"  # the write landed


def test_a_failed_download_leaves_the_pick_for_a_retry(qt):
    """A failed or cancelled download (#1216 review): the pick
    stands in the picker, so a retry fetches the same model, and
    nothing is saved; the model line reads the pick still."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    (qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin").unlink()
    held.check_model()
    page = SettingsPage(held, launched(qt))
    page.pick_model("large-v3-turbo")

    def broken(name, cache_root, progress=None, cancel=None):
        raise dictate.DictateFailure("download failed: reset")

    page.downloader = broken
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert page.status.text() == "download failed: reset"
    assert page.model_value() == "large-v3-turbo"  # the pick stands
    assert "large-v3-turbo.bin — missing" in page.model_state.text()
    assert page.download_button.isEnabled()  # a retry is offered
    assert file_doc(qt) == {}


def test_a_landed_download_saves_the_model_alone(qt):
    """A landed download (#1216 review) saves the picked model and
    nothing else: the fields the operator edited without applying
    stay as they left them, applied or not, and still ask for
    Apply."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    (qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin").unlink()
    held.check_model()
    page = SettingsPage(held, launched(qt))
    page.device.setEditText("mic-unapplied")  # never applied
    page.prompt.setText("typed mid-flight")

    def fake_download(name, cache_root, progress=None, cancel=None):
        path = cache_root / "whisper" / "ggml-small.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ggml")
        return path

    page.downloader = fake_download
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert page.status.text() == "saved ggml-small.bin"
    assert file_doc(qt) == {"dictate_model": "small"}  # the model alone
    assert held.settings.model == "small"
    assert held.settings.device == ""  # the unsaved edit saved nothing
    assert page.device_value() == "mic-unapplied"  # the field stands
    assert page.apply_button.isEnabled()  # and still asks for Apply
    page.apply()
    assert file_doc(qt)["dictate_device"] == "mic-unapplied"
    assert not page.apply_button.isEnabled()


def test_a_hand_typed_label_resolves_to_its_model(qt):
    """A listed item's label typed by hand (#1216 review) resolves
    to that item's model name, not to the label text."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, launched(qt))
    page.model.setEditText("small (466 MB)")
    assert page.model_value() == "small"
    page.model.setEditText("small")
    assert page.model_value() == "small"
    page.model.setEditText("/nix/store/x.bin")
    assert page.model_value() == "/nix/store/x.bin"


def one_pill(window, *seated) -> bool:
    """The invariant: of the window's floating pill and the seated
    pills, exactly one shows to the window."""
    from msks.client.gui.dictatepill import pill_in

    showing = [p for p in (pill_in(window), *seated) if p.isVisibleTo(window)]
    return len(showing) == 1


def test_one_pill_holds_through_every_page_round_trip(qt):
    """The invariant (#1216 review): the floating pill stands down
    while *any* seated pill shows, whatever order a page change
    hides one page and shows the next — the everyday path, list →
    workspace → Back, once showed two pills; and the long tour
    through every page and terminal-only shows exactly one
    throughout, the floating one serving terminal-only alone."""
    from msks.client.gui.dictatepill import PillHost, pill_in
    from msks.client.gui.shells import ShellHub
    from msks.client.gui.tabs import MainTabs
    from msks.client.gui.workspace import WorkspaceShell
    from PySide6 import QtWidgets
    from test_gui_workspace import StubBridge

    held, _fake = controller(qt)
    host = PillHost(held, qt.app)
    hub = ShellHub(argv_factory=lambda ws: [sys.executable, "-c", SLEEPER])
    qt.hubs.append(hub)
    shell = WorkspaceShell(StubBridge(), hub=hub, dictation=held)
    tabs = MainTabs(shell, None)
    for page in (
        QtWidgets.QWidget(),
        QtWidgets.QWidget(),
        QtWidgets.QWidget(),
    ):
        tabs.addTab(page, f"page{tabs.count()}")
    window = QtWidgets.QMainWindow()
    window.setCentralWidget(tabs)
    window.resize(900, 600)
    window.show()
    spin(qt, 50)
    floating = pill_in(window)
    listing = shell.listing
    detail = shell.detail

    def showing():
        return [
            name
            for name, pill in (
                ("floating", floating),
                ("list", listing.pill),
                ("detail", detail.pill),
            )
            if pill.isVisibleTo(window)
        ]

    assert showing() == ["list"]
    # Open a workspace, go Back: one pill each step.
    shell.open_detail("ws-a")
    spin(qt, 50)
    assert showing() == ["detail"]
    shell.show_listing()
    spin(qt, 50)
    assert showing() == ["list"]
    # The tour: Images, Secrets, Settings, terminal-only, Back, list.
    for index in (1, 2, 3):
        tabs.setCurrentIndex(index)
        spin(qt, 50)
        assert floating.isVisibleTo(window) and not listing.pill.isVisibleTo(
            window
        )
    tabs.setCurrentIndex(0)
    spin(qt, 50)
    assert showing() == ["list"]
    shell.open_detail("ws-a")
    spin(qt, 50)
    detail.terminal_only.toggle.click()
    spin(qt, 150)
    assert showing() == ["floating"]
    assert floating.x() > window.width() // 2  # at the tab row's right end
    shell.show_listing()
    spin(qt, 50)
    assert showing() == ["list"]
    # Back and forth once more: the set holds, nothing dangles.
    shell.open_detail("ws-a")
    shell.show_listing()
    spin(qt, 50)
    assert showing() == ["list"]
    # A re-dial tears the tree down: the dead pages' pills drop from
    # the floating pill's set, and the floating one serves alone.
    window.setCentralWidget(QtWidgets.QWidget())
    window.close()
    QtWidgets.QApplication.sendPostedEvents(
        None, qt.QtCore.QEvent.Type.DeferredDelete
    )
    spin(qt, 50)
    assert len(floating.seated) == 0  # the dead pages' pills forgot themselves
    host.remove()


def test_a_landed_download_saves_the_model_that_was_fetched(qt):
    """A pick changed mid-flight (#1216 review): the landing saves
    the model the worker fetched, not whatever the picker holds by
    then — the file, the controller, and the disk agree, and the
    flash names the fetched file."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    (qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin").unlink()
    held.check_model()
    page = SettingsPage(held, launched(qt))
    picked: list = []

    def fake_download(name, cache_root, progress=None, cancel=None):
        page.pick_model("large-v3-turbo")  # the operator moves on
        picked.append(name)
        path = cache_root / "whisper" / f"ggml-{name}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ggml")
        return path

    page.downloader = fake_download
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert picked == ["small"]
    assert file_doc(qt) == {"dictate_model": "small"}
    assert held.settings.model == "small" and held.state == "idle"
    assert page.status.text() == "saved ggml-small.bin"
    # The picker keeps the operator's later pick, and it differs
    # from the file: Apply asks.
    assert page.model_value() == "large-v3-turbo"
    assert page.apply_button.isEnabled()


def test_a_clean_landing_settles_apply(qt):
    """A landed download with no other edit (#1216 review): the
    fields match the file again, and Apply goes dark — the pick
    that started the download was the save."""
    from msks.client.gui.settingspage import SettingsPage

    held, _fake = controller(qt)
    page = SettingsPage(held, launched(qt))

    def fake_download(name, cache_root, progress=None, cancel=None):
        path = cache_root / "whisper" / f"ggml-{name}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ggml")
        return path

    page.downloader = fake_download
    # The pick is the only edit: it lights Apply, and the download of
    # that same model is its save.
    page.pick_model("large-v3-turbo")
    assert page.apply_button.isEnabled()
    page.download()
    page.flight.join(5)
    spin(qt, 100)
    assert file_doc(qt) == {"dictate_model": "large-v3-turbo"}
    assert not page.apply_button.isEnabled()
    assert held.settings.model == "large-v3-turbo"


def test_a_landed_download_under_a_pinned_model_keeps_the_pin(qt, monkeypatch):
    """A model the environment pins (#1216 review): the Download
    button stands dark — the pin would override the file anyway —
    and a landing that arrives regardless writes the pin's value,
    not the fetched name, saying so."""
    from msks.client.gui.settingspage import SettingsPage

    monkeypatch.setenv("MSKSC_DICTATE_MODEL", "small")
    conf = launched(
        qt, {"dictate_device": "mic-a", "dictate_model": "base.en"}
    )
    held, _fake = controller(qt, conf.doc)
    (qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin").unlink()
    held.check_model()
    page = SettingsPage(held, conf)
    assert "missing" in page.model_state.text()
    assert page.download_button.isEnabled() is False  # the pin decides
    assert page.save_model("large-v3-turbo") is False
    # The file stands as the operator left it: the pin wins at
    # resolve without a write, and the page says so.
    assert file_doc(qt) == {
        "dictate_device": "mic-a",
        "dictate_model": "base.en",
    }
    assert held.settings.model == "small"
    assert "MSKSC_DICTATE_MODEL pins the model" in page.status.text()


def test_the_floating_pill_destroyed_first_raises_nothing(qt):
    """The teardown order a re-dial makes (#1216 delta review): the
    floating pill can die before the seated pills — a re-dial's
    tree lands after it, so the window's deletion destroys it
    first, and a hand ``deleteLater`` reaches the same order. The
    seated pills' ``destroyed`` then reach a connection that died
    with the floating pill (a bound slot is the receiver's own),
    never a dead wrapper. Nothing raises."""
    from msks.client.gui.dictatepill import PillHost, pill_in
    from msks.client.gui.workspaces import WorkspaceList
    from PySide6 import QtWidgets
    from test_gui_workspaces import StubBridge

    held, _fake = controller(qt)
    host = PillHost(held, qt.app)
    errors: list = []
    before = sys.excepthook
    sys.excepthook = lambda t, v, _tb: errors.append(f"{t.__name__}: {v}")
    try:
        window = QtWidgets.QMainWindow()
        window.setAttribute(qt.QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        listing = WorkspaceList(StubBridge(), held)
        window.setCentralWidget(listing)
        window.show()
        spin(qt, 50)
        floating = pill_in(window)
        assert listing.pill in floating.seated
        # The floating pill goes first.
        floating.deleteLater()
        spin(qt, 20)
        QtWidgets.QApplication.sendPostedEvents(
            None, qt.QtCore.QEvent.Type.DeferredDelete
        )
        # Then the pages: their pills' destroyed reach nothing dead.
        window.close()
        QtWidgets.QApplication.sendPostedEvents(
            None, qt.QtCore.QEvent.Type.DeferredDelete
        )
        spin(qt, 20)
    finally:
        sys.excepthook = before
        host.remove()
    assert errors == []
