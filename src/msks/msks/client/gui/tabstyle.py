"""The tab strips' shared active-tab mark (#1012): the accent
underline and tinted background the current tab carries on
every strip the window owns — the workspace detail page's own
bar and the main window's top-level strip — so the page the
operator stands on reads at a glance, not after a study. The
mark's colors come from the bar's own palette (the accent is
the palette's Highlight), so a dark session and a light one
each paint it in their own accent, and the whole treatment is
one stylesheet Qt's own engine paints — the screens' existing
idiom (the tables' bottom rules), no custom drawing. This
module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time.
"""

from PySide6 import QtGui, QtWidgets

#: The accent underline's weight, pixels (#1012): heavy enough
#: to read at a distance, thin enough to leave the bar its own
#: height.
LINE_PX = 3

#: The focused tab's dotted outline, pixels (#1012): the
#: keyboard's own mark. The sheet owns the whole tab paint, so
#: the focus ring the platform style drew is ours to draw back
#: or lose — the bar joins the tab chain (#962), and an arrow
#: landing on it must show where the keyboard stands.
FOCUS_PX = 1

#: The selected tab's background tint, alpha over the bar's own
#: base (#1012): the Highlight color at a fraction of its
#: strength — a shift the eye catches beside the underline
#: without a shout.
TINT_ALPHA = 66

#: The tab's padding, pixels (#1012): the stylesheet resets the
#: platform style's own spacing, so the sheet states its own —
#: comfortable beside the window's scaled fonts (#939).
PAD_X = 12
PAD_Y = 4

#: The tab's top corner radius, pixels (#1012): a soft corner
#: where the tab meets the content, the underline's own ends.
RADIUS_PX = 4


def mark_active(bar: QtWidgets.QTabBar) -> None:
    """Give one tab bar the active-tab mark (#1012): the
    stylesheet :func:`active_sheet` builds for the bar's own
    palette. The bar keeps the platform style for everything
    the sheet does not name; the sheet names the tab and its
    states, and the states agree with the bar's geometry at
    every turn (the underline and the focus outline are
    reserved space, colors swapping only), so no tab moves as
    the selection or the focus crosses it."""
    bar.setStyleSheet(active_sheet(bar))


def active_sheet(bar: QtWidgets.QTabBar) -> str:
    """The mark as a stylesheet string (#1012), colored from
    the bar's own palette: every tab paints the bar's Button
    base under a transparent underline reserve, the selected
    tab paints the Highlight tint above the accent underline,
    the pointer's hover lightens an unselected tab, and the
    keyboard's focus dots the tab's rim in the palette's own
    mid tone."""
    palette = bar.palette()
    accent = palette.color(QtGui.QPalette.ColorRole.Highlight)
    base = palette.color(QtGui.QPalette.ColorRole.Button)
    hover = base.lighter(115)
    tint = QtGui.QColor(accent)
    tint.setAlpha(TINT_ALPHA)
    rgba = f"rgba({tint.red()}, {tint.green()}, {tint.blue()}, {tint.alpha()})"
    mid = palette.color(QtGui.QPalette.ColorRole.Mid)
    return f"""
        QTabBar::tab {{
            background: {base.name()};
            border: {FOCUS_PX}px solid transparent;
            border-bottom: {LINE_PX}px solid transparent;
            border-top-left-radius: {RADIUS_PX}px;
            border-top-right-radius: {RADIUS_PX}px;
            padding: {PAD_Y}px {PAD_X}px;
        }}
        QTabBar::tab:hover:!selected {{
            background: {hover.name()};
        }}
        QTabBar::tab:selected {{
            background: {rgba};
            border-bottom: {LINE_PX}px solid {accent.name()};
        }}
        QTabBar::tab:focus {{
            border: {FOCUS_PX}px dotted {mid.name()};
            border-bottom: {LINE_PX}px solid {accent.name()};
        }}
    """
