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
from .kinds import ERROR, OK, WARN

#: The GUI window's plain title (#939): the screens give it a
#: workspace to name while a detail page shows (#971), and the
#: shell takes the plain title back on the list. The shared
#: leaf carries it so the entry and the screens read one
#: definition.
WINDOW_TITLE = "msks"

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

#: The states whose row offers a start (#972): a workspace that
#: stands stopped, and one created but never started.
STARTABLE_STATES = ("stopped", "created")


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
    action: str


def workspace_label(row: dict) -> str:
    """The row's human-facing label (#246): its name, else its
    id."""
    return row.get("name") or row["id"]


def action_verb(state: str) -> str:
    """The row's start/stop action as plain data (#972): start
    while the workspace stands stopped or was only created, stop
    in any other state. The row model carries the choice; the
    widget renders the icon for it, and the choice itself reads
    as plain Python, no Qt in it."""
    return "start" if state in STARTABLE_STATES else "stop"


def state_rank(state: str) -> int:
    """The state's rank in the lifecycle order (#938) — the key
    the widget sorts by. A state outside the vocabulary ranks
    after every known one."""
    return STATE_SORT_RANK.get(state, len(STATE_SORT_ORDER))


def pending_marker(model: WorkspaceRow) -> str | None:
    """The pending-changes marker (#986, the TUI's own ↻N over
    the same flags): one per waiting half — a mounts edit the
    last start has not taken, a sizes or topology edit no boot
    has taken — else ``None``. The status cell leads its state
    word with it, the TUI listing's own shape."""
    return f"↻{model.pending_count}" if model.pending_count else None


def restart_note(model: WorkspaceRow) -> str | None:
    """The restart the pending marker names, in words (#986,
    the TUI's pending-flags own naming): how many changes wait
    for the next start — the count the marker carries, said as
    a sentence the status cell's tooltip and the strips read.
    A row with nothing waiting answers ``None``."""
    count = model.pending_count
    if not count:
        return None
    noun = "change" if count == 1 else "changes"
    verb = "applies" if count == 1 else "apply"
    return f"{count} {noun} {verb} at the next start"


#: The states that read as a failure (#1160): the daemon cannot
#: place the row's artifacts, or lost track of it.
ERROR_STATES = frozenset({"absent", "unknown"})
#: The states in flight (#1160): between resting states, with
#: the paused VM held by the hypervisor among them.
WARN_STATES = frozenset({"starting", "stopping", "paused"})


def marked(model: WorkspaceRow) -> bool:
    """Whether the status cell leads with a marker: an unresolved
    mount or a change that waits for the next start."""
    return model.issue_marker is not None or pending_marker(model) is not None


def state_kind(model: WorkspaceRow) -> str | None:
    """The status cell's tint (#1160): a running workspace reads
    ok; a transition and a row with a marker (unresolved mounts,
    pending changes) read warn; a row the daemon cannot place
    reads error. The resting states (created, stopped) wear the
    chrome's own text: a stopped workspace is not news."""
    if model.state in ERROR_STATES:
        return ERROR
    if model.state in WARN_STATES or marked(model):
        return WARN
    return OK if model.state == "running" else None


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
        action=action_verb(row["status"]),
    )


def cost_of(ws: dict) -> int | None:
    """One report entry's root-plus-home cost (#849), or None
    for an entry that predates the per-row fields."""
    if "root_bytes" not in ws or "home_bytes" not in ws:
        return None
    return ws["root_bytes"] + ws["home_bytes"]


def workspace_costs(report: dict) -> dict[str, int]:
    """The capacity report's per-workspace costs (#849): the
    root-plus-home bytes, keyed by id. An entry that predates
    the per-row fields reads as nothing, so the standing figures
    keep until a fuller report lands."""
    costs: dict[str, int] = {}
    for ws in report.get("workspaces") or []:
        cost = cost_of(ws)
        if cost is not None and "id" in ws:
            costs[ws["id"]] = cost
    return costs


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
) -> list[tuple[str | None, str]]:
    """The line's posture and cost pairs (#849's facts, #986's
    words): cpu, memory, the egress mode — then the mounts
    marker and the restart note while either shows, then the
    storage the capacity report charges. A pair's label reads
    ``None`` when the value is its own sentence — the restart
    note carries no "label value" shape."""
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


def marker_pairs(model: WorkspaceRow) -> list[tuple[str | None, str]]:
    """The strip's marker pairs (#849's shape, #986's words):
    the mounts count while any mount skips, then the restart
    note while a change waits for the next start — the pending
    count said as a sentence, the words the ✗N and ↻N cues the
    status cell carries stand for here."""
    pairs = []
    if model.issue_marker is not None:
        pairs.append(("mounts", model.issue_marker))
    note = restart_note(model)
    if note is not None:
        pairs.append((None, note))
    return pairs


def workspace_rows(rows: list[dict]) -> list[WorkspaceRow]:
    """The listing's row models (#938): one per daemon row, in
    the order the daemon served them — the widget sorts by the
    rank each model carries."""
    return [workspace_row(row) for row in rows]
