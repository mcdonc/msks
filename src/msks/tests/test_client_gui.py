"""GUI tests (#924): the ``msks gui`` command surface (#928), the
window shell, and the gui extra's contract with the base install
(#927)."""

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


def test_the_gui_command_hints_without_the_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks gui`` on a base install (#928): the absence guard
    answers with the extra's install hint — one stderr line, the
    client's error convention. The find_spec stand-in replaces
    the absent binding in a process that has it."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: None)
    assert cli.main(["gui"]) == 1
    assert app.HINT in capsys.readouterr().err


def test_a_base_install_parses_every_command(
    tmp_path: Path,
) -> None:
    """The base-install contract whole (#927), pinned for real:
    with the binding unimportable — the state of a ``pip
    install msks`` without the extra — the CLI module itself
    imports and parses. The package init once pinned the
    bridge, whose module scope loads Qt, and every command died
    at parse time; a process with the binding installed cannot
    see that bug, so the child blocks the binding the way a
    base install lives."""
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
    The find_spec stand-in keeps the test base-install safe (a host
    without the extra hears HINT here, not a failure)."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: object())
    # The launchd stand-in pins the darwin arm: the gate must
    # answer from the probe the test names, not from the host's
    # launchd — a CI runner's launchd answers for a gui domain
    # even with no console attached, and the real event loop
    # behind that answer never comes back (#934: it hung three
    # runs running).
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
    """The binding is installed but will not load: the install
    hint would send an operator who already has the extra in
    circles, so this arm names the reinstall. The None stub
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
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert " gui " in capsys.readouterr().out


def test_the_gui_extra_stays_off_the_base_install() -> None:
    """The ``gui`` extra is the only door PySide6 and the
    terminal library walk through (#927, #950): the base
    dependencies stay Qt-free (the umbrella and the Essentials
    split both checked), and the extra names the binding with
    its floor and ceiling beside the pyghostty wheel that
    bundles libghostty-vt."""
    base = {dep_name(d) for d in PYPROJECT["project"]["dependencies"]}
    assert "pyside6" not in base
    assert "pyside6-essentials" not in base
    assert "pyghostty" not in base
    assert PYPROJECT["project"]["optional-dependencies"]["gui"] == [
        "PySide6-Essentials>=6.8,<6.13",
        "pyghostty==0.1.4",
    ]


def test_the_gui_window_opens_offscreen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the extra installed (#928), the command's entry opens
    the main-window shell: a real offscreen ``QApplication``, a
    shown window, and the event loop entered (the exec stand-in
    returns at once). ``exec`` is patched on the class because
    the real loop never returns under a test."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("the gui extra is not installed on this host")
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
        # The strip's shape read while the window stands (#962):
        # the tabs, their labels, the current one, and the
        # Workspaces tab's create opener.
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
        return 0

    monkeypatch.setattr(QtWidgets.QApplication, "exec", record_and_exit)
    assert app.run() == 0
    qapp = QtWidgets.QApplication.instance()
    assert qapp is not None
    assert qapp.platformName() == "offscreen"
    # The font scale (#939): the platform default scaled — one
    # write on the application font before any widget built.
    assert qapp.font().pointSizeF() == pytest.approx(
        default_size * app.FONT_SCALE
    )
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
    assert zoom_keys == [("Ctrl++", "Ctrl+="), ("Ctrl+-",), ("Ctrl+0",)]
    # The create opener rides the tab (#962): a plus button
    # embedded in the Workspaces tab.
    plus = opened["plus"][0]
    assert plus is not None
    assert not plus.icon().isNull()  # the drawn green badge (#964)

    # Each run rescales the shared application's font (#939:
    # one scale per open), so a second run in one process would
    # compound it onto the first — the action row's buttons then
    # grow past the default width and the window's own minimum
    # wins over the pinned sizes below. Restore the platform's
    # size after every run but the last (which the tail below
    # restores once, with this same write).
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
    would."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("the gui extra is not installed on this host")
    from msks.client.gui import tabs as tabs_mod
    from msks.client.gui import workspace as workspace_mod
    from msks.client.gui import zoom as zoom_mod
    from msks.client.gui.bridge import Bridge
    from msks.client.gui.images import tab as images_tab_mod
    from msks.client.gui.secrets import tab as secrets_tab_mod

    shown: list[str] = []
    stops: list[object] = []
    styles: list[str] = []
    central: list[object] = []
    built: list[object] = []
    screens: list[object] = []
    creates: list[object] = []
    images: list[tuple[object, str]] = []
    zooms: list[tuple[object, object]] = []
    windows: list[object] = []

    class FakeSignal:
        def connect(self, slot: object) -> None:
            stops.append(slot)

    class FakeShellHub:
        """The shell hub's stub (#976): the window path hands
        the zoom the shell's own hub — the recording pins the
        handoff."""

    class FakeZoom:
        """The whole-client zoom's stub (#976): the window path
        builds it over the shell's hub and the window — the
        recording pins both."""

        def __init__(self, hub: object, window: object) -> None:
            zooms.append((hub, window))

    monkeypatch.setattr(zoom_mod, "Zoom", FakeZoom)

    class CreateSignal:
        """The strip's create opener (#962): the window path
        connects the shell's slot on it — the recording pins the
        wiring."""

        def __init__(self) -> None:
            self.slots: list[object] = []

        def connect(self, slot: object) -> None:
            creates.append(slot)

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

        def setWindowTitle(self, title: str) -> None:
            shown.append(title)

        def setCentralWidget(self, widget: object) -> None:
            central.append(widget)

        def resize(self, width: int, height: int) -> None:
            shown.append(f"sized {width}x{height}")

        def geometry(self) -> FakeRect:
            return FakeRect()

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

        def open_create(self) -> None:
            pass  # the wiring pin reads the bound method, not the call

    monkeypatch.setattr(workspace_mod, "WorkspaceShell", FakeScreen)

    class FakeImagesTab:
        """The Images tab's stub (#964): the window path builds
        it over the same bridge the shell reads, under the
        strip's second tab — the recording pins both."""

        def __init__(self, bridge: object, url: str) -> None:
            images.append((bridge, url))

    monkeypatch.setattr(images_tab_mod, "ImagesTab", FakeImagesTab)

    secrets_built: list[object] = []

    class FakeSecretsTab:
        """The Secrets tab's stub (#966): the window path builds
        it over the same bridge, under the strip's third tab —
        the recording pins it."""

        def __init__(self, bridge: object) -> None:
            secrets_built.append(bridge)

    monkeypatch.setattr(secrets_tab_mod, "SecretsTab", FakeSecretsTab)

    class FakeTabs:
        """The strip the shell lands under (#962): the stub the
        window path takes for the real tab widget — the shell is
        its first page, the Workspaces tab, the Images tab's
        page lands beside it (#964), and its create opener
        lands on the shell."""

        create_requested = CreateSignal()

        def __init__(self, page: object) -> None:
            self.page = page
            self.pages: list[tuple[object, str]] = []

        def addTab(self, page: object, label: str) -> None:
            self.pages.append((page, label))

    monkeypatch.setattr(tabs_mod, "MainTabs", FakeTabs)

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

        def font(self) -> FakeFont:
            return self.the_font

        def setFont(self, font: FakeFont) -> None:
            shown.append(f"font {font.size}")

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
    finally:
        Bridge.start, Bridge.stop = real_start, real_stop  # type: ignore[method-assign]
    assert shown.count("created") == 1  # the pre-made one only
    assert styles == ["Fusion"]  # the session never picks the style
    # The font scale (#939): the default 9pt scales to 13.5 —
    # one write on the application font, before any widget.
    assert "font 13.5" in shown
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
    assert len(central) == 1
    assert isinstance(central[0], FakeTabs)
    assert central[0].page is screens[0]
    assert built == started
    # The Images tab lands beside the shell (#964): built over
    # the same bridge, under the strip's second tab, on the
    # channel URL the config resolves — the default, with no
    # config in the run.
    from msks.client.gui.images import channel_url

    assert images == [(started[0], channel_url(None))]
    assert secrets_built == [started[0]]  # the same bridge (#966)
    assert [label for _page, label in central[0].pages] == [
        "Images",
        "Secrets",
    ]
    # The create opener lands on the shell (#962): the strip's
    # plus button opens the shell's own create dialog.
    assert creates == [screens[0].open_create]
    # The zoom takes the shell's own hub and the window (#976):
    # the terminals it steps are the shell's, the keys sit on
    # the window.
    assert zooms == [(screens[0].hub, windows[0])]
    assert shown[-1] == "stopped"  # the return path stopped the bridge
    assert len(started) == 1  # started with the window
    assert len(stops) == 1  # aboutToQuit holds the same stop
    # The saved geometry written on close (#939): the fake's
    # rect, as the next open reads it.
    saved = json.loads((tmp_path / "gui-window.json").read_text())
    assert saved == {"x": 10, "y": 20, "width": 640, "height": 480}


def test_open_window_keeps_a_user_style_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A preset ``QT_STYLE_OVERRIDE`` is the user's own choice
    (#937): the window neither rewrites the variable nor resets
    the style on the app — the session style is the only thing
    the pin replaces. The same stub shape as the reuse test,
    with the choice preset in the swapped-out environment."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("the gui extra is not installed on this host")
    from msks.client.gui import tabs as tabs_mod
    from msks.client.gui import workspace as workspace_mod
    from msks.client.gui import zoom as zoom_mod
    from msks.client.gui.bridge import Bridge
    from msks.client.gui.images import tab as images_tab_mod
    from msks.client.gui.secrets import tab as secrets_tab_mod

    styles: list[str] = []
    central: list[object] = []

    class FakeSignal:
        def connect(self, slot: object) -> None:
            pass

    class FakeTabs:
        """The strip stub (#962): the shell lands under it, the
        Images tab's page beside it (#964) — the shape the
        style test never reads past, the opener's connect the
        no-op signal's own."""

        create_requested = FakeSignal()

        def __init__(self, page: object) -> None:
            pass

        def addTab(self, page: object, label: str) -> None:
            pass

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

        def show(self) -> None:
            pass

    class FakeScreen:
        def __init__(
            self, bridge: object, terminal_cmd: object = None
        ) -> None:
            self.hub = object()  # the zoom's handoff (#976)

        def open_create(self) -> None:
            pass

    class FakeZoom:
        """The zoom's stub: built, never read — the style test
        reads the environment and the app font only."""

        def __init__(self, hub: object, window: object) -> None:
            pass

    monkeypatch.setattr(zoom_mod, "Zoom", FakeZoom)

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

    class FakeApp:
        aboutToQuit = FakeSignal()

        @staticmethod
        def instance() -> object:
            return None

        def __init__(self, argv: list[str]) -> None:
            pass

        def setStyle(self, name: str) -> None:
            styles.append(name)

        def font(self) -> object:
            return self  # the font-shape pair the scale reads

        def pointSizeF(self) -> float:
            return 10.0

        def setPointSizeF(self, size: float) -> None:
            styles.append(f"font {size:g}")

        def setFont(self, font: object) -> None:
            pass

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
    # the font scale still applied (#939): 10pt at 1.5.
    assert styles == ["font 15"]
