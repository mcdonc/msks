"""The TUI's pure helpers, asserted outside any pilot (#195).

Kept in its own module on purpose: a worker that ran a Textual
pilot loses sysmon events for later code, so the direct assertions
live where the scheduler gives them a clean worker.
"""

from msks.client.tui import consent as consent_mod
from msks.client.tui.consent_app import (
    duration_label,
    ensure_focus,
    event_dest,
    event_item,
    event_line,
    event_time,
    events_note,
    focus_event_by_id,
    focused_event_id,
    focused_rule_id,
    row_map,
    sighting_flash,
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


def event(kind: str = "swap", **kw) -> consent_mod.SecretEvent:
    """One SecretEvent with test-friendly defaults."""
    fields = {
        "seq": 1,
        "kind": kind,
        "workspace_id": "ws-a",
        "name": "api",
        "placeholder_id": 4,
        "host": "api.example.com",
        "dests": (),
        "ts": 1_700_000_000.0,
    }
    fields.update(kw)
    return consent_mod.SecretEvent(**fields)


def test_event_time_labels() -> None:
    """A dated event renders its local clock; an undated one (an
    older daemon) keeps a blank column."""
    assert event_time(0.0) == ""
    assert event_time(1_700_000_000.0) != ""


def test_event_dest_text() -> None:
    """The wire host wins over the mint's allowlist; the exit kinds
    carry no destination."""
    both = event(dests=("a.example",))
    assert event_dest(both) == " → api.example.com"
    assert event_dest(event(kind="mint", host=None, dests=("a.example",))) == (
        " → a.example"
    )
    assert event_dest(event(kind="revoke", host=None)) == ""


def test_event_line_marks_the_sighting() -> None:
    """The sighting's row carries the ``!`` marker; every other
    kind carries a blank; operator-supplied text renders escaped;
    the placeholder's row id rides its row."""
    line = event_line(event(kind="sighting", host="evil.example"))
    assert line.startswith("! ")
    assert "ws-a/api#4" in line
    assert "evil.example" in line
    assert event_line(event()).startswith("  ")
    # An undated frame keeps its column blank, not "1970".
    assert "1970" not in event_line(event(ts=0.0))
    # A frame without an id (an older daemon) keeps no suffix.
    assert "#" not in event_line(event(placeholder_id=None))


def test_event_item_carries_the_highlight_class() -> None:
    """Only the sighting row takes the ``sighting`` class; every
    row carries its seq for focus restoration."""
    marked = event_item(event(kind="sighting"))
    assert "sighting" in marked.classes
    assert marked.event_seq == 1
    plain = event_item(event(kind="mint"))
    assert "sighting" not in plain.classes


def test_events_note_and_sighting_flash() -> None:
    """The note names the marker, the recorded/live split, and the
    detection boundary; the flash names the workspace, placeholder,
    and host."""
    note = events_note()
    assert "!" in note and "decrypted" in note
    assert "recorded mints" in note and "as they happen" in note
    flash = sighting_flash(event(kind="sighting", host=None))
    assert flash == "! sighting: ws-a/api → ?"
    assert sighting_flash(event(kind="sighting")) == (
        "! sighting: ws-a/api → api.example.com"
    )


def test_event_focus_helpers() -> None:
    """The events screen's focus pair mirrors the rules': the
    focused seq reads back, an absent target falls to the top, and
    None stays None."""
    assert focused_event_id(FakeRows([], None)) is None
    seq_child = FakeChild(None)
    seq_child.event_seq = 5
    assert focused_event_id(FakeRows([seq_child], seq_child)) == 5
    rows = FakeRows([seq_child])
    focus_event_by_id(rows, 5)
    assert rows.index == 0
    focus_event_by_id(rows, None)  # absent target: the top
    assert rows.index == 0
    empty = FakeRows([])
    focus_event_by_id(empty, 5)  # nothing to focus
    assert empty.index is None
