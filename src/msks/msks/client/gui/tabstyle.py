"""The tab strips' shared active-tab mark (#1012): the
accent-tinted background the current tab carries on every strip
the window owns — the workspace detail page's own bar and the
main window's top-level strip — so the page the operator stands
on reads at a glance, not after a study. The mark's colors come
from the session palette (the accent is the palette's
Highlight), so a dark session and a light one each paint it in
their own accent, and the :class:`PaletteWatcher` recolors the
sheet when the session changes its palette under a live window.
The whole treatment is one stylesheet Qt's own engine paints —
the screens' existing idiom (the tables' bottom rules), no
custom drawing. This module imports PySide6 at module scope,
the screens' own contract: nothing imports it at CLI parse
time.
"""

from PySide6 import QtCore, QtGui, QtWidgets

#: The selected tab's background tint, alpha over the tab's own
#: base (#1012): the Highlight color at a fraction of its
#: strength — a shift the eye catches without a shout.
TINT_ALPHA = 66

#: The pointer's hover tint, alpha (#1012): the same accent,
#: weaker than the selected tab's — the tab the pointer crosses
#: leans toward the mark without wearing it. A lightness step
#: (the platform style's own hover idea) shifts too little to
#: read at any weight the palette carries.
HOVER_ALPHA = 32

#: The focused tab's dotted outline, pixels (#1012): the
#: keyboard's own mark. The sheet owns the whole tab paint, so
#: the focus ring the platform style drew is ours to draw back
#: or lose — the bar joins the tab chain (#962), and an arrow
#: landing on it must show where the keyboard stands.
FOCUS_PX = 1

#: The tab's padding, pixels (#1012): the stylesheet resets the
#: platform style's own spacing, so the sheet states its own —
#: comfortable beside the window's scaled fonts (#939).
PAD_X = 12
PAD_Y = 4

#: The tab's top corner radius, pixels (#1012): a soft corner
#: where the tab meets the content.
RADIUS_PX = 4


def mark_active(bar: QtWidgets.QTabBar) -> None:
    """Give one tab bar the active-tab mark (#1012): the
    stylesheet :func:`active_sheet` builds for the session
    palette, and the :class:`PaletteWatcher` that recolors it
    when the palette changes under the window. The bar keeps
    the platform style for everything the sheet does not name;
    the sheet names the tab and its states, and the states
    agree with the bar's geometry at every turn (the focus
    outline is reserved space, colors swapping only), so no tab
    moves as the selection or the focus crosses it."""
    sheet = active_sheet()
    if bar.styleSheet() != sheet:
        bar.setStyleSheet(sheet)
    if bar.findChild(PaletteWatcher) is None:
        PaletteWatcher(bar)


def rgba(color: QtGui.QColor) -> str:
    """One color as a stylesheet ``rgba()`` fill, the alpha
    the sheet's tints carry over the tab's base."""
    parts = (color.red(), color.green(), color.blue(), color.alpha())
    return f"rgba({parts[0]}, {parts[1]}, {parts[2]}, {parts[3]})"


def active_sheet() -> str:
    """The mark as a stylesheet string (#1012), colored from
    the session palette — the application's own, not the bar's:
    the sheet owns the bar's paint once applied, and a bar the
    stylesheet paints freezes its own palette, so the session
    is the one source that stays live. Every tab paints the
    palette's Button base, the selected tab paints the
    Highlight tint, the pointer's hover leans an unselected tab
    toward the accent at a weaker weight, and the keyboard's
    focus dots the tab's rim in the palette's own mid tone."""
    palette = QtGui.QGuiApplication.palette()
    accent = palette.color(QtGui.QPalette.ColorRole.Highlight)
    base = palette.color(QtGui.QPalette.ColorRole.Button)
    tint = QtGui.QColor(accent)
    tint.setAlpha(TINT_ALPHA)
    hover = QtGui.QColor(accent)
    hover.setAlpha(HOVER_ALPHA)
    mid = palette.color(QtGui.QPalette.ColorRole.Mid)
    return f"""
        QTabBar::tab {{
            background: {base.name()};
            border: {FOCUS_PX}px solid transparent;
            border-top-left-radius: {RADIUS_PX}px;
            border-top-right-radius: {RADIUS_PX}px;
            padding: {PAD_Y}px {PAD_X}px;
        }}
        QTabBar::tab:hover:!selected {{
            background: {rgba(hover)};
        }}
        QTabBar::tab:selected {{
            background: {rgba(tint)};
        }}
        QTabBar::tab:focus {{
            border: {FOCUS_PX}px dotted {mid.name()};
        }}
    """


class PaletteWatcher(QtCore.QObject):
    """The mark's palette follower (#1012): the colors
    :func:`active_sheet` bakes are the palette's own at apply
    time, and a session that changes its palette under a live
    window would leave the strips wearing the old accent. The
    watcher sits on the application, not the bar: the bar the
    stylesheet paints freezes its own palette, and the palette
    events never reach it — the session's change lands on the
    windows and the plain widgets, and the application sees
    every event of every object. The watcher's lifetime is the
    bar's (its parent), and a deleted Qt object unregisters its
    own filters, so teardown leaves nothing behind."""

    def __init__(self, bar: QtWidgets.QTabBar) -> None:
        super().__init__(bar)
        self.bar = bar
        QtCore.QCoreApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        kinds = (
            QtCore.QEvent.Type.PaletteChange,
            QtCore.QEvent.Type.ApplicationPaletteChange,
        )
        if event.type() in kinds:
            mark_active(self.bar)
        return False
