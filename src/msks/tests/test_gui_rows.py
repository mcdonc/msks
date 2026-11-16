"""The GUI workspace-list presenter (#938): daemon-row fakes in,
row models out — the label, the state word with its lifecycle
rank, and the ``✗N`` unresolved-mounts marker. The presenter
holds no Qt import, so these tests drive it as plain
Python, the same way the coverage gate does."""

import ast
from collections.abc import Sequence
from pathlib import Path

from msks.client.gui import rows as gui_rows
from msks.client.tui import rows as tui_rows

WS = "ws-a"


def daemon_row(
    id: str = WS,
    name: str | None = "alpha",
    status: str = "stopped",
    issues: Sequence[dict] | None = (),
) -> dict:
    """One daemon row as the listing serves it (#938's fakes):
    the three fields the presenter reads. The issue list
    defaults to the daemon's healthy shape — the listing
    attaches ``mount_issues`` to every row — and a caller
    passing ``None`` exercises the defensive read for a daemon
    that predates the field."""
    row = {"id": id, "name": name, "status": status}
    if issues is not None:
        row["mount_issues"] = list(issues)
    return row


def test_the_presenter_builds_one_model_per_row() -> None:
    """The listing's row models (#938): one per daemon row, in
    the order the daemon served them, each carrying the label,
    the state word, and the marker field."""
    models = gui_rows.workspace_rows(
        [daemon_row(), daemon_row(id="ws-b", name=None, status="running")]
    )
    assert [model.label for model in models] == ["alpha", "ws-b"]
    assert [model.state for model in models] == ["stopped", "running"]
    assert all(isinstance(model, gui_rows.WorkspaceRow) for model in models)


def test_the_model_carries_what_the_widget_renders() -> None:
    """One row model's fields (#938): the label, the state word,
    the rank the widget sorts by, and the marker it shows beside
    the state."""
    model = gui_rows.workspace_row(
        daemon_row(status="running", issues=[{"kind": "skipped"}])
    )
    assert model == gui_rows.WorkspaceRow(
        label="alpha",
        state="running",
        state_rank=2,
        issue_marker="✗1",
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
