"""The Egress tab's audit trail (#1051): the consent rows over
a stub bridge — the read painting the table newest first with
its destination, decision, duration, and wall-clock cells, the
filter's pick cancelling the standing read and reflying under
the picked decision, the resolved frame's poke, the selection
gating the deny-to-allowlist action and its emission, and the
workspace switch cancelling and blanking. The widgets run under
the offscreen platform on every host.
"""

import importlib.util
import os
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client.ages import clock_label  # noqa: E402
from msks.client.gui import egressaudit as audit_mod  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402

#: One recorded consent row as the daemon's listing serves it
#: (the model's row_dict shape), newest first.
ROW_NEW = {
    "id": "req-2",
    "workspace_id": "ws-a",
    "dest_host": "denied.example",
    "dest_port": 443,
    "decision": "denied",
    "duration": None,
    "requested_at": 2000.0,
    "decided_at": None,
    "decided_by": None,
}
ROW_OLD = {
    "id": "req-1",
    "workspace_id": "ws-a",
    "dest_host": "allowed.example",
    "dest_port": 0,
    "decision": "allowed",
    "duration": "forever",
    "requested_at": 1000.0,
    "decided_at": 1001.0,
    "decided_by": "token",
}


class FakeSignal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once, in the GUI thread
    the real signal's queued delivery would land them in."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class StubBridge:
    """The bridge's surface for the trail's tests: the two
    signals it reads and the one verb it calls, every call
    recorded, every flight a real future — the trail holds it
    for the cancel a workspace switch takes."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object):
        self.calls.append((key, method, args))
        return Future()


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the trail needs, the
    dialog tests' own fixture shape (#962)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(app=app, QtWidgets=QtWidgets, keep=[])
    yield ns
    del ns.keep[:]
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def make_trail(qt) -> tuple:
    """The trail over the stub bridge, hosted on the workspace
    with its first read landed and the calls cleared."""
    bridge = StubBridge()
    trail = audit_mod.AuditTrail(bridge)
    qt.keep.append(trail)
    trail.host("ws-a")
    trail.poke()
    bridge.succeeded.emit(trail.key, [ROW_NEW, ROW_OLD])
    bridge.calls.clear()
    return trail, bridge


def table_cells(trail) -> list[list[str]]:
    """The table's rows as text — the cells the operator
    reads."""
    return [
        [
            trail.table.item(line, column).text()
            for column in range(trail.table.columnCount())
        ]
        for line in range(trail.table.rowCount())
    ]


def pick_filter(trail, choice: str) -> None:
    """The operator's pick on the filter: the index moves and
    the combo's own ``activated`` signal fires."""
    index = trail.filter.findText(choice)
    trail.filter.setCurrentIndex(index)
    trail.filter.activated.emit(index)


def test_row_text_carries_the_cells() -> None:
    """One row's cells (#1051): the destination label with port
    0 reading as all ports, the decision, the duration with the
    dash stand-in, and the requested stamp's wall-clock label —
    the shared leaf's own spelling, blank on a stamp the row did
    not carry."""
    assert audit_mod.dest_text({"dest_host": "h", "dest_port": 0}) == (
        "h (all ports)"
    )
    assert audit_mod.dest_text({"dest_host": "h", "dest_port": None}) == (
        "h (all ports)"
    )
    assert audit_mod.dest_text({"dest_host": "h", "dest_port": 443}) == "h:443"
    assert audit_mod.row_text(ROW_OLD) == (
        "allowed.example (all ports)",
        "allowed",
        "forever",
        clock_label(1000.0),
    )
    assert audit_mod.row_text(ROW_NEW)[2] == "-"
    undated = dict(ROW_NEW, requested_at=None)
    assert audit_mod.row_text(undated)[3] == ""


def test_the_read_paints_the_rows_newest_first(qt) -> None:
    """The trail's read (#1051): the landed rows paint the
    table in the order the listing serves them — newest first —
    the empty state's stand-in hidden under rows and shown
    under none, and an answer that is not a list blanking the
    table the tab's own tolerance."""
    trail, bridge = make_trail(qt)
    assert table_cells(trail) == [
        [
            "denied.example:443",
            "denied",
            "-",
            clock_label(2000.0),
        ],
        [
            "allowed.example (all ports)",
            "allowed",
            "forever",
            clock_label(1000.0),
        ],
    ]
    assert not trail.empty.isVisibleTo(trail)
    bridge.succeeded.emit(trail.key, [])
    assert trail.table.rowCount() == 0
    assert trail.empty.isVisibleTo(trail)
    bridge.succeeded.emit(trail.key, "not a list")
    assert trail.table.rowCount() == 0


def test_the_read_skips_junk_members(qt) -> None:
    """A listing with unusable members (#1051): the dicts paint,
    the junk between them drops, and the rows the table shows
    are the rows the actions read."""
    trail, bridge = make_trail(qt)
    bridge.succeeded.emit(trail.key, [ROW_OLD, "junk", None])
    assert trail.table.rowCount() == 1
    assert trail.rows == [ROW_OLD]


def test_the_filter_picks_the_query_and_reflies(qt) -> None:
    """The filter's pick (#1051): the unfiltered entry reads
    every decision (the query omitted), a picked decision flies
    in the query beside the limit, and the pick cancels the
    standing read — armed for the decision the table last
    shows — and flies again at once, a read with none standing
    flying straight. A poke already pending guards itself, the
    listing's own rule."""
    trail, bridge = make_trail(qt)
    assert trail.decision() is None
    trail.poke()
    assert bridge.calls == [
        (trail.key, "egress_requests", ("ws-a", None, 100))
    ]
    pick_filter(trail, "denied")
    assert bridge.calls[-1] == (
        trail.key,
        "egress_requests",
        ("ws-a", "denied", 100),
    )
    trail.poke()  # the refly's read is out: guarded
    assert len(bridge.calls) == 2
    bridge.succeeded.emit(trail.key, [])  # no flight standing
    pick_filter(trail, "all")
    assert bridge.calls[-1] == (
        trail.key,
        "egress_requests",
        ("ws-a", None, 100),
    )


def test_a_programmatic_filter_change_decides_nothing(qt) -> None:
    """The filter's follow shapes (#1051): a programmatic index
    change — nothing the operator picked — sends no read; the
    next poke alone flies under the moved entry."""
    trail, bridge = make_trail(qt)
    trail.filter.setCurrentIndex(3)  # "denied", no activation
    assert bridge.calls == []
    assert trail.decision() == "denied"  # the read would use it
    trail.poke()
    assert bridge.calls == [
        (trail.key, "egress_requests", ("ws-a", "denied", 100))
    ]


def test_a_poke_before_a_host_flies_nothing(qt) -> None:
    """The unhosted trail (#1051): no workspace stands, and the
    poke and the refly both guard."""
    bridge = StubBridge()
    trail = audit_mod.AuditTrail(bridge)
    qt.keep.append(trail)
    trail.poke()
    trail.refly()
    assert bridge.calls == []


def test_another_keys_answer_is_not_the_trails_to_take(qt) -> None:
    """The bridge is shared (#1051): a foreign key's answer and
    a foreign key's failure change nothing."""
    trail, bridge = make_trail(qt)
    bridge.succeeded.emit("egress-audit-99", [])
    bridge.failed.emit("detail-row", "not this trail's")
    assert trail.table.rowCount() == 2
    assert trail.note.text() == ""


def test_a_refusal_names_itself_and_the_next_poke_flies(
    qt,
) -> None:
    """A refused read (#1051): the route's own words land on the
    note line, the rows stand, and the next poke flies once the
    refusal cleared the pending flag."""
    trail, bridge = make_trail(qt)
    bridge.failed.emit(trail.key, "the daemon is gone")
    assert trail.note.text() == "refused: the daemon is gone"
    assert trail.table.rowCount() == 2
    trail.poke()
    assert [c for c in bridge.calls if c[1] == "egress_requests"]


def test_a_resolved_frame_pokes_the_read(qt) -> None:
    """The pushed frame (#1051): a resolved verdict — the
    trail's newest row — flies the read again; a foreign
    workspace's resolution and any other frame change
    nothing."""
    trail, bridge = make_trail(qt)
    trail.push(
        {
            "event": "egress.resolved",
            "data": {"workspace_id": "ws-a", "request_id": "req-2"},
        }
    )
    assert [c for c in bridge.calls if c[1] == "egress_requests"]
    bridge.succeeded.emit(trail.key, [ROW_NEW])
    trail.push(
        {
            "event": "egress.resolved",
            "data": {"workspace_id": "ws-b", "request_id": "req-9"},
        }
    )
    trail.push({"event": "egress.request", "data": {"workspace_id": "ws-a"}})
    trail.push(
        {
            "event": "egress.resolved",
            "data": {"workspace_id": "ws-a", "request_id": "req-3"},
        }
    )  # the read is out: the poke guards
    assert len(bridge.calls) == 2


def test_the_selection_gates_the_allowlist_button(qt) -> None:
    """The action's state (#1051): the Allowlist button on a
    selected denial row — the denied row alone, the allowed row
    and no row answering nothing."""
    trail, _bridge = make_trail(qt)
    assert not trail.allow_button.isEnabled()
    trail.table.setCurrentCell(1, 0)  # the allowed row
    assert not trail.allow_button.isEnabled()
    trail.table.setCurrentCell(0, 0)  # the denied row
    assert trail.allow_button.isEnabled()


def test_allow_emits_the_denied_name(qt) -> None:
    """The deny-to-allowlist action (#1051): a selected denial
    row's name crosses on ``allow_wanted`` for the tab's own
    write path; an allowed row and a nameless row decide
    nothing."""
    trail, _bridge = make_trail(qt)
    heard: list[str] = []
    trail.allow_wanted.connect(heard.append)
    trail.table.setCurrentCell(1, 0)  # allowed: no emission
    trail.allow()
    assert heard == []
    trail.table.setCurrentCell(0, 0)  # denied: the name crosses
    trail.allow()
    assert heard == ["denied.example"]
    trail.rows = [dict(ROW_NEW, dest_host="")]  # nameless denial
    trail.allow()
    assert heard == ["denied.example"]


def test_the_selected_row_beyond_the_rows_reads_none(qt) -> None:
    """The selection's guard (#1051): a selection the shrunken
    rows no longer hold — the state a direct shrink leaves —
    and no selection at all each read as no row."""
    trail, _bridge = make_trail(qt)
    trail.table.setCurrentCell(0, 0)
    trail.rows = []  # the selection outlives a direct shrink
    assert trail.selected_row() is None
    trail.table.clearSelection()
    assert trail.selected_row() is None


def test_a_repaint_that_changes_nothing_keeps_the_selection(
    qt,
) -> None:
    """The repaint guard (#1051): an answer whose rows the table
    already shows rebuilds nothing — the selection the
    Allowlist action reads survives the page's cadence — and an
    answer that changes the rows rebuilds the table, the
    selection going with it."""
    trail, bridge = make_trail(qt)
    trail.table.setCurrentCell(0, 0)
    assert trail.allow_button.isEnabled()
    bridge.succeeded.emit(trail.key, [ROW_NEW, ROW_OLD])
    assert trail.table.currentRow() == 0
    bridge.succeeded.emit(trail.key, [ROW_OLD])
    assert trail.table.currentRow() == -1
    assert not trail.allow_button.isEnabled()


def test_a_workspace_switch_cancels_and_blanks(qt) -> None:
    """The open's reset (#1051): a workspace switch cancels the
    read the old workspace still had out — its answer is not the
    new workspace's to take — blanks the table, and lets the new
    workspace's own read fill it. A done flight's cancel is a
    no-op, and the same workspace re-hosted changes nothing."""
    trail, bridge = make_trail(qt)
    trail.poke()  # a read the switch cancels mid-flight
    stale = trail.key
    trail.host("ws-b")
    assert trail.table.rowCount() == 0
    assert trail.empty.isVisibleTo(trail)
    bridge.succeeded.emit(stale, [ROW_NEW])
    assert trail.table.rowCount() == 0
    trail.poke()
    assert bridge.calls[-1] == (
        trail.key,
        "egress_requests",
        ("ws-b", None, 100),
    )
    bridge.succeeded.emit(trail.key, [ROW_OLD])
    done = Future()
    done.set_result(None)
    trail.flight = done
    trail.host("ws-c")  # the done flight's cancel is a no-op
    trail.poke()
    bridge.succeeded.emit(trail.key, [ROW_NEW])
    assert table_cells(trail)[0][0] == "denied.example:443"
    trail.host("ws-c")  # the same workspace: the trail stands
    assert trail.table.rowCount() == 1


def test_a_first_read_with_no_rows_shows_the_stand_in(qt) -> None:
    """The empty stand-in on the first read (#1051 review): a
    trail whose first landed read carries no rows builds nothing
    — and the stand-in still says itself, the discovery state
    the trail advertises. The hosted trail with nothing read
    shows it too: the blanked table is the honest state."""
    bridge = StubBridge()
    trail = audit_mod.AuditTrail(bridge)
    qt.keep.append(trail)
    trail.host("ws-a")
    assert trail.empty.isVisibleTo(trail)  # nothing read yet
    trail.poke()
    bridge.succeeded.emit(trail.key, [ROW_NEW])
    assert not trail.empty.isVisibleTo(trail)
    trail.poke()
    bridge.succeeded.emit(trail.key, [])  # the rows left
    assert trail.table.rowCount() == 0
    assert trail.empty.isVisibleTo(trail)


def test_a_recovered_read_clears_the_refusal_note(qt) -> None:
    """The refusal note's clear (#1051 review): the read the
    note named comes back, and the note leaves with it."""
    trail, bridge = make_trail(qt)
    trail.poke()
    bridge.failed.emit(trail.key, "the daemon is gone")
    assert trail.note.text() == "refused: the daemon is gone"
    trail.poke()
    bridge.succeeded.emit(trail.key, [ROW_NEW])
    assert trail.note.text() == ""


def test_the_decisions_match_the_daemons_tokens() -> None:
    """The duplicated vocabulary's pin (#1051 review): the
    filter's decisions are the daemon's own tokens in the same
    order — a drifted token would surface only as a runtime
    400 when its filter is picked, so the test holds the two
    spellings together."""
    from msks.spec.egress import DECISIONS as SPEC_DECISIONS

    assert audit_mod.DECISIONS == tuple(SPEC_DECISIONS)
