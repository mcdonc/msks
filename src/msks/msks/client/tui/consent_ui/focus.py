"""Focus arithmetic over the ListView rows.

Reading which row holds focus, restoring focus by rule or event
id across re-renders, and the attribute the focused row carries.
"""

from __future__ import annotations

from textual.widgets import ListView


def focused_rule_or_none(screen) -> str | None:
    """The rules list's focused rule id, or None while absent (a
    rebuild's swap window) or nothing focused."""
    try:
        return focused_rule_id(screen.query_one("#rule-rows", ListView))
    except Exception:
        return None


def row_map(rows: ListView) -> dict:
    """The list's rows keyed by their request id."""
    return {
        getattr(child, "request_id", None): child for child in rows.children
    }


def row_ids(rows: ListView) -> set:
    """The ids of the rows currently in the list."""
    return {
        getattr(child, "request_id", None)
        for child in rows.children
        if getattr(child, "request_id", None) is not None
    }


def row_rule_ids(rows: ListView) -> list:
    """The rules list's row ids in order — the in-place repaint's
    membership check (order included: a reordered snapshot takes
    the fresh-list swap, never a text-only pass over moved rows)."""
    return [getattr(child, "rule_id", None) for child in rows.children]


def focused_request_id(rows: ListView | None) -> str | None:
    """The focused queue row's id, or None when nothing is focused
    (a None rows is a rebuild's swap window)."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, "request_id", None)


def focus_by_id(rows: ListView, target: str | None) -> None:
    """Highlight the row carrying *target* (the top when it is
    absent or None) — positions come from a freshly-built list, so
    every child under the index is real."""
    for position, child in enumerate(rows.children):
        if target is not None and getattr(child, "request_id", None) == target:
            rows.index = position
            return
    ensure_focus(rows)


def focused_rule_id(rows: ListView | None) -> str | None:
    """The focused rule row's id, or None when nothing is focused
    (a None rows is a rebuild's swap window)."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, "rule_id", None)


def ensure_focus(rows: ListView) -> None:
    """Give the list a highlight when it has rows but none focused —
    a hold is always decidable from the keyboard. The first row
    that takes it: a disabled row (a caption, #752) stands
    outside the walk and passes the highlight through."""
    if rows.index is None and rows.children:
        for position, child in enumerate(rows.children):
            if not child.disabled:
                rows.index = position
                return


def focused_attr(rows: ListView | None, attr: str):
    """The focused row's ``attr`` value, or None when nothing is
    focused (a None rows is a rebuild's swap window) — the tree's
    listing rebuild rule, carried beside the consent queue's own
    focus helpers."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, attr, None) if child is not None else None


def focus_attr(rows: ListView, attr: str, target) -> None:
    """Highlight the row carrying ``target`` (the top when it left
    or was never set) — positions come from a freshly-built list,
    so every child under the index is real."""
    for position, child in enumerate(rows.children):
        if target is not None and getattr(child, attr, None) == target:
            rows.index = position
            return
    ensure_focus(rows)


def focus_rule_by_id(rows: ListView, rule_id: str | None) -> None:
    """Highlight the rule row carrying *rule_id* (the top when it
    left or was never set) — a repaint must never move the target of
    a destructive key."""
    for position, child in enumerate(rows.children):
        if rule_id is not None and getattr(child, "rule_id", None) == rule_id:
            rows.index = position
            return
    ensure_focus(rows)


def focused_event_id(rows: ListView | None) -> int | None:
    """The focused event row's seq, or None when nothing is focused
    (a None rows is a rebuild's swap window)."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, "event_seq", None)


def focus_event_by_id(rows: ListView, target: int | None) -> None:
    """Highlight the event row carrying *target* (the top when it
    left or was never set) — a repaint must never move a row out
    from under a reading operator."""
    for position, child in enumerate(rows.children):
        if target is not None and getattr(child, "event_seq", None) == target:
            rows.index = position
            return
    ensure_focus(rows)
