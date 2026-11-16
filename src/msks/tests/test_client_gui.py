"""GUI tests (#924): the ``msks gui`` command seam (#928), the
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
    client's error convention. The find_spec seam stands in for
    the absent binding in a process that has it."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: None)
    assert cli.main(["gui"]) == 1
    assert app.HINT in capsys.readouterr().err


def test_the_gui_command_names_a_headless_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No display and no platform override on Linux: the
    pre-flight answers with one line instead of Qt's abort — the
    qFatal the QApplication constructor would raise is past
    anything Python can catch (exit 134 and a multi-line dump).
    The find_spec seam keeps the test base-install safe (a host
    without the extra hears HINT here, not a failure)."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: object())
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["gui"]) == 1
    assert app.NO_DISPLAY in capsys.readouterr().err


def test_the_display_gate_skips_macos(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stock macOS session sets none of the three variables,
    and cocoa needs none of them — the gate sits darwin out and
    the window path runs. The open_window seam stands in for the
    real window."""
    client_env(monkeypatch)
    monkeypatch.setattr(app, "find_spec", lambda name: object())
    monkeypatch.setattr(sys, "platform", "darwin")
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(var, raising=False)
    seen: list[str] = []
    monkeypatch.setattr(
        app, "open_window", lambda QtWidgets: seen.append("window") or 4
    )
    assert cli.main(["gui"]) == 4
    assert seen == ["window"]


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
    """With the extra installed (#928), the command's seam opens
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

    monkeypatch.setattr(QtWidgets.QApplication, "exec", lambda self: 0)
    assert app.run() == 0
    qapp = QtWidgets.QApplication.instance()
    assert qapp is not None
    assert qapp.platformName() == "offscreen"
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


def test_open_window_reuses_an_existing_app() -> None:
    """The window path survives a QApplication made earlier in
    the process — the instance check, not a second application
    (Qt wants one per process). A stub QtWidgets pins the shape
    without a display: the shell reuses the instance it finds
    and shows the window before the loop runs."""
    shown: list[str] = []

    class FakeWindow:
        def setWindowTitle(self, title: str) -> None:
            shown.append(title)

        def show(self) -> None:
            shown.append("shown")

    class FakeApp:
        def __init__(self, argv: list[str]) -> None:
            shown.append("created")

        def exec(self) -> int:
            shown.append("exec")
            return 3

    existing = FakeApp([])
    stub = ModuleType("QtWidgets")
    stub.QApplication = FakeApp  # type: ignore[attr-defined]
    stub.QMainWindow = FakeWindow  # type: ignore[attr-defined]
    FakeApp.instance = staticmethod(lambda: existing)  # type: ignore[attr-defined]
    assert app.open_window(stub) == 3
    assert shown.count("created") == 1  # the pre-made one only
    assert app.WINDOW_TITLE in shown
    assert shown[-1] == "exec"
