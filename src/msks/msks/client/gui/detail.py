"""The workspace detail view's presenter (phase 4 of #924,
#944): daemon rows and event frames in; mount entries and event
lines out — plain data the tabs render. The Info pane that
composed whole sections is gone (#1017), and the parts the
surviving tabs read stand: the mounts tab reads the entries
with their derived state, the events tab reads the frame lines,
and the page's buffer filter reads the frame naming. The module
imports nothing from PySide6, so the suite drives it as plain
Python and the coverage gate lands on it whole; its vocabulary
comes from :mod:`msks.spec` through the client allowlist (the
mount modes), the same door the TUI's own detail modules use.
"""

from dataclasses import dataclass

from ...spec.mounts import MODE_RW

#: A mount the gates passed, and one they refused (#620): the
#: state words the mounts tab carries.
HEALTHY = "healthy"
SKIPPED = "skipped"

#: How many recent frames the events window keeps (#944, #989):
#: the tab's own tail bound, newest last.
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
    """The mounts tab's entries (#620, part #624): one entry per
    configured mount, its derived state beside its spec —
    healthy alone, skipped with the reason the gate named. A
    workspace that mounts nothing reads as an empty table."""
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
    """One mount as its entry (#620): the spec the operator
    wrote — the mode defaulting to the vocabulary's own MODE_RW
    — beside the state the daemon derived."""
    state, reason = mount_state(issue)
    return MountEntry(
        host=mount.get("host") or "-",
        guest=mount.get("guest") or "-",
        mode=mount.get("mode") or MODE_RW,
        state=state,
        reason=reason,
    )


def event_line(frame: dict) -> str:
    """One frame's compact line (#8, #944): the lifecycle
    transition's status word for a status frame, the skipped
    mount's guest and kind for a mount frame, the consent
    request's destination for an egress frame, and the stream
    name with its payload's scalar fields for anything
    else."""
    kind = frame.get("event") or "?"
    data = frame.get("data") or {}
    if kind == "workspace.status":
        return status_event_line(data)
    return named_event_line(kind, data)


def status_event_line(data: dict) -> str:
    """The status frame's compact line (#8): the transition's
    own word."""
    return f"status {data.get('status', '?')}"


def named_event_line(kind: str, data: dict) -> str:
    """The named families' lines (#8, #944): the mount and
    egress families carry their own; everything else reads the
    generic payload line."""
    if kind.startswith("mount."):
        return mount_event_line(kind, data)
    if kind.startswith("egress."):
        return egress_event_line(kind, data)
    return payload_line(kind, data)


def mount_event_line(kind: str, data: dict) -> str:
    """A mount frame's compact line (#620): the guest the frame
    names beside the skip's kind."""
    guest = data.get("guest") or "?"
    return f"{guest} {kind.partition('.')[2]}"


def payload_line(kind: str, data: dict) -> str:
    """A kept frame's generic line (#944): the stream name with
    its payload's scalar fields as ``key=value`` pairs — a list
    joined, a nested mapping left to its family's own dedicated
    line — the id fields the filter matched on left out. A
    family the section keeps reads what its payload names, not
    its name alone."""
    fields = []
    for key in sorted(data):
        if key in ("id", "workspace_id", "workspaces"):
            continue
        text = value_text(data[key])
        if text:
            fields.append(f"{key}={text}")
    line = " ".join(fields)
    return f"{kind} {line}".rstrip()


def value_text(value: object) -> str:
    """One payload value as line text (#944): a scalar as its
    own str, a list joined by commas; a nested mapping renders
    nothing here — the family that carries one has its own
    dedicated line."""
    if isinstance(value, dict):
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def egress_event_line(kind: str, data: dict) -> str:
    """A consent frame's compact line (#944): the nested request
    row's destination beside the frame's own verb."""
    request = data.get("request") or {}
    host = request.get("dest_host") or "?"
    port = request.get("dest_port")
    port_text = "?" if port is None else str(port)
    return f"egress {host}:{port_text} {kind.partition('.')[2]}"


def frame_names(data: dict, workspace_id: str) -> bool:
    """Whether one frame's data names the workspace (#944):
    under any of the stream's naming conventions — ``id`` on the
    lifecycle frames, the legacy ``workspace_id`` scalar on the
    consent and secret frames, and the ``workspaces`` list those
    frames' own contract calls the truth. A daemon-wide row
    (``*``) belongs to no workspace's page."""
    if data.get("id") == workspace_id:
        return True
    if data.get("workspace_id") == workspace_id:
        return True
    return workspace_id in (data.get("workspaces") or [])
