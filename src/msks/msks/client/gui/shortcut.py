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

#: The characters the desktop entry spec reserves in an
#: ``Exec`` value (#1021): a resolved path that carries one
#: would need the spec's own quoting, and the entry keeps the
#: plain command instead of a line the desktop could misparse.
RESERVED = set(" \t\n\"'`$><|&*?#()\\;~%=")

#: What the installer says when ``XDG_DATA_HOME`` carries a
#: relative path (#1021): the spec wants an absolute one, and
#: a relative value would plant the tree under whatever the
#: working directory happened to be.
NOT_ABSOLUTE = "msks: XDG_DATA_HOME must be an absolute path"


def icon_dir() -> Path:
    """The wheel's icon assets (#1021): the directory this
    package ships them in — the master SVG the window loads,
    the desktop entry template, and the hicolor tree of PNG
    renders the installer copies."""
    return Path(__file__).resolve().parent / "icons"


def usable_exe(exe: str) -> bool:
    """Whether the entry can carry the resolved command
    verbatim (#1021): the desktop entry spec wants the
    executable a full path or a bare name, and a path with a
    reserved character would need the spec's own quoting.
    Both answers keep the plain ``msks`` the template ships."""
    return Path(exe).is_absolute() and not RESERVED & set(exe)


def desktop_file_text(exe: str | None) -> str:
    """The desktop entry the installer writes (#1021): the
    shipped template, with its ``Exec`` line naming the
    ``msks`` the session resolved on ``PATH``. The desktop
    runs the entry outside the shell that installed it, where
    a venv's bin directory may not sit on ``PATH``. A lookup
    that found nothing — or a path the entry cannot carry
    verbatim — leaves the plain command the template ships,
    and the rewrite lands on whatever ``Exec`` line the
    template carries, not on one line's exact spelling."""
    text = (icon_dir() / DESKTOP_FILE).read_text()
    if exe is None or not usable_exe(exe):
        return text
    kept = []
    for line in text.splitlines():
        if not line.startswith("Exec="):
            kept.append(line)
            continue
        # The template's own arguments stand: the rewrite
        # swaps the executable alone, whatever command the
        # template's line carries.
        kept.append(" ".join([f"Exec={exe}", *line.split()[1:]]))
    return "\n".join(kept) + "\n"


def install_shortcut(data_home: Path, exe: str | None) -> Path:
    """Plant the icon theme and the entry (#1021) under
    ``data_home`` and answer the entry's path. Every PNG the
    shipped hicolor tree carries lands under
    ``icons/hicolor``, the master SVG under that theme's
    scalable slot, and the entry under ``applications``. A
    second run overwrites in place — the install is
    idempotent, so an upgrade refreshes the renders it ships
    with the same command."""
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


def data_home() -> Path:
    """The XDG data home (#1021, #1045): the variable's own
    value when set, else ``~/.local/share`` — the tree the
    command and the launch plant both write under."""
    return Path(
        os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    )


def exec_exe(line: str) -> str | None:
    """The executable one standing ``Exec`` line carries
    (#1045): the first field after the key, when the line has
    one and its path can carry verbatim — else ``None``, the
    answer that leaves the template's plain command."""
    parts = line.removeprefix("Exec=").split()
    if parts and usable_exe(parts[0]):
        return parts[0]
    return None


def standing_exe(entry: Path) -> str | None:
    """The executable the standing entry carries (#1045),
    read from its ``Exec`` line: the launch's last resolution
    tier. A session that resolves no client — the desktop's
    ``PATH`` is not the launching shell's — must not downgrade
    an entry that names a working client to the bare command
    the desktop cannot run."""
    try:
        lines = entry.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("Exec="):
            continue
        return exec_exe(line)
    return None


def launch_exe(entry: Path) -> str | None:
    """The executable the launch replants the entry's ``Exec``
    line with (#1045), in tiers: the running command itself
    when the entry's own absolute launch answers for it — a
    launch from the desktop runs the path the entry carries,
    and the replant writes the same path back — else the
    ``PATH`` lookup the command uses, else the executable the
    standing entry already carries."""
    if (
        sys.argv
        and Path(sys.argv[0]).name == DESKTOP_ID
        and usable_exe(sys.argv[0])
    ):
        return sys.argv[0]
    return shutil.which(DESKTOP_ID) or standing_exe(entry)


def ensure_installed() -> None:
    """Plant the shortcut at GUI startup (#1045): the wheel
    cannot run a step after pip installs it — pip runs console
    scripts, not install hooks — so the launch performs the
    install-time plant: every ``msks gui`` on a Linux desktop
    plants the entry and the renders before the window maps,
    the moment a compositor first resolves the taskbar icon,
    and an upgrade's next launch refreshes the renders with
    the same idempotent install the command performs. The
    plant is best-effort: a desktop outside Linux hears
    nothing (its dock reads the window icon Qt already
    carries), and a refusal — a data home the filesystem will
    not write, a relative ``XDG_DATA_HOME``, a home the
    platform will not vouch for — says one line on stderr and
    the window still opens."""
    if not sys.platform.startswith("linux"):
        return
    try:
        home = data_home()
        if not home.is_absolute():
            print(NOT_ABSOLUTE, file=sys.stderr)
            return
        install_shortcut(
            home, launch_exe(home / "applications" / DESKTOP_FILE)
        )
    except (OSError, RuntimeError) as exc:
        print(
            f"msks: cannot install the desktop shortcut: {exc}",
            file=sys.stderr,
        )


def run() -> int:
    """The ``msks gui-shortcut`` command body (#1021): install
    the shortcut under the XDG data home — the variable's own
    value when set, else ``~/.local/share`` — and print the
    entry's path, the client's one-line convention. A desktop
    outside Linux hears it has no part in this, a relative
    data home hears the absolute-path rule, and a write the
    filesystem refuses names itself and fails the same way."""
    if not sys.platform.startswith("linux"):
        print(NOT_LINUX, file=sys.stderr)
        return 1
    try:
        home = data_home()
        if not home.is_absolute():
            print(NOT_ABSOLUTE, file=sys.stderr)
            return 1
        entry = install_shortcut(home, shutil.which(DESKTOP_ID))
    except (OSError, RuntimeError) as exc:
        print(
            f"msks: cannot install the desktop shortcut: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"msks: the desktop shortcut is installed: {entry}")
    return 0
