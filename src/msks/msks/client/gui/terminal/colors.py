"""The terminal's color rules (#1006, phase 6 of #924): the
two color sets a desktop's scheme picks between. The dark set
is the library's own default theme — its built-in palette with
the background and foreground the palette's own ends name. The
light set is white with a black face, the library's palette
generator deriving the cube and the grayscale ramp to match
the pair.

Each set lands on a terminal as its *default* colors — the
layer a program's OSC 10/11/4 overrides sit above (the
library's own two-layer design): a scheme switch repaints the
grid without erasing a program's own colors.

The module imports no Qt: the scheme values are the library's
own light/dark constants, and the repaint is the screen's own
refresh.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

from .. import ghostty as gt

#: The light grid's pair (#1006): a white page with a black
#: face — the pairing the chrome's light base paints (Fusion's
#: own standard palette carries a white Base).
LIGHT_BACKGROUND = (255, 255, 255)
LIGHT_FOREGROUND = (0, 0, 0)


@dataclass(frozen=True)
class TerminalColors:
    """One scheme's grid: the default background and
    foreground beside the whole 256-color palette."""

    background: tuple[int, int, int]
    foreground: tuple[int, int, int]
    palette: ctypes.Array


def triple(color: gt.ColorRgb) -> tuple[int, int, int]:
    """One library color as a plain RGB triple."""
    return (color.r, color.g, color.b)


def rgb(values: tuple[int, int, int]) -> gt.ColorRgb:
    """A plain RGB triple as the library's color."""
    color = gt.ColorRgb()
    color.r, color.g, color.b = values
    return color


def build(scheme: int, lib) -> TerminalColors:
    """One scheme's color set from the library's own helpers:
    the dark set the built-in default palette names at its own
    ends, the light set the generator matches to the white
    pair (the 216-color cube and the grayscale ramp derived
    from the new background and foreground, the base sixteen
    kept)."""
    base = (gt.ColorRgb * 256)()
    lib.color_palette_default(base)
    if scheme == gt.COLOR_SCHEME_DARK:
        return TerminalColors(
            triple(base[0]), triple(base[7]), base
        )
    palette = (gt.ColorRgb * 256)()
    lib.color_palette_generate(
        base,
        ctypes.byref(gt.PaletteMask()),
        ctypes.byref(rgb(LIGHT_BACKGROUND)),
        ctypes.byref(rgb(LIGHT_FOREGROUND)),
        True,
        palette,
    )
    return TerminalColors(LIGHT_BACKGROUND, LIGHT_FOREGROUND, palette)


# The built sets, keyed by scheme: the library's answers are
# pure functions of the inputs, so one build serves every
# terminal and every switch.
_SETS: dict[int, TerminalColors] = {}


def colors_for(scheme: int, lib) -> TerminalColors:
    """One scheme's color set, built on first ask and shared
    after — every terminal the scheme reaches takes the same
    values."""
    held = _SETS.get(scheme)
    if held is None:
        held = build(scheme, lib)
        _SETS[scheme] = held
    return held


def apply(screen, scheme: int) -> None:
    """One scheme's color set on one live terminal (#1006):
    the defaults written — background, foreground, palette —
    and the grid refreshed, so the paint takes the new colors
    at once. A program's own OSC overrides keep winning over
    the new defaults (the library's two layers); the scheme
    also stands on the screen as the answer its color-scheme
    query reports."""
    colors = colors_for(scheme, screen.lib)
    screen.lib.terminal_set(
        screen.terminal,
        gt.OPT_COLOR_BACKGROUND,
        ctypes.byref(rgb(colors.background)),
    )
    screen.lib.terminal_set(
        screen.terminal,
        gt.OPT_COLOR_FOREGROUND,
        ctypes.byref(rgb(colors.foreground)),
    )
    screen.lib.terminal_set(
        screen.terminal, gt.OPT_COLOR_PALETTE, colors.palette
    )
    screen.color_scheme = scheme
    screen.refresh()
