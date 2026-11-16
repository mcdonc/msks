"""GUI tests (#924): the ``msks gui`` command surface (#928), the
window shell, and the gui extra's contract with the base install
(#927)."""

import ctypes
import importlib.util
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
        app, "open_window", lambda QtWidgets: seen.append("window") or 4
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
    """The ``gui`` extra is the only door PySide6 walks through
    (#927): the base dependencies stay Qt-free (the umbrella and
    the Essentials split both checked), and the extra names the
    binding with its floor and ceiling."""
    base = {dep_name(d) for d in PYPROJECT["project"]["dependencies"]}
    assert "pyside6" not in base
    assert "pyside6-essentials" not in base
    assert PYPROJECT["project"]["optional-dependencies"]["gui"] == [
        "PySide6-Essentials>=6.8,<6.13"
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
    import PySide6.QtWidgets as QtWidgets

    # The app exists before the entry runs: run() reuses it, so
    # the test can snapshot the platform's default point size
    # and pin the window's font scale against it (#939).
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-gui-test"]
    )
    default_size = qapp.font().pointSizeF()

    # The shown window outlives run() only inside Qt: the local
    # goes with the frame, and a parentless widget is
    # Python-owned — the exec stand-in records it while it
    # lives, for the opening-size pin below.
    opened: dict[str, object] = {}

    def record_and_exit(self: QtWidgets.QApplication) -> int:
        opened["windows"] = [
            widget
            for widget in self.topLevelWidgets()
            if isinstance(widget, QtWidgets.QMainWindow)
        ]
        return 0

    monkeypatch.setattr(QtWidgets.QApplication, "exec", record_and_exit)
    assert app.run() == 0
    qapp = QtWidgets.QApplication.instance()
    assert qapp is not None
    assert qapp.platformName() == "offscreen"
    # The font scale (#939): the platform default doubled — one
    # write on the application font before any widget built.
    assert qapp.font().pointSizeF() == pytest.approx(
        default_size * app.FONT_SCALE
    )
    # The opening height (#939): the screen's own size hint
    # doubled, the width the hint's own — the window opens on a
    # listing that out-talls the shell around it.
    windows = opened["windows"]
    assert len(windows) == 1
    screen = windows[0].centralWidget()
    assert windows[0].height() == round(
        screen.sizeHint().height() * app.HEIGHT_SCALE
    )
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
    monkeypatch: pytest.MonkeyPatch,
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
    from msks.client.gui import workspaces as workspaces_mod
    from msks.client.gui.bridge import Bridge

    shown: list[str] = []
    stops: list[object] = []
    styles: list[str] = []
    central: list[object] = []
    screens: list[object] = []

    class FakeSignal:
        def connect(self, slot: object) -> None:
            stops.append(slot)

    class FakeSize:
        """The window size's shape for the opening-size pin
        (#939): the Qt calls the window path makes."""

        def width(self) -> int:
            return 640

        def height(self) -> int:
            return 480

    class FakeWindow:
        def setWindowTitle(self, title: str) -> None:
            shown.append(title)

        def setCentralWidget(self, widget: object) -> None:
            central.append(widget)

        def adjustSize(self) -> None:
            pass  # the fake already carries its opening size

        def size(self) -> FakeSize:
            return FakeSize()

        def resize(self, width: int, height: int) -> None:
            shown.append(f"sized {width}x{height}")

        def show(self) -> None:
            shown.append("shown")

    class FakeScreen:
        """The screen the window opens on (#939): the stub the
        window path takes for the real widget, recording the
        bridge it was built over."""

        def __init__(self, bridge: object) -> None:
            screens.append(bridge)

    monkeypatch.setattr(workspaces_mod, "WorkspaceList", FakeScreen)

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
    # The font scale (#939): the default 9pt doubles to 18 —
    # one write on the application font, before any widget.
    assert "font 18.0" in shown
    # The opening size (#939): the width the layout named, the
    # height doubled.
    assert "sized 640x960" in shown
    # The override landed before the constructor could consult
    # the session theme.
    assert os.environ["QT_STYLE_OVERRIDE"] == "Fusion"
    assert app.WINDOW_TITLE in shown
    # The window opens on the workspace list (#939): the screen
    # is the central widget, built over the same bridge the
    # window started.
    assert len(central) == 1
    assert screens == started
    assert shown[-1] == "stopped"  # the return path stopped the bridge
    assert len(started) == 1  # started with the window
    assert len(stops) == 1  # aboutToQuit holds the same stop


def test_open_window_keeps_a_user_style_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preset ``QT_STYLE_OVERRIDE`` is the user's own choice
    (#937): the window neither rewrites the variable nor resets
    the style on the app — the session style is the only thing
    the pin replaces. The same stub shape as the reuse test,
    with the choice preset in the swapped-out environment."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("the gui extra is not installed on this host")
    from msks.client.gui import workspaces as workspaces_mod
    from msks.client.gui.bridge import Bridge

    styles: list[str] = []
    central: list[object] = []

    class FakeSignal:
        def connect(self, slot: object) -> None:
            pass

    class FakeWindow:
        def setWindowTitle(self, title: str) -> None:
            pass

        def setCentralWidget(self, widget: object) -> None:
            central.append(widget)

        def adjustSize(self) -> None:
            pass

        def size(self) -> FakeSize:
            return FakeSize()

        def resize(self, width: int, height: int) -> None:
            pass

        def show(self) -> None:
            pass

    class FakeSize:
        def width(self) -> int:
            return 640

        def height(self) -> int:
            return 480

    class FakeScreen:
        def __init__(self, bridge: object) -> None:
            pass

    monkeypatch.setattr(workspaces_mod, "WorkspaceList", FakeScreen)

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
    # the font scale still applied (#939): 10pt doubled.
    assert styles == ["font 20"]
