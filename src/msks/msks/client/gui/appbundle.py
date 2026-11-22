"""The GUI's application bundle (#1048): the ``msks.app`` a
macOS user launches from the Dock, Launchpad, and Spotlight.

An unbundled ``msks gui`` process cannot name the application
menu, the Dock, or the Cmd-Tab switcher — AppKit reads all
three from the launching bundle's ``Info.plist``, and a console
script has none, so the desktop falls back to the interpreter's
executable name (#1043's probes pinned every unbundled lever as
dead). The bundle carries the name: ``CFBundleName`` msks, the
client's icon as an icns, and a wrapper whose interpreter
symlink keeps the exec'd process inside the bundle — the path
AppKit resolves — so the desktop names the client after the
bundle that launched it.

This module is Qt-free at parse time like every gui helper the
CLI parses (the suite's layering rule), stdlib-only beside the
shipped icon assets, and installs idempotently: a re-run
overwrites the wrapper, re-points the interpreter symlink at
the install that ran it, and rebuilds the icon."""

import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from ...spec.version import __version__
from .assets import icon_dir

#: The bundle's directory name and the names inside it (#1048):
#: the wrapper ``Contents/MacOS/msks`` the desktop execs, and
#: the interpreter symlink beside it — the pair that keeps the
#: process inside the bundle AppKit resolves.
BUNDLE_DIR = "msks.app"
EXEC_NAME = "msks"
PY_NAME = "python"

#: The bundle's identifier (#1048): the reverse-DNS form of the
#: project's home, the name Launchpad and Spotlight key the
#: bundle by.
IDENTIFIER = "io.github.mcdonc.msks"

#: The icon renders the icns build pairs (#1048): each iconset
#: slot takes the hicolor render of its size — the doubled
#: ``@2x`` slots the Retina sizes the wheel already ships.
ICONSET_SIZES = (
    ("icon_16x16.png", "16x16"),
    ("icon_16x16@2x.png", "32x32"),
    ("icon_32x32.png", "32x32"),
    ("icon_32x32@2x.png", "64x64"),
    ("icon_128x128.png", "128x128"),
    ("icon_128x128@2x.png", "256x256"),
    ("icon_256x256.png", "256x256"),
    ("icon_256x256@2x.png", "512x512"),
    ("icon_512x512.png", "512x512"),
)

#: The PNG the bundle carries when iconutil will not answer
#: (#1048): the largest render the wheel ships, still a valid
#: ``CFBundleIconFile`` the Finder and Dock read.
FALLBACK_ICON_SIZE = "512x512"

#: What the command says when no console script sits beside the
#: one running it (#1048): the bundle's wrapper needs the
#: install's own ``msks`` to launch.
NOT_BESIDE = (
    "msks: no msks command beside this one; run the install "
    "that shipped the GUI"
)


def python_path() -> str:
    """The wrapper's baked PYTHONPATH (#1048 review): the
    interpreter symlink the bundle execs resolves its prefix
    from the base build it points at, so the install's own
    modules ride along explicitly. The package's import root
    goes first — an editable dev install keeps the package in
    the source tree, and a PYTHONPATH entry runs no ``.pth``
    hooks — and the interpreter's site-packages rides second:
    a wheel install's two roots are the same directory, and
    the dependencies ride with either."""
    root = Path(__file__).resolve().parents[3]
    site = Path(sysconfig.get_paths()["purelib"])
    if root == site:
        return str(site)
    return f"{root}{os.pathsep}{site}"


def shell_quote(value: str) -> str:
    """One POSIX single-quoted word: a path the wrapper's own
    double-quoted ``exec`` line can carry whatever it holds."""
    return "'" + value.replace("'", "'\\''") + "'"


def wrapper_text(script: Path, pythonpath: str) -> str:
    """The bundle's executable (#1048): a shell wrapper that
    runs the install's own ``msks gui`` under the interpreter
    symlink beside it. The symlink is the naming half —
    exec'ing a path inside ``Contents/MacOS`` keeps the
    process AppKit resolves inside the bundle, so the name and
    icon read from this bundle's ``Info.plist``. The baked
    PYTHONPATH is the import half — the symlinked interpreter
    resolves its prefix from the base build it points at, so
    the site-packages the install carries ride along
    explicitly."""
    return (
        "#!/bin/sh\n"
        f"export PYTHONPATH={shell_quote(pythonpath)}\n"
        'exec "$(dirname "$0")/python" '
        f'{shell_quote(str(script))} gui "$@"\n'
    )


def numeric_version() -> str:
    """The version word's numeric prefix (#1048 review):
    LaunchServices wants ``CFBundleVersion`` numeric, so the
    dev markers between releases drop there; the full word
    stays in the short-version string beside it."""
    return ".".join(part for part in __version__.split(".") if part.isdigit())


def info_plist(icon_file: str) -> dict:
    """The bundle's ``Info.plist`` as a mapping (#1048): the
    name pair the menu bar, Dock, and switcher read, the
    wrapper and icon the bundle ships, and the client's own
    version word the About dialog already carries."""
    return {
        "CFBundleName": "msks",
        "CFBundleDisplayName": "msks",
        "CFBundleIdentifier": IDENTIFIER,
        "CFBundleVersion": numeric_version(),
        "CFBundleShortVersionString": __version__,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": EXEC_NAME,
        "CFBundleIconFile": icon_file,
        "NSHighResolutionCapable": True,
    }


def write_icon(resources: Path, runner) -> str:
    """The bundle's icon (#1048): an icns built from the
    shipped renders through the platform's own iconutil, or
    the largest PNG copied beside it when iconutil will not
    answer — the downgrade names itself on stderr, an
    absorbed failure the operator hears. The answer names the
    file the plist points at."""
    iconset = Path(tempfile.mkdtemp(prefix="msks-", suffix=".iconset"))
    try:
        for name, size in ICONSET_SIZES:
            render = icon_dir() / "hicolor" / size / "apps" / "msks.png"
            shutil.copyfile(render, iconset / name)
        done = runner(
            [
                "iconutil",
                "-c",
                "icns",
                str(iconset),
                "-o",
                str(resources / "msks.icns"),
            ],
            capture_output=True,
            timeout=30,
        )
        if done.returncode == 0 and (resources / "msks.icns").exists():
            return "msks.icns"
        print(
            "msks: iconutil did not answer; the bundle carries "
            "the shipped PNG icon",
            file=sys.stderr,
        )
    except FileNotFoundError, subprocess.SubprocessError, OSError:
        print(
            "msks: iconutil is missing; the bundle carries the "
            "shipped PNG icon",
            file=sys.stderr,
        )
    finally:
        shutil.rmtree(iconset, ignore_errors=True)
    shutil.copyfile(
        icon_dir() / "hicolor" / FALLBACK_ICON_SIZE / "apps" / "msks.png",
        resources / "msks.png",
    )
    return "msks.png"


def install_bundle(
    apps_dir: Path, python: Path, script: Path, runner=subprocess.run
) -> Path:
    """Plant the bundle under *apps_dir* (#1048) and answer its
    path. The wrapper, the interpreter symlink, the icon, and
    the plist land in one pass, and a second run overwrites in
    place — the install is idempotent, so an upgrade re-points
    the symlink at the interpreter that ran it."""
    contents = apps_dir / BUNDLE_DIR / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    exe = macos / EXEC_NAME
    exe.write_text(wrapper_text(script, python_path()))
    exe.chmod(0o755)
    py_link = macos / PY_NAME
    if py_link.is_symlink() or py_link.exists():
        py_link.unlink()
    py_link.symlink_to(python)
    # The icon arms sweep each other (#1048 review): a re-run
    # after an arm change leaves no stale icns beside the png
    # the plist now points at, and a red iconutil's partial
    # file never reads as the green arm's answer.
    for stale in ("msks.icns", "msks.png"):
        icon = resources / stale
        if icon.exists():
            icon.unlink()
    icon_file = write_icon(resources, runner)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(info_plist(icon_file))
    )
    return apps_dir / BUNDLE_DIR


def run() -> int:
    """The ``msks gui-shortcut`` command's darwin body (#1048):
    install the bundle under the home's Applications — the tree
    Launchpad reads beside the system's own — and print its
    path, the client's one-line convention. A run whose console
    script sits alone hears the stand-down line, and a write the
    filesystem refuses names itself and fails the same way."""
    script = Path(sys.argv[0]).resolve().parent / EXEC_NAME
    if not script.exists():
        print(NOT_BESIDE, file=sys.stderr)
        return 1
    try:
        bundle = install_bundle(
            Path.home() / "Applications",
            Path(sys.executable),
            script,
        )
    except (OSError, RuntimeError) as exc:
        print(
            f"msks: cannot install the application bundle: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"msks: the application bundle is installed: {bundle}")
    return 0
