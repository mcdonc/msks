"""The workspace list's presenter (phase 3 of #924, #938):
daemon rows in, row models out — plain data the list screen's
widget (the phase after this one) renders. The module holds no
PySide6 import, so the suite drives it as plain Python and the
coverage gate lands on it whole: no widgets, no daemon imports,
the same contract :mod:`msks.client.tui.rows` keeps for the
TUI's screens.
"""

from dataclasses import dataclass

#: The state word's sort order (#938), mirroring the TUI's
#: ``STATUS_SORT_ORDER`` (#854): the lifecycle order an operator
#: scans in, the vocabulary's own sequence. A state outside the
#: vocabulary sorts after it.
STATE_SORT_ORDER = (
    "created",
    "starting",
    "running",
    "paused",
    "stopping",
    "stopped",
    "absent",
    "unknown",
)

#: The lifecycle rank per state value (#938).
STATE_SORT_RANK = {state: rank for rank, state in enumerate(STATE_SORT_ORDER)}


@dataclass(frozen=True)
class WorkspaceRow:
    """One workspace as the list screen renders it (#938): the
    label, the state word beside its rank in the lifecycle
    order, and the unresolved-mounts marker — the whole row the
    widget needs to sort and render, as plain data with nothing
    Qt-bound in it."""

    label: str
    state: str
    state_rank: int
    issue_marker: str | None


def workspace_label(row: dict) -> str:
    """The row's human-facing label (#246): its name, else its
    id."""
    return row.get("name") or row["id"]


def state_rank(state: str) -> int:
    """The state's rank in the lifecycle order (#938) — the key
    the widget sorts by. A state outside the vocabulary ranks
    after every known one."""
    return STATE_SORT_RANK.get(state, len(STATE_SORT_ORDER))


def issue_marker(row: dict) -> str | None:
    """The unresolved-mounts marker (#620, part #624, #938):
    ``✗N``, N counting the workspace's unresolved mounts, for a
    row whose mounts skip — else ``None``, the field the widget
    shows beside the state word only while there is a count to
    show."""
    issues = row.get("mount_issues") or []
    return f"✗{len(issues)}" if issues else None


def workspace_row(row: dict) -> WorkspaceRow:
    """One daemon row as its row model (#938): the label, the
    state word with its rank, and the marker — plain data
    whatever the daemon's vocabulary grows."""
    return WorkspaceRow(
        label=workspace_label(row),
        state=row["status"],
        state_rank=state_rank(row["status"]),
        issue_marker=issue_marker(row),
    )


def workspace_rows(rows: list[dict]) -> list[WorkspaceRow]:
    """The listing's row models (#938): one per daemon row, in
    the order the daemon served them — the widget sorts by the
    rank each model carries."""
    return [workspace_row(row) for row in rows]
