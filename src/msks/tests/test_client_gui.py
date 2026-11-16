"""GUI scaffold tests (#924 phase 1): the ``msks gui`` command
seam against the package's three-part shell, and the gui extra's
contract with the base install (#927)."""

import ctypes
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from msks.client import cli
from msks.client.gui import app
from test_client_cli import client_env

ROOT = Path(__file__).resolve().parents[3]

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def dep_name(spec: str) -> str:
    """One dependency spec's bare package name, normalized."""
    return re.split(r"[><=~!;\[]", spec)[0].strip().lower()


def test_the_gui_command_answers_until_the_window_ships(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks gui`` parses onto the seam, which answers that the
    desktop GUI is not in this build yet — one stderr line, the
    client's error convention."""
    client_env(monkeypatch)
    assert cli.main(["gui"]) == 1
    assert app.PENDING_MSG in capsys.readouterr().err


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


def test_the_gui_extra_imports_offscreen(
    tmp_path: Path,
) -> None:
    """The extra's own verification (#927): with PySide6 installed,
    an offscreen ``QApplication`` comes up. On a NixOS host every
    soname the wheels name must be on the loader path (the devenv
    ``gui-libs`` set) or this fails with ``ImportError`` — the pin
    the dev shell's comment points at. Hosts without the extra
    (macOS CI installs the base package) skip.
    """
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

    qapp = QtWidgets.QApplication([])
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
    # The xcb plugin's sonames pin the same way: a plain dlopen
    # resolves every DT_NEEDED entry or fails — no display
    # needed, so a headless CI host exercises it too. macOS
    # wheels ship no libqxcb (qcocoa there); the check runs only
    # where the plugin exists.
    xcb = Path(QtWidgets.__file__).parent / "Qt/plugins/platforms/libqxcb.so"
    if xcb.exists():
        ctypes.CDLL(str(xcb), mode=os.RTLD_NOW)
