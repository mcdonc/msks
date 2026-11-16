"""The workspace detail view's presenter (phase 4 of #924, #944):
daemon rows, health docs, and event frames in; section models out
— plain data the detail widget (the next issue) renders. The
module imports nothing from PySide6, so the suite drives it as
plain Python and the coverage gate lands on it whole; its
vocabulary comes from :mod:`msks.spec` through the client
allowlist (the mount modes), the same door the TUI's own detail
modules use.
"""

from dataclasses import dataclass

from ...spec.mounts import MODE_RW

#: A mount the gates passed, and one they refused (#620): the
#: state words the mounts section carries.
HEALTHY = "healthy"
SKIPPED = "skipped"

#: The mounts section's next-start note (#756): the daemon's
#: ``mounts_pending`` flag stands while the configured set
#: differs from the set the last start attached.
PENDING_NOTE = "applies at next start"

#: How many recent frames the events section keeps (#944): the
#: detail view reads the tail, newest last.
EVENTS_LIMIT = 20


@dataclass(frozen=True)
class MountEntry:
    """One configured mount with its derived state (#620): the
    spec the operator wrote — host, guest, mode — beside the
    state the daemon derived, healthy or skipped with the reason
    the gate named."""

    host: str
    guest: str
    mode: str
    state: str
    reason: str | None


@dataclass(frozen=True)
class EventEntry:
    """One recent lifecycle frame (#8) as the section reads it:
    the event's stream name and its compact line, newest
    last."""

    kind: str
    line: str


@dataclass(frozen=True)
class DetailSections:
    """The detail view's sections (#944): the workspace's id
    and label, the mounts section with its next-start note
    while a change waits, the health facts, and the recent
    events — plain data for the widget to compose beside the
    row's own facts, nothing Qt-bound in it."""

    id: str
    label: str
    mounts: tuple[MountEntry, ...]
    pending_note: str | None
    health: tuple[tuple[str, str], ...]
    events: tuple[EventEntry, ...]


def issue_for(mount: dict, issues: list[dict]) -> dict | None:
    """The derived issue naming one mount, if any: matched on the
    host path beside the guest path, the pair a row names — the
    TUI mounts line's own match."""
    for issue in issues or []:
        if issue.get("host") == mount.get("host") and issue.get(
            "guest"
        ) == mount.get("guest"):
            return issue
    return None


def skip_reason(issue: dict) -> str:
    """The skip's reason as one line (#620): its kind, with the
    gate's detail behind it when the frame named one."""
    kind = issue.get("kind") or "?"
    detail = issue.get("detail")
    return f"{kind}: {detail}" if detail else kind


def mount_entries(row: dict) -> tuple[MountEntry, ...]:
    """The mounts section (#620, part #624): one entry per
    configured mount, its derived state beside its spec —
    healthy alone, skipped with the reason the gate named. A
    workspace that mounts nothing reads as an empty section."""
    issues = row.get("mount_issues") or []
    return tuple(
        mount_entry(mount, issue_for(mount, issues))
        for mount in row.get("mounts") or []
    )


def mount_state(issue: dict | None) -> tuple[str, str | None]:
    """The derived state and its reason (#620): healthy alone,
    skipped with the gate's reason."""
    if issue is None:
        return HEALTHY, None
    return SKIPPED, skip_reason(issue)


def mount_entry(mount: dict, issue: dict | None) -> MountEntry:
    """One mount as its section entry (#620): the spec the
    operator wrote — the mode defaulting to the vocabulary's
    own MODE_RW — beside the state the daemon derived."""
    state, reason = mount_state(issue)
    return MountEntry(
        host=mount.get("host") or "-",
        guest=mount.get("guest") or "-",
        mode=mount.get("mode") or MODE_RW,
        state=state,
        reason=reason,
    )


def pending_note(row: dict) -> str | None:
    """The mounts section's next-start note (#756): standing
    while the configured set differs from the set the last start
    attached, gone when the two agree again."""
    return PENDING_NOTE if row.get("mounts_pending") else None


def health_pairs(doc: dict | None) -> tuple[tuple[str, str], ...]:
    """The health section (#944): the daemon's health doc as
    label/value facts — its status, version, machine, and image,
    each reading the honest dash when the doc carries nothing.
    The doc's fastapi field is plumbing, not an operator fact,
    and stays off the section. No doc yet reads as an empty
    section."""
    if not doc:
        return ()
    keys = ("status", "version", "machine", "image")
    return tuple((key, str(doc.get(key) or "-")) for key in keys)


def event_line(frame: dict) -> str:
    """One frame's compact line (#8, #944): the lifecycle
    transition's status word for a status frame, the mount and
    its reason for a skip frame, the stream name alone for
    anything else."""
    kind = frame.get("event") or "?"
    data = frame.get("data") or {}
    if kind == "workspace.status":
        return f"status {data.get('status', '?')}"
    if kind.startswith("mount."):
        return mount_event_line(kind, data)
    return payload_line(kind, data)


def mount_event_line(kind: str, data: dict) -> str:
    """A mount frame's compact line (#620): the guest the frame
    names beside the skip's kind."""
    guest = data.get("guest") or "?"
    return f"{guest} {kind.partition('.')[2]}"


def payload_line(kind: str, data: dict) -> str:
    """A kept frame's generic line (#944): the stream name with
    its payload's fields as ``key=value`` pairs, the id fields
    the filter matched on left out — a family the section keeps
    reads what its payload names, not its name alone."""
    fields = " ".join(
        f"{key}={value}"
        for key, value in sorted(data.items())
        if key not in ("id", "workspace_id")
    )
    return f"{kind} {fields}".rstrip()


def event_entries(
    frames: list[dict], workspace_id: str, limit: int = EVENTS_LIMIT
) -> tuple[EventEntry, ...]:
    """The events section (#8, #944): the workspace's recent
    frames, newest last, the tail the detail view reads. The
    stream's two id conventions both name a workspace — ``id``
    on the lifecycle frames, ``workspace_id`` on the consent and
    secret frames — and the section keeps a frame under
    either."""
    mine = [
        EventEntry(kind=frame.get("event") or "?", line=event_line(frame))
        for frame in frames
        if frame_names(frame.get("data") or {}, workspace_id)
    ]
    return tuple(mine[-limit:])


def frame_names(data: dict, workspace_id: str) -> bool:
    """Whether one frame's data names the workspace (#944):
    under either of the stream's id conventions."""
    if data.get("id") == workspace_id:
        return True
    return data.get("workspace_id") == workspace_id


def detail_sections(
    row: dict, health: dict | None, frames: list[dict]
) -> DetailSections:
    """The detail view's sections (#944) for one workspace:
    its daemon row's mounts and label, the daemon's health doc,
    and the recent event frames — plain data the widget composes
    beside the row's own facts."""
    return DetailSections(
        id=row["id"],
        label=row.get("name") or row["id"],
        mounts=mount_entries(row),
        pending_note=pending_note(row),
        health=health_pairs(health),
        events=event_entries(frames, row["id"]),
    )
