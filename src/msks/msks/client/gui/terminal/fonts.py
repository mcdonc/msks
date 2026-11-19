"""The terminal's font rules (phase 4.5 of #924, #952).

Konsole's TerminalDisplay is the reference: the cell width is
the average advance over a representative ASCII run (robust
when the resolved face is not truly fixed-pitch), the cell
height is ascent plus descent capped at 1.2 em, and glyphs
draw baseline-anchored at the cell's left edge. One face is
measured and rendered — the same ``QFont`` object feeds the
metrics and the painter, and the family resolves from the
database before any measurement, never the style hint alone:
measuring one face and rendering another drifts narrow glyphs
and pads the lines (the klangk flterm lesson, mcdonc/klangk#2
— check the rasterized family, not just the metrics).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics

# The four bundled faces (JetBrains Mono, OFL 1.1 — the face
# Ghostty itself ships) beside this module: registering them
# first gives every host the same measured and rendered face,
# no system fonts required.
FACES = Path(__file__).resolve().parent / "faces"

# The resolved face, first match wins: the terminal faces in
# descending preference (Menlo is the macOS terminal face),
# with the database's own first fixed-pitch family and the
# generic hint as the floors.
PREFERRED = (
    "JetBrains Mono",
    "UbuntuMono Nerd Font Mono",
    "Fira Code",
    "Cascadia Mono",
    "Menlo",
    "DejaVu Sans Mono",
    "Liberation Mono",
)

# The run whose average advance is the cell width — Konsole's
# own representative characters.
REPCHAR = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgjijklmnopqrstuvwxyz0123456789./+@"

# Font-size stepping: Ctrl+Plus/Minus walk this window.
MIN_PT, MAX_PT = 7, 28
DEFAULT_PT = 15


def stepped(points: int, direction: int) -> int:
    """One zoom step's size (#976): the asked direction clamped
    to the stepping window."""
    return min(max(points + direction, MIN_PT), MAX_PT)


# The row-height cap: JetBrains Mono's natural line box is
# about 1.3 em; terminals ship exactly this trim as their
# line-height setting.
ROW_HEIGHT_EM = 1.2


@dataclass(frozen=True)
class CellMetrics:
    """One face's grid: how every cell lays out."""

    cell_width: int
    cell_height: int
    ascent: int

    def size(self, cols: int, rows: int) -> tuple[int, int]:
        """The pixel size a grid of this many cells takes."""
        return self.cell_width * cols, self.cell_height * rows


_FACES_SEATED = False


def register_faces() -> None:
    """Seat the bundled faces in the database, once per
    process — a host with no fonts at all (a CI runner's
    offscreen platform answers an empty database) still
    measures and renders one real face."""
    global _FACES_SEATED
    if _FACES_SEATED:
        return
    _FACES_SEATED = True
    for name in ("Regular", "Bold", "Italic", "BoldItalic"):
        path = FACES / f"JetBrainsMono-{name}.ttf"
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def resolve_family() -> str:
    """The family the database actually holds: the preference
    list in order, else the database's first fixed-pitch
    family, so the rasterizer draws the face the metrics
    measured (asking for a name the database lacks hands Qt a
    substitution — the spacing bug of mcdonc/klangk#2). The
    generic hint floor covers a host with no fixed-pitch face
    at all — never reached while the bundled faces register."""
    families = QFontDatabase.families()
    held = set(families)
    for name in PREFERRED:
        if name in held:
            return name
    for name in families:
        if QFontDatabase.isFixedPitch(name):
            return name
    return "Monospace"


def make_font(points: int = DEFAULT_PT) -> QFont:
    """The one face: resolved family, typewriter hint, the
    asked point size. The same object measures and renders."""
    font = QFont(resolve_family())
    font.setStyleHint(QFont.StyleHint.TypeWriter)
    font.setPointSize(points)
    return font


def em_pixels(font: QFont) -> int:
    """The em in pixels: what QFontInfo answers once the face
    is realized — exact at the platform's font dpi (72 on
    macOS, 96 most elsewhere) — with the 96-dpi logical
    estimate standing in while Qt still answers -1 (a font it
    has not realized; the M advance is narrower than the em
    square and the cap would cut the row under the
    baseline)."""
    realized = QFontInfo(font).pixelSize()
    if realized >= 0:
        return max(1, realized)
    return max(1, round(font.pointSizeF() * 96 / 72))


def measure(font: QFont) -> CellMetrics:
    """The grid a face lays out: average advance, ascent plus
    descent capped at 1.2 em, and the baseline offset glyphs
    anchor to."""
    metrics = QFontMetrics(font)
    width = max(1, round(metrics.horizontalAdvance(REPCHAR) / len(REPCHAR)))
    height = max(
        1, min(metrics.height(), round(em_pixels(font) * ROW_HEIGHT_EM))
    )
    return CellMetrics(width, height, metrics.ascent())


def variants(font: QFont) -> dict[str, QFont]:
    """The four style variants cells paint with, keyed the way
    a style tuple names them."""

    def variant(bold: bool, italic: bool) -> QFont:
        face = QFont(font)
        face.setBold(bold)
        face.setItalic(italic)
        return face

    return {
        "normal": variant(False, False),
        "bold": variant(True, False),
        "italic": variant(False, True),
        "bold_italic": variant(True, True),
    }


def font_key(bold: bool, italic: bool) -> str:
    """The variants-dict key for a style's flags — passing the
    bare string to ``setFont`` would silently install it as a
    family name and every glyph would render in Sans Serif."""
    if bold and italic:
        return "bold_italic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "normal"
