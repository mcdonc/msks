"""The GUI's desktop shortcut (#1021): the installer that
plants the window's icon and launcher entry where a Linux
desktop looks them up. A compositor resolves a window's
taskbar icon from the surface's app id — the window names
``msks`` (:func:`msks.client.gui.app.apply_identity` sets it) —
and the desktop entry by that name carries the ``Icon`` the
desktop shows. The installer writes both under the XDG data
home, from the icon assets the wheel ships beside this
module."""

import os
import shutil
import sys
from pathlib import Path

#: What the installer says on a system without the Linux
#: desktop conventions (#1021): the entry and the icon theme
#: are that desktop's own; macOS's dock reads the window icon
#: Qt already carries, and needs no entry planted.
NOT_LINUX = "msks: the desktop shortcut is a Linux desktop feature"

#: The desktop entry's own name (#1021): the file the
#: installer writes under the data home's applications tree.
#: The same word names the app id the window answers to and
#: the entry's ``StartupWMClass`` — the one name a compositor
#: matches the window, the entry, and the icon by.
DESKTOP_ID = "msks"

#: The desktop entry's file name, ``DESKTOP_ID`` with the
#: desktop-file suffix.
DESKTOP_FILE = f"{DESKTOP_ID}.desktop"

#: The scalable master's file name (#1021): the SVG the window
#: itself loads — Qt rasterizes it at any size — and the
#: installer copies into the icon theme's scalable slot.
ICON_FILE = f"{DESKTOP_ID}.svg"

#: The icon theme the installer plants under (#1021): hicolor
#: is the fallback theme every desktop reads.
HICOLOR = "hicolor"


def icon_dir() -> Path:
    """The wheel's icon assets (#1021): the directory this
    package ships them in — the master SVG the window loads,
    the desktop entry template, and the hicolor tree of PNG
    renders the installer copies."""
    return Path(__file__).resolve().parent / "icons"


def desktop_file_text(exe: str | None) -> str:
    """The desktop entry the installer writes (#1021): the
    shipped template, with its ``Exec`` line naming the
    absolute ``msks`` the session runs when one resolved on
    ``PATH``. The desktop runs the entry outside the shell
    that installed it, where a venv's bin directory may not
    sit on ``PATH``; a lookup that found nothing leaves the
    plain command the template ships."""
    text = (icon_dir() / DESKTOP_FILE).read_text()
    if exe is None:
        return text
    return text.replace(f"Exec={DESKTOP_ID} gui", f"Exec={exe} gui")


def install_shortcut(data_home: Path, exe: str | None) -> Path:
    """Plant the icon theme and the entry (#1021) under
    ``data_home`` and answer the entry's path. Every PNG the
    shipped hicolor tree carries lands under
    ``icons/hicolor``, the master SVG under that theme's
    scalable slot, and the entry under ``applications``. A
    second run overwrites in place — the install is
    idempotent, so an upgrade that ships new renders refreshes
    them with the same command."""
    source = icon_dir() / HICOLOR
    for png in sorted(source.glob("*x*/apps/*.png")):
        target = data_home / "icons" / png.relative_to(icon_dir())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png, target)
    scalable = data_home / "icons" / HICOLOR / "scalable" / "apps"
    scalable.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_dir() / ICON_FILE, scalable / ICON_FILE)
    entry = data_home / "applications" / DESKTOP_FILE
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(desktop_file_text(exe))
    return entry


def run() -> int:
    """The ``msks gui-shortcut`` command body (#1021): install
    the shortcut under the XDG data home — the variable's own
    value when set, else ``~/.local/share`` — and print the
    entry's path, the client's one-line convention. A desktop
    outside Linux hears it has no part in this, and a write
    the filesystem refuses names itself and fails the same
    way."""
    if not sys.platform.startswith("linux"):
        print(NOT_LINUX, file=sys.stderr)
        return 1
    data_home = Path(
        os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    )
    try:
        entry = install_shortcut(data_home, shutil.which(DESKTOP_ID))
    except OSError as exc:
        print(
            f"msks: cannot install the desktop shortcut: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"msks: the desktop shortcut is installed: {entry}")
    return 0
