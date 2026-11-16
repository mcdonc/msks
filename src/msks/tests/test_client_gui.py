"""GUI scaffold tests (#924 phase 1): the ``msks gui`` command
seam against the package's three-part shell, and the gui extra's
contract with the base install (#927)."""

import tomllib
from pathlib import Path

import pytest
from msks.client import cli
from msks.client.gui import app
from test_client_cli import client_env


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
    (#927): the base dependencies stay Qt-free, and the extra
    names the binding with its floor."""
    root = Path(__file__).resolve().parents[3]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert "PySide6" not in " ".join(data["project"]["dependencies"])
    assert data["project"]["optional-dependencies"]["gui"] == ["PySide6>=6.8"]
