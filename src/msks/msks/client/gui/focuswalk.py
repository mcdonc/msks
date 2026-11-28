"""The tab strips' shared focus walk (#962, #1226): the first tab
stop under a page, for the strip whose Down hands the keyboard to
the page below — the main window's strip and the settings page's
subtab strip, one walk for both. The walk reads Qt's own focus
chain and changes nothing; the caller sets the focus it answers.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


def first_tab_stop(page: QtWidgets.QWidget):
    """The page's first tab stop, or None (#962): the focus chain
    walked from the page — depth-first, the page's own descendants
    before anything else — until a widget the Tab key would stop on.
    The walk stops at the page's edge: a stop outside the page (the
    strip itself, a later tab's content) is not the page's own, and
    the chain's wrap back to the page answers None."""
    seen = page.nextInFocusChain()
    while inside_walk(page, seen):
        if is_tab_stop(page, seen):
            return seen
        seen = seen.nextInFocusChain()
    return None


def inside_walk(page, seen) -> bool:
    """Whether the walk continues at this widget: a real widget,
    inside the page's subtree and not the page itself — the page is
    the chain's own wrap point, so the cycle back to it ends the
    walk (a page the chain can walk forever — a window with nothing
    focusable under it — hangs nothing)."""
    return seen is not None and seen is not page and page.isAncestorOf(seen)


def is_tab_stop(page, seen) -> bool:
    """Whether the walk's current widget is the page's own tab stop:
    inside the subtree, not the page itself, enabled (a dark field —
    a pinned setting's — is no stop, #1226), and carrying the Tab
    key's focus bit."""
    return (
        seen is not page
        and seen.isEnabled()
        and seen.focusPolicy() & QtCore.Qt.FocusPolicy.TabFocus
    )
