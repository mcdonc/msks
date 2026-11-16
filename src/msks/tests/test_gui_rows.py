"""The GUI workspace-list presenter (#938): daemon-row fakes in,
row models out — the label, the state word with its lifecycle
rank, the ``✗N`` unresolved-mounts marker, and the id, image,
and created cells (#939). The presenter holds no Qt import, so
these tests drive it as plain Python, the same way the coverage
gate does."""

import ast
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest
from msks.client import ages as ages_mod
from msks.client.gui import rows as gui_rows
from msks.client.tui import rows as tui_rows

WS = "ws-a"


def daemon_row(
    id: str = WS,
    name: str | None = "alpha",
    status: str = "stopped",
    issues: Sequence[dict] | None = (),
    image_ref: str | None = "debian:13.6",
    image_hash: str | None = "b" * 64,
    created_at: str | None = "2026-01-02T03:04:05",
    cpus: int | None = 2,
    mem_mib: int | None = 8192,
    egress_mode: str | None = "allow",
    mounts_pending: bool = False,
    sizes_pending: bool = False,
) -> dict:
    """One daemon row as the listing serves it (#938's fakes):
    the fields the presenter reads. The issue list defaults to
    the daemon's healthy shape — the listing attaches
    ``mount_issues`` to every row — and a caller passing
    ``None`` exercises the defensive read for a daemon that
    predates the field."""
    row = {
        "id": id,
        "name": name,
        "status": status,
        "image_ref": image_ref,
        "image_hash": image_hash,
        "created_at": created_at,
        "cpus": cpus,
        "mem_mib": mem_mib,
        "egress_mode": egress_mode,
        "mounts_pending": mounts_pending,
        "sizes_pending": sizes_pending,
    }
    if issues is not None:
        row["mount_issues"] = list(issues)
    return row


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The created labels read one fixed clock (#350's seam, on
    the shared leaf since #939): the fixture's Jan 2 stamp reads
    ``2d ago`` under the pinned Jan 4."""
    monkeypatch.setattr(
        ages_mod,
        "clock_now",
        lambda: datetime.fromisoformat("2026-01-04T12:00:00"),
    )


def test_the_presenter_builds_one_model_per_row() -> None:
    """The listing's row models (#938): one per daemon row, in
    the order the daemon served them, each carrying the five
    fields the screen renders."""
    models = gui_rows.workspace_rows(
        [daemon_row(), daemon_row(id="ws-b", name=None, status="running")]
    )
    assert [model.label for model in models] == ["alpha", "ws-b"]
    assert [model.state for model in models] == ["stopped", "running"]
    assert all(isinstance(model, gui_rows.WorkspaceRow) for model in models)


def test_the_model_carries_what_the_widget_renders() -> None:
    """One row model's fields (#938, #939): the label, the state
    word, the rank the widget sorts by, the marker it shows
    beside the state, and the id, image, and created cells —
    the TUI listing's five over the same row."""
    model = gui_rows.workspace_row(
        daemon_row(status="running", issues=[{"kind": "skipped"}])
    )
    assert model == gui_rows.WorkspaceRow(
        label="alpha",
        state="running",
        state_rank=2,
        issue_marker="✗1",
        id=WS,
        image="debian:13.6",
        created="2d ago",
        created_rank=gui_rows.created_sort_key("2026-01-02T03:04:05"),
        cpus=2,
        mem_mib=8192,
        egress_mode="allow",
        pending_count=0,
    )


def test_the_label_falls_back_to_the_id() -> None:
    """The label (#246): the workspace's name, else its id — a
    nameless workspace (no name, or an empty one) reads by its
    id."""
    assert gui_rows.workspace_label(daemon_row()) == "alpha"
    assert gui_rows.workspace_label(daemon_row(name=None)) == WS
    assert gui_rows.workspace_label(daemon_row(name="")) == WS


def test_the_marker_counts_unresolved_mounts() -> None:
    """The unresolved-mounts marker (#620, part #624): ``✗N``
    with the count of the workspace's unresolved mounts, and no
    marker for a healthy row. The absent-field arm is the
    defensive read for a daemon predating the field — the
    listing itself attaches the list to every row."""
    issues = [{"kind": "skipped"}, {"kind": "missing"}]
    assert gui_rows.issue_marker(daemon_row(issues=issues)) == "✗2"
    assert gui_rows.issue_marker(daemon_row(issues=[])) is None
    assert gui_rows.issue_marker(daemon_row(issues=None)) is None


def test_the_image_prefers_the_reference() -> None:
    """The IMAGE cell's value (#470 L3): the catalog reference
    first, the image hash when no reference came, and ``-`` when
    the row carries neither — the TUI's own read of the cell."""
    assert gui_rows.image_text(daemon_row()) == "debian:13.6"
    assert gui_rows.image_text(daemon_row(image_ref=None)) == "b" * 64
    assert (
        gui_rows.image_text(daemon_row(image_ref=None, image_hash=None)) == "-"
    )


def test_the_created_reads_the_shared_ladder() -> None:
    """The CREATED cell's value (#939): the relative label off
    the shared leaf (#350's pinned rule), ``-`` for a row with
    no stamp. The GUI and the TUI read the same ladder, so the
    two listings age a row alike."""
    assert gui_rows.workspace_row(daemon_row()).created == "2d ago"
    fresh = gui_rows.workspace_row(
        daemon_row(created_at="2026-01-04T09:00:00")
    )
    assert fresh.created == "today"
    missing = gui_rows.workspace_row(daemon_row(created_at=None))
    assert missing.created == "-"


def test_the_created_sort_key_reads_the_moment() -> None:
    """The CREATED column's sort key (#939, #854's rule): the
    parsed moment, never the relative label — the labels'
    text order and date order disagree — with a missing or
    unparseable stamp reading as the oldest."""
    assert gui_rows.created_sort_key(None) == 0.0
    assert gui_rows.created_sort_key("not a stamp") == 0.0
    assert gui_rows.created_sort_key("") == 0.0
    older = gui_rows.created_sort_key("2025-01-15T00:00:00")
    newer = gui_rows.created_sort_key("2026-01-04T09:00:00")
    assert 0.0 < older < newer
    model = gui_rows.workspace_row(daemon_row())
    assert model.created_rank == gui_rows.created_sort_key(
        "2026-01-02T03:04:05"
    )


def test_the_state_ranks_in_the_lifecycle_order() -> None:
    """The rank follows the vocabulary's own sequence (#938): a
    created workspace ranks before a starting one, running
    before stopped, and a state outside the vocabulary ranks
    after every known one."""
    assert (
        gui_rows.state_rank("created")
        < gui_rows.state_rank("starting")
        < gui_rows.state_rank("running")
        < gui_rows.state_rank("stopped")
    )
    known = max(
        gui_rows.state_rank(state) for state in gui_rows.STATE_SORT_ORDER
    )
    assert gui_rows.state_rank("hibernating") > known


def test_the_order_mirrors_the_tui_sort_order() -> None:
    """The GUI sorts in the TUI's own lifecycle order (#854's
    STATUS_SORT_ORDER): the two clients scan a fleet the same
    way, and a state either vocabulary grows lands on both at
    once."""
    assert gui_rows.STATE_SORT_ORDER == tui_rows.STATUS_SORT_ORDER


def test_the_helpers_match_the_tui_over_the_same_rows() -> None:
    """The mirrored helpers keep behavior parity with the TUI
    (#938): over the same daemon-row fakes, both clients read
    the same label and build the same ``✗N`` marker — the sort
    order's pin alone does not hold the pair together, and a
    formatting change on one side must fail here rather than
    drift the two listings apart."""
    rows = [
        daemon_row(),
        daemon_row(name=None),
        daemon_row(status="running", issues=[{"kind": "skipped"}]),
        daemon_row(id="ws-b", name="beta", issues=[]),
        daemon_row(issues=None),
    ]
    for row in rows:
        assert gui_rows.workspace_label(row) == tui_rows.workspace_label(row)
        assert gui_rows.issue_marker(row) == tui_rows.issue_marker(row)


def test_the_pending_count_counts_waiting_halves() -> None:
    """The pending marker's count (#898's rule, the GUI's read):
    one per waiting half — a mounts edit the last start has not
    taken, a sizes or topology edit no boot has taken."""
    none = gui_rows.workspace_row(daemon_row())
    assert none.pending_count == 0
    mounts = gui_rows.workspace_row(daemon_row(mounts_pending=True))
    assert mounts.pending_count == 1
    both = gui_rows.workspace_row(
        daemon_row(mounts_pending=True, sizes_pending=True)
    )
    assert both.pending_count == 2


def test_the_detail_line_reads_the_model() -> None:
    """The detail strip's single line (#849's facts, the GUI's
    read): identity, posture, and costs ``·``-separated, the
    markers while either counts, the storage figure from the
    cache, dashes for fields a row predates."""
    model = gui_rows.workspace_row(
        daemon_row(
            status="running",
            issues=[{"kind": "skipped"}],
            mounts_pending=True,
        )
    )
    assert gui_rows.detail_text(model, 10737418241) == (
        f"alpha  ·  id {WS}  ·  image debian:13.6  ·  cpu 2  ·  "
        "mem 8G  ·  egress allow  ·  mounts ✗1  ·  next ↻1  ·  "
        "storage 10G"
    )
    bare = gui_rows.workspace_row(
        daemon_row(cpus=None, mem_mib=None, egress_mode=None)
    )
    assert gui_rows.detail_text(bare, None) == (
        f"alpha  ·  id {WS}  ·  image debian:13.6  ·  "
        "cpu -  ·  mem -  ·  egress -  ·  storage -"
    )


def test_the_module_imports_nothing_from_pyside6() -> None:
    """The presenter stays Qt-free (#938): the widget module
    owns every Qt import, and this module's plain data keeps the
    coverage gate — and a base install's parse path, through the
    package init's pin — off the binding."""
    tree = ast.parse(Path(gui_rows.__file__).read_text())
    roots = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots |= {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(root.split(".")[0] == "PySide6" for root in roots)
