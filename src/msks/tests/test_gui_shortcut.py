"""The desktop shortcut's suite (#1021): the icon assets the
wheel ships, the installer that plants them under the XDG data
home, the ``msks gui-shortcut`` command over it, and the
window identity the compositor matches the installed entry
by."""

import importlib.util
import os
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from msks.client import cli
from msks.client.gui import app as app_mod
from msks.client.gui import rows, shortcut
from test_client_cli import client_env

#: The PNG renders the wheel ships (#1021): one per hicolor
#: size, plus the scalable SVG the window itself loads.
SIZES = {16, 24, 32, 48, 64, 128, 256, 512}


def png_size(data: bytes) -> tuple[int, int]:
    """One PNG's own IHDR dimensions — the header read in
    place, no imaging dependency in the suite."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


def test_the_wheel_ships_the_icon_renders() -> None:
    """The hicolor tree (#1021) carries one PNG per size under
    the id every other name in the match chain derives from,
    and each render's own dimensions match the slot it sits
    in — a mislabeled render would install into the wrong size
    and the desktop would scale garbage."""
    tree = shortcut.icon_dir() / "hicolor"
    name = f"{shortcut.DESKTOP_ID}.png"
    renders = list(tree.glob(f"*x*/apps/{name}"))
    assert {int(p.parent.parent.name.split("x")[0]) for p in renders} == SIZES
    for png in renders:
        declared = int(png.parent.parent.name.split("x")[0])
        assert png_size(png.read_bytes()) == (declared, declared)


def test_the_master_icon_parses() -> None:
    """The scalable master (#1021) is well-formed XML with a
    512-square viewBox — the window loads this file, and a
    broken one would silently leave the fallback icon."""
    master = shortcut.icon_dir() / shortcut.ICON_FILE
    root = ET.fromstring(master.read_text())
    assert root.get("viewBox") == "0 0 512 512"


def test_the_desktop_entry_names_the_match_keys() -> None:
    """The shipped entry template (#1021) carries the keys the
    compositor's match needs — the type, the command, the
    class the window answers to — and its icon and class keys
    derive from the same id the entry's file name, the app id
    the window sets, and the hicolor file names do, so one
    rename that misses the template fails here rather than
    putting the fallback icon back."""
    text = (shortcut.icon_dir() / shortcut.DESKTOP_FILE).read_text()
    assert shortcut.DESKTOP_FILE == f"{shortcut.DESKTOP_ID}.desktop"
    for line in (
        "Type=Application",
        f"Exec={shortcut.DESKTOP_ID} gui",
        f"Icon={shortcut.DESKTOP_ID}",
        f"StartupWMClass={shortcut.DESKTOP_ID}",
        "Terminal=false",
    ):
        assert line in text


def test_the_entry_keeps_the_plain_command_without_a_lookup() -> None:
    """No ``msks`` on ``PATH`` (#1021): the entry keeps the
    plain command the template ships."""
    assert f"Exec={shortcut.DESKTOP_ID} gui" in shortcut.desktop_file_text(
        None
    )


def test_the_entry_names_the_absolute_command() -> None:
    """A resolved ``msks`` (#1021) lands in the entry's Exec
    line: the desktop runs the entry outside the installing
    shell, where the venv's bin directory may not sit on
    ``PATH``."""
    text = shortcut.desktop_file_text("/opt/venv/bin/msks")
    assert "Exec=/opt/venv/bin/msks gui" in text
    assert f"Exec={shortcut.DESKTOP_ID} gui" not in text


def test_the_entry_rewrites_whatever_exec_line_the_template_carries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rewrite swaps the executable alone (#1021),
    landing on the ``Exec`` line itself rather than one
    line's exact spelling: a template whose command gained a
    flag keeps the flag, so the entry and the template
    cannot drift apart silently."""
    rigged = tmp_path / "icons"
    rigged.mkdir()
    (rigged / shortcut.DESKTOP_FILE).write_text(
        "[Desktop Entry]\nExec=msks --flag gui\nIcon=msks\n"
    )
    monkeypatch.setattr(shortcut, "icon_dir", lambda: rigged)
    text = shortcut.desktop_file_text("/opt/msks")
    assert "Exec=/opt/msks --flag gui" in text


def test_the_entry_refuses_a_path_it_cannot_carry_verbatim() -> None:
    """A resolved path the spec cannot carry verbatim (#1021)
    keeps the plain command: a space, a tilde, an equal sign,
    a field-code percent, or a relative spelling — each would
    arrive misparsed or mutated from the desktop's menus,
    silently running the wrong binary."""
    for unusable in (
        "/home/u/My Apps/venv/bin/msks",
        "bin/msks",
        "/home/ci/50%dir/msks",
        "/home/u~x/msks",
        "/opt/we=ird/msks",
    ):
        text = shortcut.desktop_file_text(unusable)
        assert f"Exec={shortcut.DESKTOP_ID} gui" in text
        assert unusable not in text


def test_the_install_plants_the_tree(tmp_path: Path) -> None:
    """The install (#1021) lands every render under the data
    home's hicolor tree, the master under its scalable slot,
    and the entry under applications, each under the id the
    window's app id answers to."""
    entry = shortcut.install_shortcut(tmp_path, None)
    assert entry == tmp_path / "applications" / shortcut.DESKTOP_FILE
    for size in SIZES:
        png = (
            tmp_path
            / "icons/hicolor"
            / f"{size}x{size}/apps/{shortcut.DESKTOP_ID}.png"
        )
        assert png_size(png.read_bytes()) == (size, size)
    svg = tmp_path / "icons/hicolor/scalable/apps" / shortcut.ICON_FILE
    assert svg.exists()
    assert f"Icon={shortcut.DESKTOP_ID}" in entry.read_text()


def test_the_install_is_idempotent(tmp_path: Path) -> None:
    """A second run (#1021) overwrites in place and answers
    the same path — an upgrade refreshes the renders it ships
    with the same command."""
    first = shortcut.install_shortcut(tmp_path, None)
    second = shortcut.install_shortcut(tmp_path, None)
    assert first.read_text() == second.read_text()


def test_the_command_installs_under_the_data_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``msks gui-shortcut`` (#1021) on a Linux desktop: the
    entry lands under ``XDG_DATA_HOME`` naming the resolved
    command, and the one stdout line names the entry — the
    client's own convention."""
    client_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(
        shortcut.shutil, "which", lambda name: "/opt/venv/bin/msks"
    )
    assert cli.main(["gui-shortcut"]) == 0
    out = capsys.readouterr().out
    entry = tmp_path / "applications" / shortcut.DESKTOP_FILE
    assert str(entry) in out
    assert "Exec=/opt/venv/bin/msks gui" in entry.read_text()


def test_the_command_refuses_off_linux(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A desktop without the Linux conventions (#1021) hears
    the one-line refusal instead of a planted file that could
    only sit unused."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert shortcut.run() == 1
    assert shortcut.NOT_LINUX in capsys.readouterr().err


def test_the_command_refuses_a_relative_data_home(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A relative ``XDG_DATA_HOME`` (#1021) hears the absolute
    path rule instead of planting the tree under whatever the
    working directory happened to be."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "somewhere/relative")
    assert shortcut.run() == 1
    assert shortcut.NOT_ABSOLUTE in capsys.readouterr().err


def test_the_command_names_an_unwritable_data_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A write the filesystem refuses (#1021): the one-line
    failure names the shortcut and the error, the client's
    error convention — the data home pointing at a file is the
    rigged refusal."""
    monkeypatch.setattr(sys, "platform", "linux")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocked))
    assert shortcut.run() == 1
    assert "cannot install the desktop shortcut" in capsys.readouterr().err


def test_the_command_names_a_home_it_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``XDG_DATA_HOME`` and no home the platform will
    vouch for (#1021): the one-line failure, not a traceback —
    the home resolution sits inside the guarded block."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    def unresolvable() -> Path:
        raise RuntimeError("no home directory")

    monkeypatch.setattr(Path, "home", staticmethod(unresolvable))
    assert shortcut.run() == 1
    assert "cannot install the desktop shortcut" in capsys.readouterr().err


def test_the_window_names_the_entry_and_carries_the_icon() -> None:
    """The window's desktop identity (#1021): the desktop file
    name the compositor matches — the app id a Wayland taskbar
    resolves the icon by — and the window icon itself, loaded
    from the master the wheel ships beside the module. The
    application name (#1043) sits beside them: macOS's menu
    bar titles the application menu after it, and a bare
    console script would name the interpreter there."""
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("PySide6 is not installed on this host")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtWidgets

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    app_mod.apply_identity(application)
    assert application.applicationName() == rows.WINDOW_TITLE
    assert application.desktopFileName() == shortcut.DESKTOP_ID
    assert not application.windowIcon().isNull()
