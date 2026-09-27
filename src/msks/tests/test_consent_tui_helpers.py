"""The TUI's pure helpers, asserted outside any pilot (#195).

Kept in its own module on purpose: a worker that ran a Textual
pilot loses sysmon events for later code, so the direct assertions
live where the scheduler gives them a clean worker.
"""

import json
from datetime import UTC, datetime

from msks.client.tui import consent as consent_mod
from msks.client.tui.consent_ui import (
    duration_label,
    ensure_focus,
    event_dest,
    event_item,
    event_line,
    event_time,
    events_note,
    focus_by_id,
    focus_event_by_id,
    focus_rule_by_id,
    focused_event_id,
    focused_rule_id,
    render_order,
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
    # A timed duration with no clock reading keeps its plain label.
    timed = consent_mod.ConsentRule(
        id="t5",
        dest_host="h",
        dest_port=443,
        decision="allowed",
        duration="5m",
        decided_at=None,
        decided_by=None,
    )
    assert duration_label(timed, None) == "5m"
    assert duration_label(timed, 42.0) == "42s left"


def test_row_and_focus_helpers() -> None:
    mapped = row_map(FakeRows([FakeChild("a"), FakeChild("b")]))
    assert sorted(mapped) == ["a", "b"]
    assert all(child.request_id == key for key, child in mapped.items())
    assert focused_rule_id(FakeRows([], None)) is None
    assert (
        focused_rule_id(FakeRows([FakeChild("x"), None], FakeChild("x", "r9")))
        == "r9"
    )
    focus_by_id(FakeRows([FakeChild("a"), FakeChild("b")]), "b")
    rows_b = FakeRows([FakeChild("a"), FakeChild("b")])
    focus_by_id(rows_b, "b")  # the target's own position
    assert rows_b.index == 1
    # The rules pair: the target's rule_id keeps its position, and
    # an absent target leaves a live highlight standing.
    rules = FakeRows(
        [FakeChild(None, rule_id="a1"), FakeChild(None, rule_id="d1")]
    )
    focus_rule_by_id(rules, "d1")
    assert rules.index == 1
    focus_rule_by_id(rules, "gone")
    assert rules.index == 1
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


def test_render_order_sorts_by_timestamp() -> None:
    """The render order is timestamp order with arrival breaking
    ties (#305): a live frame the socket delivered between two
    replayed rows renders in its time's place, not wherever the
    interleaving dropped it."""
    live = event(kind="swap", host="live.example", ts=99.5)
    replayed = event(kind="mint", ts=100.0)
    assert render_order([live, replayed]) == [live, replayed]
    assert render_order([replayed, live]) == [live, replayed]
    tie = event(kind="swap", ts=100.0, seq=2)
    assert render_order([tie, replayed]) == [replayed, tie]


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
    assert "every workspace's recorded mints" in note
    assert "as they happen" in note
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


# -- the daemon-wide audit posture (#390) ---------------------------------


def audit_listing_row(
    id: int,
    kind: str,
    *,
    workspaces: list[str] | None = None,
    name: str = "api",
    created_at: str = "2030-01-02T03:04:05",
) -> dict:
    """One REST audit row as the endpoint serves it."""
    return {
        "id": id,
        "kind": kind,
        "workspaces": list(workspaces or []),
        "name": name,
        "dests": ["api.example"],
        "created_at": created_at,
    }


def live_secret_frame(kind: str, **data) -> str:
    """One interceptor audit frame as the daemon publishes it."""
    payload = {"name": "api"}
    payload.update(data)
    return json.dumps({"event": f"secret.{kind}", "data": payload})


def test_watch_all_takes_every_frame() -> None:
    """A watch-all controller lands scoped foreign rows a
    workspace-scoped one filters out (#390): the daemon-wide view
    carries the whole daemon."""
    watcher = consent_mod.ConsentController(watch_all=True)
    scoped = live_secret_frame(
        "mint", workspace_id="ws-a", workspaces=["ws-a"]
    )
    assert watcher.apply_frame(scoped) == (
        consent_mod.SECRET_EVENT,
        watcher.events[0],
    )
    one_workspace = consent_mod.ConsentController(workspace_id="ws-b")
    assert one_workspace.apply_frame(scoped)[0] == consent_mod.IGNORED


def test_seed_audit_lands_rows_oldest_first() -> None:
    """The endpoint serves newest first; the seed lands oldest
    first behind any live tail — the row order the renderer's
    newest-first flip expects — with the coverage spellings and
    the epoch timestamp the live frames carry."""
    controller = consent_mod.ConsentController(watch_all=True)
    controller.seed_audit(
        [
            audit_listing_row(2, "revoke"),
            audit_listing_row(
                1,
                "mint",
                workspaces=["ws-a"],
                created_at="2030-01-01T03:04:05",
            ),
        ]
    )
    kinds = [row.kind for row in controller.events]
    assert kinds == ["mint", "revoke"]
    mint = controller.events[0]
    assert mint.workspace_id == "ws-a"
    assert mint.audit_id == 1
    assert mint.placeholder_id is None  # the record holds no placeholder id
    assert mint.ts == datetime(2030, 1, 1, 3, 4, 5, tzinfo=UTC).timestamp()
    assert controller.events[1].workspace_id == "*"


def test_seed_audit_dedups_the_live_tail() -> None:
    """A live frame that raced the read drops the replayed row's
    second delivery (#305 carried to the seed); a row nobody
    delivered live still lands."""
    controller = consent_mod.ConsentController(watch_all=True)
    controller.apply_frame(
        live_secret_frame(
            "mint", workspace_id="ws-a", workspaces=["ws-a"], audit_id=1
        )
    )
    controller.seed_audit(
        [
            audit_listing_row(2, "revoke"),
            audit_listing_row(1, "mint", workspaces=["ws-a"]),
        ]
    )
    assert [row.kind for row in controller.events] == ["mint", "revoke"]


def test_seed_audit_skips_unusable_rows() -> None:
    """A row without the kind-name pair lands nowhere — the frame
    parser's own rule, carried to the seed."""
    controller = consent_mod.ConsentController(watch_all=True)
    controller.seed_audit(
        [{"id": 1, "workspaces": [], "name": "api"}, "junk", None]
    )
    assert controller.events == []


def test_audit_stamp_epoch_reads_naive_utc_and_offsets() -> None:
    """The stored naive-UTC stamp converts as UTC; a stamped
    offset converts; junk reads 0.0 (the undated rendering)."""
    naive = consent_mod.audit_stamp_epoch("2030-01-01T03:04:05")
    assert naive == datetime(2030, 1, 1, 3, 4, 5, tzinfo=UTC).timestamp()
    offset = consent_mod.audit_stamp_epoch("2030-01-01T03:04:05+02:00")
    assert offset == datetime(2030, 1, 1, 1, 4, 5, tzinfo=UTC).timestamp()
    assert consent_mod.audit_stamp_epoch(None) == 0.0
    assert consent_mod.audit_stamp_epoch("junk") == 0.0
