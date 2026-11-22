"""The wheel's icon assets (#1021, #1048): the directory this
package ships the icon masters and renders in — the SVG the
window loads, the desktop entry template, and the hicolor tree
of PNG renders the Linux shortcut and the macOS application
bundle both copy from. The seam stands alone so the two
installers share it without importing each other."""

from pathlib import Path


def icon_dir() -> Path:
    """The wheel's icon assets (#1021): the directory this
    package ships them in — the master SVG the window loads,
    the desktop entry template, and the hicolor tree of PNG
    renders the installer copies."""
    return Path(__file__).resolve().parent / "icons"
