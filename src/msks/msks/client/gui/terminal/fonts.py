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

from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics

# The resolved face, first match wins: the terminal faces in
# descending preference, with the generic hint as the floor.
PREFERRED = (
    "JetBrains Mono",
    "UbuntuMono Nerd Font Mono",
    "Fira Code",
    "Cascadia Mono",
    "DejaVu Sans Mono",
    "Liberation Mono",
    "Monospace",
)

# The run whose average advance is the cell width — Konsole's
# own representative characters.
REPCHAR = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgjijklmnopqrstuvwxyz0123456789./+@"

# Font-size stepping: Ctrl+Plus/Minus walk this window.
MIN_PT, MAX_PT = 7, 28
DEFAULT_PT = 15

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


def resolve_family() -> str:
    """The family the database actually holds, from the
    preference list in order — the generic hint only when
    nothing else matched."""
    families = set(QFontDatabase.families())
    for name in PREFERRED:
        if name in families:
            return name
    return PREFERRED[-1]


def make_font(points: int = DEFAULT_PT) -> QFont:
    """The one face: resolved family, typewriter hint, the
    asked point size. The same object measures and renders."""
    font = QFont(resolve_family())
    font.setStyleHint(QFont.StyleHint.TypeWriter)
    font.setPointSize(points)
    return font


def measure(font: QFont) -> CellMetrics:
    """The grid a face lays out: average advance, ascent plus
    descent capped at 1.2 em, and the baseline offset glyphs
    anchor to."""
    metrics = QFontMetrics(font)
    width = max(1, round(metrics.horizontalAdvance(REPCHAR) / len(REPCHAR)))
    em = QFontInfo(font).pixelSize()
    height = max(1, min(metrics.height(), round(em * ROW_HEIGHT_EM)))
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
