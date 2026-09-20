"""The TUI's pure helpers, asserted outside any pilot (#195).

Kept in its own module on purpose: a worker that ran a Textual
pilot loses sysmon events for later code, so the direct assertions
live where the scheduler gives them a clean worker.
"""

from msks.client.tui import consent as consent_mod
from msks.client.tui.consent_app import (
    duration_label,
    ensure_focus,
    focused_rule_id,
    row_map,
)


class FakeChild:
    def __init__(self, rid, rule_id=None):
        self.request_id = rid
        self.rule_id = rule_id


class FakeRows:
    def __init__(self, children, highlighted=None, index=None):
        self.children = children
        self.highlighted_child = highlighted
        self.index = index


def test_duration_labels() -> None:
    tilrestart = consent_mod.ConsentRule(
        id="t",
        dest_host="h",
        dest_port=443,
        decision="allowed",
        duration="tilrestart",
        decided_at=1.0,
        decided_by=None,
    )
    assert duration_label(tilrestart, 5.0) == "until restart"
    forever = consent_mod.ConsentRule(
        id="f",
        dest_host="h",
        dest_port=0,
        decision="denied",
        duration="forever",
        decided_at=None,
        decided_by=None,
    )
    assert duration_label(forever, None) == "forever"


def test_row_and_focus_helpers() -> None:
    mapped = row_map(FakeRows([FakeChild("a"), FakeChild("b")]))
    assert sorted(mapped) == ["a", "b"]
    assert all(child.request_id == key for key, child in mapped.items())
    assert focused_rule_id(FakeRows([], None)) is None
    assert (
        focused_rule_id(FakeRows([FakeChild("x"), None], FakeChild("x", "r9")))
        == "r9"
    )
    empty = FakeRows([])
    ensure_focus(empty)
    assert empty.index is None  # nothing to focus
    rows = FakeRows([FakeChild("a")])
    ensure_focus(rows)
    assert rows.index == 0
    focused = FakeRows([FakeChild("a")], index=0)
    ensure_focus(focused)
    assert focused.index == 0  # kept, not reset
