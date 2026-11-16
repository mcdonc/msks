"""The workspace list's presenter (phase 3 of #924, #938):
daemon rows in, row models out — plain data the list screen's
widget (the phase after this one) renders. The model carries the
TUI listing's five fields — label, state with its lifecycle
rank, the unresolved-mounts marker, id, image, and the created
label (:mod:`msks.client.ages`) — with the relative-label
arithmetic shared, not duplicated (#939). The module holds no
PySide6 import, so the suite drives it as plain Python and the
coverage gate lands on it whole: no widgets, no daemon imports.
"""

from dataclasses import dataclass

from ..ages import created_label, parse_stamp
from ..volumes import MIB, human_bytes

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
    """One workspace as the list screen renders it (#938, #939):
    the label, the state word beside its rank in the lifecycle
    order, the unresolved-mounts marker, and the id, image, and
    created cells the TUI's listing shows of the same row — the
    whole row the widget needs to sort and render, as plain data
    with nothing Qt-bound in it."""

    label: str
    state: str
    state_rank: int
    issue_marker: str | None
    id: str
    image: str
    created: str
    created_rank: float
    cpus: int | None
    mem_mib: int | None
    egress_mode: str
    pending_count: int


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


def image_text(row: dict) -> str:
    """The IMAGE cell's value (#470 L3): the catalog reference,
    else the image hash the row carries, else ``-`` — the TUI's
    own read of the same cell."""
    return row.get("image_ref") or row.get("image_hash") or "-"


def created_sort_key(created_at: str | None) -> float:
    """The CREATED column's sort key (#854's rule, the GUI's
    read): the parsed creation timestamp — never the relative
    label, whose text order and date order disagree. A stamp
    that is missing or does not parse reads as the oldest, so
    it sinks on the newest-first read, the TUI's own standing
    order."""
    stamp = parse_stamp(created_at) if created_at else None
    return stamp.timestamp() if stamp is not None else 0.0


def workspace_row(row: dict) -> WorkspaceRow:
    """One daemon row as its row model (#938): the label, the
    state word with its rank, the marker, and the id, image, and
    created cells — plain data whatever the daemon's vocabulary
    grows."""
    return WorkspaceRow(
        label=workspace_label(row),
        state=row["status"],
        state_rank=state_rank(row["status"]),
        issue_marker=issue_marker(row),
        id=row["id"],
        image=image_text(row),
        created=created_label(row.get("created_at")),
        created_rank=created_sort_key(row.get("created_at")),
        cpus=row.get("cpus"),
        mem_mib=row.get("mem_mib"),
        egress_mode=row.get("egress_mode") or "-",
        pending_count=int(bool(row.get("mounts_pending")))
        + int(bool(row.get("sizes_pending"))),
    )


def detail_text(model: WorkspaceRow, used_bytes: int | None) -> str:
    """The detail strip's single line (#849's facts, the GUI's
    read): the identity — the name bare, the id, the image —
    then the posture and costs — cpu, memory, the egress mode,
    the mounts and pending markers while either counts, the
    storage the capacity report charges — one ``·``-separated
    row the operator can select and copy whole. Every fact but
    storage reads the row model; storage reads the screen's
    cached report, an honest dash until one lands."""
    pairs = [(None, model.label), ("id", model.id), ("image", model.image)]
    pairs += detail_pairs(model, used_bytes)
    return "  ·  ".join(
        value if label is None else f"{label} {value}"
        for label, value in pairs
    )


def detail_pairs(
    model: WorkspaceRow, used_bytes: int | None
) -> list[tuple[str, str]]:
    """The line's posture and cost pairs (#849's facts): cpu,
    memory, the egress mode — then the mounts and pending
    markers while either counts, then the storage the capacity
    report charges."""
    pairs = [
        ("cpu", cpu_value(model)),
        ("mem", mem_value(model)),
        ("egress", model.egress_mode or "-"),
    ]
    pairs += marker_pairs(model)
    pairs.append(
        ("storage", "-" if used_bytes is None else human_bytes(used_bytes))
    )
    return pairs


def cpu_value(model: WorkspaceRow) -> str:
    """The cpu fact (#849's shape): the count, the honest dash
    for a row that predates the field."""
    return "-" if model.cpus is None else str(model.cpus)


def mem_value(model: WorkspaceRow) -> str:
    """The memory fact in binary units (#849's shape): the same
    renderer the storage figure uses, the honest dash for a row
    that predates the field."""
    if model.mem_mib is None:
        return "-"
    return human_bytes(model.mem_mib * MIB)


def marker_pairs(model: WorkspaceRow) -> list[tuple[str, str]]:
    """The strip's marker pairs (#849's shape): the mounts count
    while any mount skips, the pending count while a change
    waits for the next start — the same ✗N/↻N cues the status
    cell carries, read beside the costs."""
    pairs = []
    if model.issue_marker is not None:
        pairs.append(("mounts", model.issue_marker))
    if model.pending_count:
        pairs.append(("next", f"↻{model.pending_count}"))
    return pairs


def workspace_rows(rows: list[dict]) -> list[WorkspaceRow]:
    """The listing's row models (#938): one per daemon row, in
    the order the daemon served them — the widget sorts by the
    rank each model carries."""
    return [workspace_row(row) for row in rows]
