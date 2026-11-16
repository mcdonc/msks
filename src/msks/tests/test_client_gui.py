"""GUI scaffold tests (#924 phase 1): the ``msks gui`` command
seam against the package's three-part shell."""

import pytest
from msks.client import cli
from msks.client.gui import app
from test_client_cli import client_env


def test_the_gui_command_answers_until_the_window_ships(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks gui`` parses onto the seam, which names #928 and
    exits nonzero — one stderr line, the client's error
    convention."""
    client_env(monkeypatch)
    assert cli.main(["gui"]) == 1
    assert app.PENDING_MSG in capsys.readouterr().err
