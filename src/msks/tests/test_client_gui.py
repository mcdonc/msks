"""GUI tests (#924): the ``msks gui`` command surface (#928), the
window shell, and the GUI dependencies' contract with the base
install (#1023, which reversed #927's extra: the deps the extra
gated are base dependencies now)."""

import ctypes
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from msks.client import cli
from msks.client.gui import app
from test_client_cli import client_env

ROOT = Path(__file__).resolve().parents[3]

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def dep_name(spec: str) -> str:
    """One dependency spec's bare package name, normalized."""
    return re.split(r"[><=~!;\[]", spec)[0].strip().lower()


def test_the_gui_command_hints_without_the_binding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks gui`` on a stripped or partial install (#928): the
    absence guard answers with the reinstall hint — one stderr
    line, the client's error convention. The binding is a base
    dependency (#1023), so its absence means the install itself
    is broken. The find_spec stand-in replaces the absent
    binding in a process that has it."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: None)
    assert cli.main(["gui"]) == 1
    assert app.HINT in capsys.readouterr().err


def test_a_base_install_parses_every_command(
    tmp_path: Path,
) -> None:
    """The base-install contract whole (#1023), pinned for real:
    with the binding unimportable — the state of an install
    whose Qt binding was removed or failed to install — the
    CLI module itself imports and parses. The package init
    once pinned the bridge, whose module scope loads Qt, and
    every command died at parse time; a process with the
    binding installed cannot see that bug, so the child blocks
    the binding the way a stripped install lives."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "sys.modules['PySide6'] = None\n"  # every PySide6 import now raises
        "import msks.client.cli\n"  # the parse-time import chain, whole
        "print('parsed')\n"
    )
    child = subprocess.run(
        [sys.executable, str(probe)], capture_output=True, timeout=30
    )
    assert child.returncode == 0, child.stderr.decode()
    assert child.stdout.decode().strip() == "parsed"


def test_the_gui_command_names_a_headless_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No display and no platform override on Linux: the
    pre-flight answers with one line instead of Qt's abort — the
    qFatal the QApplication constructor would raise is past
    anything Python can catch (exit 134 and a multi-line dump).
    The find_spec stand-in keeps the test parse-safe (a host
    without the binding hears HINT here, not a failure)."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: object())
    # The platform pin runs the env-var arm on every host —
    # darwin's launchd arm is the test below — and the launchd
    # stand-in keeps the gate answering from the probe the test
    # names, not the host's launchd, wherever the pin moves: a
    # CI runner's launchd answers for a gui domain even with no
    # console attached, and the real event loop behind that
    # answer never comes back (#934: it hung three runs running).
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(app, "gui_session_present", lambda: False)
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["gui"]) == 1
    assert app.NO_DISPLAY in capsys.readouterr().err


def test_the_display_gate_skips_macos(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stock macOS desktop session sets none of the three
    variables, and cocoa needs none of them — with a GUI session
    behind it (the launchd probe answers true) the gate stands
    aside and the window path runs. The open_window stand-in
    replaces the real window."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: object())
    monkeypatch.setattr(app, "gui_session_present", lambda: True)
    monkeypatch.setattr(sys, "platform", "darwin")
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(var, raising=False)
    seen: list[str] = []
    monkeypatch.setattr(
        app,
        "open_window",
        lambda QtWidgets, conf=None: seen.append("window") or 4,
    )
    assert cli.main(["gui"]) == 4
    assert seen == ["window"]


def test_the_launchd_probe_reads_headless_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The macOS arm's own probe: launchd's zero exit means the
    GUI session exists; a nonzero exit (no GUI login) and a
    launchd that will not answer both read as headless — the
    notice, not the hang."""
    import subprocess as sp

    class FakeCompleted:
        def __init__(self, code: int) -> None:
            self.returncode = code

    answers = [0, 1]
    asked: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        asked.append(cmd)
        return FakeCompleted(answers.pop(0))

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    assert app.gui_session_present() is True  # launchd said yes
    assert app.gui_session_present() is False  # no GUI session
    assert asked[0][:2] == ["launchctl", "print"]

    def silent(cmd, **kwargs):
        raise sp.TimeoutExpired("launchctl", 5)

    monkeypatch.setattr(app.subprocess, "run", silent)
    assert app.gui_session_present() is False  # no answer: headless


def test_the_gui_command_names_a_broken_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The binding is installed but will not load: the absence
    hint would send an operator who already has the dependency
    in circles, so this arm names the reinstall. The None stub
    makes ``from PySide6 import ...`` raise ImportError in a
    process where the binding loads clean."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "PySide6", None)
    assert cli.main(["gui"]) == 1
    err = capsys.readouterr().err
    assert app.BROKEN in err
    assert "halted" in err  # the caught error's text named the cause


def test_the_scale_knobs_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The presentation knobs (#939): the environment's value
    wins over the default, an unset variable reads the default,
    and a value that does not parse reads the default too — a
    presentation knob never refuses the window."""
    monkeypatch.setenv("MSKS_GUI_TEST_SCALE", "1.25")
    assert app.scale_from_env("MSKS_GUI_TEST_SCALE", 1.5) == 1.25
    monkeypatch.delenv("MSKS_GUI_TEST_SCALE")
    assert app.scale_from_env("MSKS_GUI_TEST_SCALE", 1.5) == 1.5
    for unusable in ("huge", "nan", "inf", "-inf", "1e999", "0", "-1.5"):
        monkeypatch.setenv("MSKS_GUI_TEST_SCALE", unusable)
        assert app.scale_from_env("MSKS_GUI_TEST_SCALE", 1.5) == 1.5, unusable
    # The font knob's name, pinned: a preset value is the
    # operator's own choice (#937's spirit).
    assert app.FONT_SCALE_ENV == "MSKSC_GUI_FONT_SCALE"


def test_the_chrome_anchor_converts_through_the_screen_dpi() -> None:
    """The chrome's pixel anchor (#1056): the point size a
    screen's font dpi answers for it, so the same 15 pixels
    rasterize wherever the session opens — 15pt at macOS's 72,
    11.25pt at the 96 an X11 or Wayland session or the
    offscreen platform answers. A session with no screen at
    all takes the offscreen floor."""

    class Screen:
        def __init__(self, dpi: float) -> None:
            self.dpi = dpi

        def logicalDotsPerInch(self) -> float:
            return self.dpi

    class Session:
        def __init__(self, screen: Screen | None) -> None:
            self.screen = screen

        def primaryScreen(self) -> Screen | None:
            return self.screen

    assert app.screen_dpi(Session(Screen(96.0))) == 96.0
    assert app.screen_dpi(Session(None)) == app.NO_SCREEN_DPI
    assert app.chrome_pt(15, 72.0) == pytest.approx(15.0)  # macOS
    assert app.chrome_pt(15, 96.0) == pytest.approx(11.25)  # Linux
    assert app.chrome_pt(18, 96.0) == pytest.approx(13.5)  # 1.2 knob


def test_the_saved_geometry_reads_only_usable_rects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The saved geometry's read (#939): a rect comes back only
    from a file that parses, names four whole numbers, and has
    area — everything else reads as nothing saved, and the
    window opens on the fixed default."""
    saved = tmp_path / "gui-window.json"
    monkeypatch.setattr(app, "geometry_path", lambda: saved)
    assert app.read_geometry() is None  # nothing written yet
    saved.write_text("not json at all")
    assert app.read_geometry() is None
    saved.write_text(json.dumps({"x": "ten"}))
    assert app.read_geometry() is None
    saved.write_text(json.dumps({"x": 1, "y": 2}))
    assert app.read_geometry() is None
    saved.write_text(json.dumps({"x": 0, "y": 0, "width": 0, "height": 600}))
    assert app.read_geometry() is None
    saved.write_text(
        json.dumps({"x": 30, "y": 40, "width": 900, "height": 620})
    )
    assert app.read_geometry() == (30, 40, 900, 620)


def test_the_geometry_save_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """The geometry save (#939): a state root the window cannot
    write says so on stderr, the client's one-line convention,
    and the close still completes."""

    class ClosedWindow:
        def geometry(self) -> object:
            return self

        def x(self) -> int:
            return 0

        def y(self) -> int:
            return 0

        def width(self) -> int:
            return 640

        def height(self) -> int:
            return 480

    unwritable = tmp_path / "it-is-a-directory"
    unwritable.mkdir()
    monkeypatch.setattr(app, "geometry_path", lambda: unwritable)
    app.save_geometry(ClosedWindow())
    assert "cannot save the window geometry" in capsys.readouterr().err


def test_the_parser_help_lists_gui(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The help names the command, and parsing stays Qt-free:
    the help render never imports the binding."""
    # A GitHub runner's GITHUB_ACTIONS (or FORCE_COLOR) hands
    # rich a forced terminal, and the command names render
    # ANSI-wrapped — a raw ``" gui "`` then matches nothing
    # (the gui extra's docstring once supplied the match by
    # carrying the words "the gui extra" in the help text
    # itself, #927). The assertion reads the de-colored output,
    # so the command row is what matches wherever the test
    # runs.
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert " gui " in re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)


def test_the_gui_dependencies_are_base_dependencies() -> None:
    """PySide6 and the terminal library walk through the base
    dependencies (#1023, reversing #927's extra): the plain
    ``pip install msks`` runs the GUI too, and no optional
    extra gates them (the umbrella and the Essentials split
    both checked) beside the pyghostty wheel that bundles
    libghostty-vt."""
    deps = list(PYPROJECT["project"]["dependencies"])
    base = {dep_name(d) for d in deps}
    assert "pyside6" not in base, "the PySide6 umbrella entered the base deps"
    assert "pyside6-essentials" in base, (
        "PySide6-Essentials left the base deps"
    )
    assert "pyghostty" in base, "pyghostty left the base deps"
    assert "PySide6-Essentials>=6.8,<6.13" in deps, (
        "the PySide6-Essentials floor or ceiling moved"
    )
    assert "pyghostty==0.1.4" in deps, "the pyghostty pin moved"
    assert "optional-dependencies" not in PYPROJECT["project"], (
        "an optional extra returned — the GUI deps are base "
        "dependencies (#1023); a new extra for other purposes "
        "needs this assert revisited"
    )


def test_the_gui_window_opens_offscreen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the binding installed (#928), the command's entry opens
    the main-window shell: a real offscreen ``QApplication``, a
    shown window, and the event loop entered (the exec stand-in
    returns at once). ``exec`` is patched on the class because
    the real loop never returns under a test."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("PySide6 is not installed on this host")
    # os.environ, not monkeypatch: Qt's shutdown-time fontconfig
    # re-scan runs after fixture teardown has restored the env —
    # monkeypatch.undo would hand it the host /etc/fonts and the
    # warnings return at exit. The bare <fontconfig/> keeps both
    # the in-test scan and the shutdown scan silent; no fonts are
    # needed offscreen.
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # The client state root, isolated (#939): the run's geometry
    # memory reads and writes only this temporary tree.
    state = tmp_path / "state"
    monkeypatch.setenv("MSKSC_STATE_DIR", str(state))
    # The launch plant's target (#1045), isolated the same way:
    # on a Linux host every run() plants the desktop shortcut
    # under the data home before the window opens.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    import PySide6.QtWidgets as QtWidgets

    # The app exists before the entry runs: run() reuses it, so
    # the test can snapshot the platform's default point size
    # and pin the window's font scale against it (#939).
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-gui-test"]
    )
    default_size = qapp.font().pointSizeF()  # restored at the
    # end: the run scales the shared application's font, and
    # later tests in this worker measure with the platform's
    # own metrics. The style pin stays (Fusion): Qt owns the
    # style object, and it is what the window runs anyway.

    # The shown window outlives run() only inside Qt: the local
    # goes with the frame, and a parentless widget is
    # Python-owned — the exec stand-in records it (and its
    # geometry) while it lives, for the opening pins below.
    # Earlier runs' and tests' windows may still stand in Qt's
    # top-level list; only a window this run first shows is
    # this run's.
    opened: dict[str, object] = {}
    seen: set[int] = set()

    def record_and_exit(self: QtWidgets.QApplication) -> int:
        windows = [
            widget
            for widget in self.topLevelWidgets()
            if isinstance(widget, QtWidgets.QMainWindow)
            and id(widget) not in seen
        ]
        seen.update(id(widget) for widget in windows)
        opened["windows"] = windows
        opened["geometry"] = [
            (w.x(), w.y(), w.width(), w.height()) for w in windows
        ]
        # The strip's shape read while the window stands (#962,
        # #1011): the tabs, their labels, the current one, and
        # the Workspaces tab's side staying buttonless.
        opened["tabs"] = [
            (
                type(w.centralWidget()).__name__,
                [
                    w.centralWidget().tabText(i)
                    for i in range(w.centralWidget().count())
                ],
                w.centralWidget().currentIndex(),
            )
            if isinstance(w.centralWidget(), QtWidgets.QTabWidget)
            else None
            for w in windows
        ]
        opened["plus"] = [
            (
                w.centralWidget()
                .tabBar()
                .tabButton(0, QtWidgets.QTabBar.ButtonPosition.RightSide)
            )
            if isinstance(w.centralWidget(), QtWidgets.QTabWidget)
            else None
            for w in windows
        ]
        # The picker's row read while the window stands (#1009):
        # the listing's connection button, the label it carries.
        opened["picker"] = [
            (
                type(w.centralWidget().widget(0).listing.connection).__name__,
                w.centralWidget().widget(0).listing.connection.text(),
            )
            for w in windows
        ]
        return 0

    monkeypatch.setattr(QtWidgets.QApplication, "exec", record_and_exit)
    assert app.run() == 0
    # The launch planted the desktop shortcut (#1045): a Linux
    # host answers with the entry under the data home, macOS
    # with nothing planted — its dock reads the window's own
    # icon.
    entry = tmp_path / "data" / "applications" / "msks.desktop"
    if sys.platform.startswith("linux"):
        assert entry.exists()
    else:
        assert not entry.exists()
    qapp = QtWidgets.QApplication.instance()
    assert qapp is not None
    assert qapp.platformName() == "offscreen"
    # The chrome's anchor (#1046, #1056): the terminal face's
    # default as a pixel size — 15 logical pixels — one absolute
    # write on the application font before any widget built,
    # converted through the screen's own dpi so the same pixels
    # open on every platform (the offscreen platform answers
    # 96 where macOS answers 72).
    from msks.client.gui.terminal import fonts

    dpi = qapp.primaryScreen().logicalDotsPerInch()
    assert qapp.font().pointSizeF() == pytest.approx(
        app.chrome_pt(fonts.DEFAULT_PT * app.FONT_SCALE, dpi)
    )
    # The converted point size rasterizes back to the anchor's
    # own pixels: 15 at the default scale, on a 96-dpi host and
    # a 72-dpi one alike (#1056). The pin reads the size back
    # off a face the database always holds — the bundled
    # terminal face, registered because the offscreen
    # platform may answer an empty database, where an app font
    # of Sans Serif resolves to nothing — and the conversion
    # is the dpi's, not the family's.
    from PySide6.QtGui import QFontInfo

    fonts.register_faces()
    face = fonts.make_font()
    face.setPointSizeF(qapp.font().pointSizeF())
    realized = QFontInfo(face).pixelSize()
    assert realized >= 0
    assert realized == round(fonts.DEFAULT_PT * app.FONT_SCALE)
    # The opening geometry, first open (#939): nothing saved
    # answers, so the fixed 1280x720 default does (the platform
    # places the frame, so the size is what is pinned).
    first = opened["geometry"][0]
    assert first[2:] == (app.DEFAULT_WIDTH, app.DEFAULT_HEIGHT)
    # The top-level strip (#962): the window's central widget is
    # the tab strip, the Workspaces tab first and current, the
    # Images tab beside it (#964), the Secrets tab third (#966).
    from msks.client.gui.tabs import (
        IMAGES_LABEL,
        SECRETS_LABEL,
        WORKSPACES_LABEL,
    )

    assert opened["tabs"][0] == (
        "MainTabs",
        [WORKSPACES_LABEL, IMAGES_LABEL, SECRETS_LABEL],
        0,
    )
    # The picker rides the listing's status row (#1009): a
    # connection button in the listing's chrome, its label
    # naming whichever preset the run's environment stands on.
    kind, label = opened["picker"][0]
    assert kind == "ConnectionButton"
    assert label
    # The whole-client zoom's keys (#976): three application
    # shortcuts on the window — both plus spellings on the grow
    # one — so the focus never gates them.
    from PySide6 import QtCore, QtGui

    zoom_keys = sorted(
        tuple(sorted(key.toString() for key in shortcut.keys()))
        for widget in opened["windows"]
        for shortcut in widget.findChildren(QtGui.QShortcut)
        if shortcut.context() == QtCore.Qt.ShortcutContext.ApplicationShortcut
    )
    assert zoom_keys == [
        ("Ctrl++", "Ctrl+=", "Ctrl+Shift++"),
        ("Ctrl+-",),
        ("Ctrl+0",),
    ]
    # The strip stays plain (#1011): the Workspaces tab's side
    # carries no button — the create action lives on the
    # listing's own button row, the page's bottom bar.
    assert opened["plus"][0] is None

    # Each run anchors the shared application's font at the
    # terminal's default (#1046), and later tests in this
    # worker measure with the platform's own metrics — restore
    # the platform's size after every run but the last (which
    # the tail below restores once, with this same write). The
    # write is absolute, so a second run rewrites the same size
    # instead of compounding a scale onto the first.
    def unscale() -> None:
        font = qapp.font()
        font.setPointSizeF(default_size)
        qapp.setFont(font)

    unscale()
    # The close wrote it down (#939): the file holds the window
    # the operator closed — size and place — for the next open
    # to take back.
    saved = json.loads((state / "gui-window.json").read_text())
    assert saved["width"] == app.DEFAULT_WIDTH
    assert saved["height"] == app.DEFAULT_HEIGHT
    assert saved["x"] >= 0 and saved["y"] >= 0
    # A saved geometry the screens still show wins over the
    # default (#939): the second open takes the closed rect
    # back — place and size together.
    (state / "gui-window.json").write_text(
        json.dumps({"x": 30, "y": 40, "width": 900, "height": 620})
    )
    assert app.run() == 0
    second = opened["geometry"][0]
    assert second[2:] == (900, 620)
    assert abs(second[0] - 30) <= 4 and abs(second[1] - 40) <= 4
    unscale()
    # A rect no connected screen shows is refused (#939): a
    # monitor change or an unplugged display leaves the old
    # place off the desktop, and the default answers instead —
    # a window opened where nothing can see it never comes
    # back.
    (state / "gui-window.json").write_text(
        json.dumps({"x": 99999, "y": 99999, "width": 900, "height": 620})
    )
    assert app.run() == 0
    assert opened["geometry"][0][2:] == (
        app.DEFAULT_WIDTH,
        app.DEFAULT_HEIGHT,
    )
    # Every run scaled the shared application's font; restore it
    # so later tests in this worker measure with the platform's
    # own metrics.
    restored = qapp.font()
    restored.setPointSizeF(default_size)
    qapp.setFont(restored)
    # The style pin (#937): whatever the session would name, the
    # window runs Fusion — the wheel ships it inside QtWidgets,
    # so no session style engine loads alongside it.
    assert qapp.style().objectName().lower() == "fusion"
    # The pin wrote the real environment (the constructor reads
    # it); drop it again so later tests in this worker start
    # from a clean one.
    os.environ.pop("QT_STYLE_OVERRIDE", None)
    # The child-process import pins libz and libzstd: pytest's
    # own process preloads both (its dev dependencies carry
    # them), so the in-process import above cannot fail on
    # them — a child with no pytest can.
    child = subprocess.run(
        [sys.executable, "-c", "from PySide6 import QtWidgets"],
        capture_output=True,
    )
    assert child.returncode == 0, child.stderr.decode()
    # The platform plugins' sonames pin the same way: a plain
    # dlopen resolves every DT_NEEDED entry or fails — no
    # display needed, so a headless CI host exercises it too.
    # macOS wheels ship no libqxcb (qcocoa there); the check
    # runs only where the plugins exist.
    qt_dir = Path(QtWidgets.__file__).parent / "Qt/plugins/platforms"
    for plugin in ("libqxcb.so", "libqwayland.so"):
        plug = qt_dir / plugin
        if plug.exists():
            ctypes.CDLL(str(plug), mode=os.RTLD_NOW)
    # The wayland plugin loads its graphics integration as
    # subplugins at runtime; the egl one names libwayland-egl,
    # the shm fallback covers the rest (the devenv comment
    # above its set names the split).
    egl = (
        qt_dir.parent
        / "wayland-graphics-integration-client/libqt-plugin-wayland-egl.so"
    )
    if egl.exists():
        ctypes.CDLL(str(egl), mode=os.RTLD_NOW)


def test_open_window_reuses_an_existing_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The window path survives a QApplication made earlier in
    the process — the instance check, not a second application
    (Qt wants one per process) — and owns its bridge: started
    with the window, stopped on the way out. A stub QtWidgets
    pins the shape without a display; the bridge itself is the
    real one (its own tests cover the loop), and the stub's
    aboutToQuit records the stop hook like the real signal
    would. The path seats the bundled faces before it builds
    the window (#1211), and the order pin reads the seat before
    the window's construction."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("PySide6 is not installed on this host")
    from msks.client.gui import arrival as arrival_mod
    from msks.client.gui import connection as connection_mod
    from msks.client.gui import interrupt as interrupt_mod
    from msks.client.gui import menubar as menubar_mod
    from msks.client.gui import scheme as scheme_mod
    from msks.client.gui import tabs as tabs_mod
    from msks.client.gui import unlock as unlock_mod
    from msks.client.gui import workspace as workspace_mod
    from msks.client.gui import zoom as zoom_mod
    from msks.client.gui.bridge import Bridge
    from msks.client.gui.connection import ConnectionPreset
    from msks.client.gui.images import tab as images_tab_mod
    from msks.client.gui.secrets import tab as secrets_tab_mod

    shown: list[str] = []
    stops: list[object] = []
    styles: list[str] = []
    identities: list[str] = []
    icons: list[object] = []
    central: list[object] = []
    built: list[object] = []
    settles: list[tuple[object, object]] = []
    screens: list[object] = []
    images: list[tuple[object, str]] = []
    zooms: list[tuple[object, object]] = []
    schemes: list[tuple[object, object]] = []
    windows: list[object] = []
    bars: list[object] = []

    # The stub window carries no menu bar (#1007): the window
    # path asks the bar module to install, and the stub records
    # the ask — the bar's own construction runs in its own
    # suite over a real window, not over the FakeWindow here.
    monkeypatch.setattr(
        menubar_mod, "install_bar", lambda window: bars.append(window)
    )

    armed: list[object] = []
    settled: list[int] = []

    def fake_arm(qapp: object):
        """The Ctrl-C wiring's stub (#1037): the recording pins
        the arming over the standing app and the settle over
        the loop's code; the real wiring runs in its own
        suite."""
        armed.append(qapp)
        return lambda code: settled.append(code) or code

    monkeypatch.setattr(interrupt_mod, "arm_sigint", fake_arm)

    class FakeSignal:
        def connect(self, slot: object) -> None:
            stops.append(slot)

    shutdowns: list[object] = []

    class FakeShellHub:
        """The shell hub's stub (#976, #1009): the window path
        hands the zoom the shell's own hub — the recording pins
        the handoff — and a reconnect's teardown shuts the hub
        down with the connection it belonged to."""

        def shutdown(self) -> None:
            shutdowns.append(self)

    retargets: list[object] = []

    class FakeZoom:
        """The whole-client zoom's stub (#976, #1009): the window
        path builds it over the shell's hub and the window — the
        recording pins both — and a reconnect retargets it at
        the fresh hub."""

        def __init__(self, hub: object, window: object) -> None:
            zooms.append((hub, window))

        def retarget(self, hub: object) -> None:
            retargets.append(hub)

    monkeypatch.setattr(zoom_mod, "Zoom", FakeZoom)

    scheme_retargets: list[object] = []

    class FakeScheme:
        """The desktop-scheme pairing's stub (#1006, #1009): the
        window path builds it over the shell's hub and the app
        — the recording pins both — and a reconnect retargets
        it at the fresh hub."""

        def __init__(self, hub: object, app: object) -> None:
            schemes.append((hub, app))

        def retarget(self, hub: object) -> None:
            scheme_retargets.append(hub)

    monkeypatch.setattr(scheme_mod, "Scheme", FakeScheme)

    # The bundled faces' seat (#1211): the window path calls
    # it before it builds its first widget — the recorder pins
    # the call's place in the order, never the real seat (the
    # stub run builds no application for a database to sit in).
    from msks.client.gui.terminal import fonts as faces

    monkeypatch.setattr(faces, "register_faces", lambda: shown.append("faces"))

    class FakeSettle:
        """The opening settle's stub (#1177): the window path
        clears the window's central widget through the gate and
        waits for the window's first paint — the recording
        pins both, and the order pin reads the clear before
        the show."""

        def __init__(self, content: object, bridge: object = None) -> None:
            self.content = content
            self.bridge = bridge

        def after_first_paint(self, window: object) -> None:
            settles.append((self.content, self.bridge, window))
            shown.append("settled")

    monkeypatch.setattr(arrival_mod, "Settle", FakeSettle)

    class FakeRect:
        """The window geometry's shape for the save pin (#939):
        the four reads the close path makes."""

        def x(self) -> int:
            return 10

        def y(self) -> int:
            return 20

        def width(self) -> int:
            return 640

        def height(self) -> int:
            return 480

    class FakeWindow:
        def __init__(self) -> None:
            windows.append(self)
            shown.append("window")

        def setWindowTitle(self, title: str) -> None:
            shown.append(title)

        def setCentralWidget(self, widget: object) -> None:
            central.append(widget)

        def resize(self, width: int, height: int) -> None:
            shown.append(f"sized {width}x{height}")

        def geometry(self) -> FakeRect:
            return FakeRect()

        def centralWidget(self) -> object:
            return central[-1]

        def show(self) -> None:
            shown.append("shown")

    class FakeScreen:
        """The shell the window opens on (#945): the stub the
        window path takes for the real widget — the list and
        the detail page's stack — recording the bridge it was
        built over."""

        def __init__(
            self, bridge: object, terminal_cmd: object = None
        ) -> None:
            built.append(bridge)
            screens.append(self)
            self.terminal_cmd = terminal_cmd
            self.hub = FakeShellHub()  # the zoom's handoff (#976)
            self.listing = FakeListing()  # the picker's row (#1009)
            self.detail = FakeDetail()  # the page's mode ask (#1152)

    bars_hidden: list[bool] = []

    class HiddenSignal:
        """The page's terminal-only ask's stub (#1152): the
        window path wires it to the strip's own hide."""

        def __init__(self) -> None:
            self.slots: list[object] = []

        def connect(self, slot: object) -> None:
            self.slots.append(slot)

    class FakeDetail:
        """The detail page's stub (#1152): the terminal-only
        mode's ask, the one signal the window path reads."""

        chrome_hidden = HiddenSignal()

    monkeypatch.setattr(workspace_mod, "WorkspaceShell", FakeScreen)

    class FakeImagesTab:
        """The Images tab's stub (#964): the window path builds
        it over the same bridge the shell reads, under the
        strip's second tab — the recording pins both."""

        def __init__(self, bridge: object, url: str) -> None:
            images.append((bridge, url))

    monkeypatch.setattr(images_tab_mod, "ImagesTab", FakeImagesTab)

    secrets_built: list[object] = []
    secrets_tabs: list[object] = []

    class FakeSecretsTab:
        """The Secrets tab's stub (#966): the window path builds
        it over the same bridge, under the strip's third tab —
        the recording pins it."""

        def __init__(self, bridge: object) -> None:
            secrets_built.append(bridge)
            secrets_tabs.append(self)

    monkeypatch.setattr(secrets_tab_mod, "SecretsTab", FakeSecretsTab)

    unlock_windows: list[object] = []
    unlock_retargets: list[tuple[object, object, object]] = []

    class FakeUnlockFlow:
        """The unlock flow's stub (#1147): the window path builds
        one flow over the window before the first tree lands, and
        every landing retargets it at the fresh tree's bridge,
        listing, and Secrets tab — the recording pins the
        handoffs; the flow's own suite runs the real one."""

        def __init__(self, window: object) -> None:
            unlock_windows.append(window)

        def retarget(
            self, bridge: object, listing: object, secrets: object
        ) -> None:
            unlock_retargets.append((bridge, listing, secrets))

    monkeypatch.setattr(unlock_mod, "UnlockFlow", FakeUnlockFlow)

    class FakeTabs:
        """The strip the shell lands under (#962): the stub the
        window path takes for the real tab widget — the shell is
        its first page, the Workspaces tab, the Images tab's
        page lands beside it (#964). The strip carries no
        signals of its own since the plus badge left it
        (#1011)."""

        def __init__(self, page: object, bridge: object = None) -> None:
            self.page = page
            self.bridge = bridge
            self.pages: list[tuple[object, str]] = []

        def addTab(self, page: object, label: str) -> None:
            self.pages.append((page, label))

        def set_bar_hidden(self, hidden: bool) -> None:
            """The strip's own hide (#1152): recorded for the
            wiring pin."""
            bars_hidden.append(hidden)

    monkeypatch.setattr(tabs_mod, "MainTabs", FakeTabs)

    class PickedSignal:
        """The strip's pick signal's stub (#1009): the window
        path connects its re-dial on it."""

        def __init__(self) -> None:
            self.slots: list[object] = []

        def connect(self, slot: object) -> None:
            self.slots.append(slot)

    listings: list[FakeListing] = []

    class FakeListing:
        """The shell's listing stub (#1009): the picker rides its
        status row — the presets host on it, the picks cross on
        its signal, a refused pick flashes its status line —
        recording all three for the wiring pins."""

        connection_picked = PickedSignal()

        def __init__(self) -> None:
            self.hosted: list[object] = []
            self.flashed: list[str] = []
            listings.append(self)

        def host_presets(self, presets: object) -> None:
            self.hosted.append(presets)

        def flash(self, message: str) -> None:
            self.flashed.append(message)

    class FakeConnections:
        """The picker's presenter stub (#1009): one plain row,
        the pick recording itself, the answer steerable for the
        refused arm."""

        def __init__(self, conf: object = None) -> None:
            self.conf = conf
            self.picked: list[object] = []
            self.answer = True

        def presets(self) -> list[ConnectionPreset]:
            return [ConnectionPreset("environment", "environment", "")]

        def pick(self, preset: object) -> bool:
            self.picked.append(preset)
            return self.answer

    connections_holder: list[FakeConnections] = []

    def fake_connections(conf: object = None) -> FakeConnections:
        held = FakeConnections(conf)
        connections_holder.append(held)
        return held

    monkeypatch.setattr(connection_mod, "Connections", fake_connections)

    class FakeFont:
        """The app font's shape for the scale pin (#939): a point
        size the window path reads and rewrites."""

        def __init__(self, size: float) -> None:
            self.size = size

        def pointSizeF(self) -> float:
            return self.size

        def setPointSizeF(self, size: float) -> None:
            self.size = size

    class FakeApp:
        aboutToQuit = FakeSignal()
        the_font = FakeFont(9.0)

        def __init__(self, argv: list[str]) -> None:
            shown.append("created")

        def setStyle(self, name: str) -> None:
            styles.append(name)

        def setDesktopFileName(self, name: str) -> None:
            identities.append(name)

        def setApplicationName(self, name: str) -> None:
            identities.append(name)

        def setWindowIcon(self, icon: object) -> None:
            icons.append(icon)

        def font(self) -> FakeFont:
            return self.the_font

        def setFont(self, font: FakeFont) -> None:
            shown.append(f"font {font.size}")

        def primaryScreen(self) -> object:
            return self  # the screen-shape pair the anchor converts through

        def logicalDotsPerInch(self) -> float:
            return 96.0

        def exec(self) -> int:
            shown.append("exec")
            return 3

    existing = FakeApp([])
    # os.environ, swapped for a plain dict copy: open_window
    # writes the style override, and the write must not leak
    # into the worker's real environment for later tests —
    # teardown drops the copy with monkeypatch's restore.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("QT_STYLE_OVERRIDE", None)
    # The saved geometry's path, isolated (#939): the stub run
    # must read nothing the operator saved and write nothing it
    # would see — an empty temporary root answers both.
    monkeypatch.setattr(
        app, "geometry_path", lambda: tmp_path / "gui-window.json"
    )
    stub = ModuleType("QtWidgets")
    stub.QApplication = FakeApp  # type: ignore[attr-defined]
    stub.QMainWindow = FakeWindow  # type: ignore[attr-defined]
    FakeApp.instance = staticmethod(lambda: existing)  # type: ignore[attr-defined]
    started: list[Bridge] = []
    real_start, real_stop = Bridge.start, Bridge.stop
    Bridge.start = lambda self: started.append(self)  # type: ignore[method-assign]
    Bridge.stop = lambda self: shown.append("stopped")  # type: ignore[method-assign]
    try:
        assert app.open_window(stub) == 3
        # The faces are seated before the window is built
        # (#1211): a strip built ahead of the seat measures its
        # bar with the fallback face and keeps that size.
        assert shown.index("faces") < shown.index("window")
        # One pick's re-dial (#1009): the presenter applies the
        # preset, the standing bridge stops, the shell hub's
        # terminals close with the connection they belonged to,
        # and a fresh tree lands — the zoom retargeted at the
        # fresh hub, the menu re-hosted. Driven here, inside the
        # patch's own bracket.
        preset = ConnectionPreset("farm", "alias", "https://10.0.0.9:9660")
        screens[0].listing.connection_picked.slots[0](preset)
        assert connections_holder[0].picked == [preset]
        assert len(started) == 2  # the fresh tree's own bridge
        assert len(stops) == 2  # its aboutToQuit hook beside it
        assert shutdowns == [screens[0].hub]
        assert retargets == [screens[1].hub]
        assert scheme_retargets == [screens[1].hub]
        assert central[1].page is screens[1]  # the fresh tree landed
        assert len(listings[1].hosted) == 1  # its picker hosted
        # A refused pick (#1009) — the file changed under the
        # menu — re-dials nothing: no third bridge, no teardown,
        # the menu alone re-hosted on the standing truth.
        connections_holder[0].answer = False
        screens[1].listing.connection_picked.slots[0](preset)
        assert len(started) == 2
        assert len(central) == 2
        assert len(listings[1].hosted) == 2
        # The refusal names itself where every flash does
        # (#1009): the listing's own status line.
        assert listings[1].flashed == [
            "the connection pick was refused — farm did not resolve"
        ]
        shown.append("driven")
    finally:
        Bridge.start, Bridge.stop = real_start, real_stop  # type: ignore[method-assign]
    assert shown.count("created") == 1  # the pre-made one only
    assert styles == ["Fusion"]  # the session never picks the style
    # The chrome's anchor (#1046, #1056): the terminal
    # default's 15 pixels converted through the 96-dpi screen
    # the fake answers — 11.25pt — whatever the existing app's
    # 9pt font carried. One absolute write on the application
    # font, before any widget.
    assert "font 11.25" in shown
    # The desktop identity (#1021, #1043): the entry name the
    # compositor matches the icon by, and the application name
    # macOS's menu bar titles its application menu after — both
    # the client's own name, landing beside the icon itself.
    assert identities == ["msks", "msks"]
    assert len(icons) == 1
    # The opening size (#939): nothing saved answers, so the
    # fixed 1280x720 default does.
    assert "sized 1280x720" in shown
    # The override landed before the constructor could consult
    # the session theme.
    assert os.environ["QT_STYLE_OVERRIDE"] == "Fusion"
    assert app.WINDOW_TITLE in shown
    # The window opens on the top-level tab strip (#962): the
    # screen is the strip's first page — the Workspaces tab —
    # built over the same bridge the window started.
    assert isinstance(central[0], FakeTabs)
    assert central[0].page is screens[0]
    assert len(listings[0].hosted) == 1  # the picker hosted (#1009)
    assert built == started
    # The Images tab lands beside the shell (#964): built over
    # the same bridge, under the strip's second tab, on the
    # channel URL the config resolves — the default, with no
    # config in the run.
    from msks.client.gui.images import channel_url

    assert images[:1] == [(started[0], channel_url(None))]
    assert secrets_built[:1] == [started[0]]  # the same bridge (#966)
    # The unlock flow (#1147): one flow over the window, built
    # before the first tree landed, and each landing's own
    # retarget — the first tree's and the re-dial's, each at its
    # bridge, its listing, and its Secrets tab.
    assert unlock_windows == [windows[0]]
    assert unlock_retargets == [
        (started[0], listings[0], secrets_tabs[0]),
        (started[1], listings[1], secrets_tabs[1]),
    ]
    assert [label for _page, label in central[0].pages] == [
        "Images",
        "Secrets",
    ]
    # The strip carries the window's bridge (#1177): the page
    # settle gates each tab change's fade on its refreshes.
    assert central[0].bridge is started[0]
    # The zoom takes the shell's own hub and the window (#976):
    # the terminals it steps are the shell's, the keys sit on
    # the window.
    assert zooms == [(screens[0].hub, windows[0])]
    # The page's terminal-only mode reaches the strip (#1152):
    # the page's ask is wired to the strip's own hide, once per
    # landed tree, and the ask carries its answer through.
    assert bars_hidden == []
    screens[0].detail.chrome_hidden.slots[0](True)
    assert bars_hidden == [True]
    screens[1].detail.chrome_hidden.slots[0](False)
    assert bars_hidden == [True, False]
    # The macOS menu bar's install was asked over the window
    # (#1007), once per open — the reconnect lands a fresh
    # content tree on the same window, not a fresh window.
    assert bars == [windows[0]]
    # The opening settle arms on the content tree the first
    # landing set, over the window, before the show (#1177):
    # the first frame draws the frame over a clear content.
    assert settles == [(central[0], started[0], windows[0])]
    assert shown.index("settled") < shown.index("shown")
    # The desktop-scheme pairing takes the same hub and the
    # app itself (#1006): the palette it paints is the
    # application's, and its retarget follows the zoom's
    # (#1009).
    assert schemes == [(screens[0].hub, existing)]
    assert shown[-1] == "driven"  # the drive ran last, inside the bracket
    # The faces were seated once (#1211): the re-dial above rebuilt
    # the tree and asked for no second seat.
    assert shown.count("faces") == 1
    assert "stopped" in shown  # the return path stopped the first bridge
    # The saved geometry written on close (#939): the fake's
    # rect, as the next open reads it.
    saved = json.loads((tmp_path / "gui-window.json").read_text())
    assert saved == {"x": 10, "y": 20, "width": 640, "height": 480}
    # The Ctrl-C wiring (#1037): armed over the standing app
    # before the loop, the settle applied to the loop's own
    # code on the way out.
    assert armed == [existing]
    assert settled == [3]


def test_open_window_keeps_a_user_style_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A preset ``QT_STYLE_OVERRIDE`` is the user's own choice
    (#937): the window neither rewrites the variable nor resets
    the style on the app — the session style is the only thing
    the pin replaces. The same stub shape as the reuse test,
    with the choice preset in the swapped-out environment."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("PySide6 is not installed on this host")
    from msks.client.gui import arrival as arrival_mod
    from msks.client.gui import connection as connection_mod
    from msks.client.gui import menubar as menubar_mod
    from msks.client.gui import scheme as scheme_mod
    from msks.client.gui import tabs as tabs_mod
    from msks.client.gui import unlock as unlock_mod
    from msks.client.gui import workspace as workspace_mod
    from msks.client.gui import zoom as zoom_mod
    from msks.client.gui.bridge import Bridge
    from msks.client.gui.images import tab as images_tab_mod
    from msks.client.gui.secrets import tab as secrets_tab_mod

    # The stub window carries no menu bar (#1007): the bar's
    # install is stubbed the same way as the reuse test's, the
    # construction itself pinned in the bar's own suite.
    monkeypatch.setattr(menubar_mod, "install_bar", lambda window: None)

    # The unlock flow's stub (#1147): the style test reads the
    # environment and the app font only; the flow's retarget
    # needs the stubs' signals it cannot find, and the real flow
    # runs in its own suite.

    class FakeUnlockFlow:
        def __init__(self, window: object) -> None:
            pass

        def retarget(
            self, bridge: object, listing: object, secrets: object
        ) -> None:
            pass

    monkeypatch.setattr(unlock_mod, "UnlockFlow", FakeUnlockFlow)

    # The Ctrl-C wiring's stub (#1037): armed, the settle an
    # identity — the style test reads the environment and the
    # app font only; the real wiring runs in its own suite.
    from msks.client.gui import interrupt as interrupt_mod

    monkeypatch.setattr(
        interrupt_mod, "arm_sigint", lambda qapp: lambda code: code
    )

    styles: list[str] = []
    central: list[object] = []

    class FakeSignal:
        def connect(self, slot: object) -> None:
            pass

    class FakeTabs:
        """The strip stub (#962): the shell lands under it, the
        Images tab's page beside it (#964) — the shape the
        style test never reads past; the strip carries no
        signals of its own since the plus badge left it
        (#1011)."""

        def __init__(self, page: object, bridge: object = None) -> None:
            pass

        def addTab(self, page: object, label: str) -> None:
            pass

        def set_bar_hidden(self, hidden: bool) -> None:
            """The strip's own hide (#1152): a name the window
            path wires, never read here."""

    class FakeImagesTab:
        """The Images tab stub (#964): built, never read."""

        def __init__(self, bridge: object, url: str) -> None:
            pass

    class FakeWindow:
        def setWindowTitle(self, title: str) -> None:
            pass

        def setCentralWidget(self, widget: object) -> None:
            central.append(widget)

        def resize(self, width: int, height: int) -> None:
            pass

        def geometry(self) -> object:
            return self

        def x(self) -> int:
            return 0

        def y(self) -> int:
            return 0

        def width(self) -> int:
            return 800

        def height(self) -> int:
            return 600

        def centralWidget(self) -> object:
            return central[-1]

        def show(self) -> None:
            pass

    class FakeListing:
        """The listing stub (#1009): the picker's row, its two
        members the window path touches."""

        connection_picked = FakeSignal()

        def host_presets(self, presets: object) -> None:
            pass

    class FakeDetail:
        """The detail page's stub (#1152): the terminal-only
        mode's ask, the one signal the window path wires."""

        chrome_hidden = FakeSignal()

    class FakeScreen:
        def __init__(
            self, bridge: object, terminal_cmd: object = None
        ) -> None:
            self.hub = object()  # the zoom's handoff (#976)
            self.listing = FakeListing()  # the picker's row (#1009)
            self.detail = FakeDetail()  # the mode's ask (#1152)

    class FakeZoom:
        """The zoom's stub: built, never read — the style test
        reads the environment and the app font only."""

        def __init__(self, hub: object, window: object) -> None:
            pass

    monkeypatch.setattr(zoom_mod, "Zoom", FakeZoom)

    class FakeSettle:
        """The opening settle's stub (#1177): built, never read —
        the style test reads the environment and the app font
        only."""

        def __init__(self, content: object, bridge: object = None) -> None:
            pass

        def after_first_paint(self, window: object) -> None:
            pass

    monkeypatch.setattr(arrival_mod, "Settle", FakeSettle)

    class FakeScheme:
        """The pairing's stub (#1006): built, never read — the
        style test reads the environment and the app font
        only."""

        def __init__(self, hub: object, app: object) -> None:
            pass

    monkeypatch.setattr(scheme_mod, "Scheme", FakeScheme)

    monkeypatch.setattr(workspace_mod, "WorkspaceShell", FakeScreen)
    monkeypatch.setattr(images_tab_mod, "ImagesTab", FakeImagesTab)

    secrets_built: list[object] = []

    class FakeSecretsTab:
        """The Secrets tab's stub (#966): the window path builds
        it over the same bridge, under the strip's third tab —
        the recording pins it."""

        def __init__(self, bridge: object) -> None:
            secrets_built.append(bridge)

    monkeypatch.setattr(secrets_tab_mod, "SecretsTab", FakeSecretsTab)

    monkeypatch.setattr(tabs_mod, "MainTabs", FakeTabs)

    class FakeConnections:
        """The picker's presenter stub (#1009): no rows, the
        pick answered True and never called."""

        def __init__(self, conf: object = None) -> None:
            pass

        def presets(self) -> list:
            return []

        def pick(self, preset: object) -> bool:
            return True

    monkeypatch.setattr(connection_mod, "Connections", FakeConnections)

    class FakeApp:
        aboutToQuit = FakeSignal()

        @staticmethod
        def instance() -> object:
            return None

        def __init__(self, argv: list[str]) -> None:
            pass

        def setStyle(self, name: str) -> None:
            styles.append(name)

        def setDesktopFileName(self, name: str) -> None:
            pass  # the identity's own test pins the call

        def setApplicationName(self, name: str) -> None:
            pass

        def setWindowIcon(self, icon: object) -> None:
            pass

        def font(self) -> object:
            return self  # the font-shape pair the scale reads

        def pointSizeF(self) -> float:
            return 10.0

        def setPointSizeF(self, size: float) -> None:
            styles.append(f"font {size:g}")

        def setFont(self, font: object) -> None:
            pass

        def primaryScreen(self) -> object:
            return self  # the screen-shape pair the anchor converts through

        def logicalDotsPerInch(self) -> float:
            return 96.0

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ["QT_STYLE_OVERRIDE"] = "Chosen"
    # The saved geometry's path, isolated (#939): nothing the
    # operator saved is read, nothing this run writes is seen.
    monkeypatch.setattr(
        app, "geometry_path", lambda: tmp_path / "gui-window.json"
    )
    stub = ModuleType("QtWidgets")
    stub.QApplication = FakeApp  # type: ignore[attr-defined]
    stub.QMainWindow = FakeWindow  # type: ignore[attr-defined]
    real_start, real_stop = Bridge.start, Bridge.stop
    Bridge.start = lambda self: None  # type: ignore[method-assign]
    Bridge.stop = lambda self: None  # type: ignore[method-assign]
    try:
        assert app.open_window(stub) == 0
    finally:
        Bridge.start, Bridge.stop = real_start, real_stop  # type: ignore[method-assign]
    assert os.environ["QT_STYLE_OVERRIDE"] == "Chosen"
    # The style write never happened — the choice stands — but
    # the anchored font still applied (#1046, #1056): the
    # terminal default's 15 pixels converted through the 96-dpi
    # screen the stub answers — 11.25pt — whatever the stub's
    # 10pt platform font carried.
    assert styles == ["font 11.25"]
    # A preset knob multiplies the anchor itself (#1046, #1056):
    # a fresh open writes the knob's size, not the platform
    # default's — 15 pixels at 1.2 lands 18 pixels, 13.5pt at
    # the stub's 96 dpi, where the stub's 10pt platform font at
    # 1.2 would land 12pt. The geometry file the first open
    # wrote is repointed away: the second open reads nothing the
    # first saved.
    os.environ["MSKSC_GUI_FONT_SCALE"] = "1.2"
    monkeypatch.setattr(
        app, "geometry_path", lambda: tmp_path / "gui-window-2.json"
    )
    Bridge.start = lambda self: None  # type: ignore[method-assign]
    Bridge.stop = lambda self: None  # type: ignore[method-assign]
    try:
        assert app.open_window(stub) == 0
    finally:
        Bridge.start, Bridge.stop = real_start, real_stop  # type: ignore[method-assign]
    assert styles == ["font 11.25", "font 13.5"]
