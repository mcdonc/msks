"""The GUI workspace-detail presenter (#944): daemon rows and
event frames in; mount entries and event lines out — the
mounts' derived state beside the compact lines the events tab
reads, plain data the whole way. The presenter imports nothing
from PySide6, so these tests drive it as plain Python."""

import ast
from pathlib import Path

from msks.client.gui import detail as detail_mod

WS = "ws-a"


def daemon_row(
    id: str = WS,
    name: str | None = "alpha",
    mounts: list[dict] | None = None,
    issues: list[dict] | None = None,
    mounts_pending: bool = False,
) -> dict:
    """One daemon row with its mounts' derived state (#944's
    fakes): the fields the presenter reads."""
    row: dict = {
        "id": id,
        "name": name,
        "status": "stopped",
        "mounts_pending": mounts_pending,
    }
    if mounts is not None:
        row["mounts"] = mounts
    if issues is not None:
        row["mount_issues"] = issues
    return row


def frames() -> list[dict]:
    """The event tail as the socket serves it (#8): another
    workspace's frames around this one's, newest last."""
    return [
        {
            "event": "workspace.status",
            "data": {"id": "ws-b", "status": "running"},
        },
        {
            "event": "workspace.status",
            "data": {"id": WS, "status": "starting"},
        },
        {
            "event": "mount.skipped",
            "data": {
                "id": WS,
                "host": "/srv/gone",
                "guest": "/mnt/gone",
                "mode": "rw",
                "detail": "outside the allowed host-share roots",
            },
        },
        {"event": "workspace.status", "data": {"id": WS, "status": "running"}},
        # The consent frames' daemon shape: the legacy scalar and
        # the nested request row the line reads its destination
        # from — never a dict repr.
        {
            "event": "egress.request",
            "data": {
                "workspace_id": WS,
                "request": {
                    "id": "req-1",
                    "workspace_id": WS,
                    "dest_host": "api.anthropic.com",
                    "dest_port": 443,
                },
            },
        },
        # The secret frames' truth: the workspaces list.
        {
            "event": "secret.unlock",
            "data": {"workspaces": ["ws-b", WS], "ts": 1767225600.5},
        },
        # A payload family the window keeps: the line reads the
        # payload's scalars and joined list, never a repr.
        {
            "event": "workspace.resized",
            "data": {
                "id": WS,
                "changes": ["root grew to 4096 MiB", "cpus set to 4"],
            },
        },
    ]


def test_the_mounts_entries_read_the_derived_state() -> None:
    """The mounts entries (#620, part #624): one entry per
    configured mount, its spec beside its derived state —
    healthy alone, skipped with the kind and the gate's detail —
    the mode defaulting to the vocabulary's rw."""
    row = daemon_row(
        mounts=[
            {"host": "/srv/proj", "guest": "/mnt/proj", "mode": "rw"},
            {"host": "/srv/gone", "guest": "/mnt/gone"},
        ],
        issues=[
            {
                "kind": "skipped",
                "host": "/srv/gone",
                "guest": "/mnt/gone",
                "detail": "outside the allowed host-share roots",
            }
        ],
    )
    mounts = detail_mod.mount_entries(row)
    assert mounts == (
        detail_mod.MountEntry(
            host="/srv/proj",
            guest="/mnt/proj",
            mode="rw",
            state=detail_mod.HEALTHY,
            reason=None,
        ),
        detail_mod.MountEntry(
            host="/srv/gone",
            guest="/mnt/gone",
            mode="rw",  # the vocabulary's own default
            state=detail_mod.SKIPPED,
            reason="skipped: outside the allowed host-share roots",
        ),
    )
    # A workspace that mounts nothing reads as an empty table,
    # and a skip without a detail reads as its kind alone.
    assert detail_mod.mount_entries(daemon_row(mounts=[])) == ()
    bare = detail_mod.mount_entries(
        daemon_row(
            mounts=[{"host": "/h", "guest": "/g", "mode": "ro"}],
            issues=[{"kind": "gone", "host": "/h", "guest": "/g"}],
        )
    )
    assert bare[0].reason == "gone"


def test_the_lines_read_the_stream_families() -> None:
    """The frame lines (#8, #944): the workspace's own frames,
    each as its compact line — the status transition's word, the
    skipped mount's guest and kind, the stream name alone for
    anything else."""
    assert [
        detail_mod.event_line(frame)
        for frame in frames()
        if detail_mod.frame_names(frame.get("data") or {}, WS)
    ] == [
        "status starting",
        "/mnt/gone skipped",
        "status running",
        # named under the legacy workspace_id scalar
        "egress api.anthropic.com:443 request",
        # named under the workspaces list
        "secret.unlock ts=1767225600.5",
        "workspace.resized changes=root grew to 4096 MiB,cpus set to 4",
    ]
    # An empty payload reads the name alone, and a nested mapping
    # in a generic family's payload renders nothing — its
    # family's own line owns it — while the payload's scalars
    # still read.
    assert detail_mod.event_line(
        {"event": "bare.tap", "data": {"id": WS}}
    ) == ("bare.tap")
    assert (
        detail_mod.event_line(
            {
                "event": "holder.attach",
                "data": {"id": WS, "nested": {"a": 1}, "n": 2},
            }
        )
        == "holder.attach n=2"
    )


def test_frame_names_reads_every_convention() -> None:
    """The buffer's filter (#944): every naming convention the
    stream serves reads, and another workspace — or a
    daemon-wide row — stays out under any of them."""
    assert detail_mod.frame_names({"id": WS}, WS)
    assert detail_mod.frame_names({"workspace_id": WS}, WS)
    assert detail_mod.frame_names({"workspaces": ["ws-b", WS]}, WS)
    assert not detail_mod.frame_names({"id": "ws-b"}, WS)
    assert not detail_mod.frame_names({"workspace_id": "ws-b"}, WS)
    assert not detail_mod.frame_names({"workspaces": ["ws-c"]}, WS)
    # A daemon-wide row belongs to no workspace's page.
    assert not detail_mod.frame_names(
        {"workspace_id": "*", "workspaces": []}, WS
    )
    assert not detail_mod.frame_names({}, WS)


def test_the_module_imports_nothing_from_pyside6() -> None:
    """The presenter stays Qt-free (#944): the widget module owns
    every Qt import, and this module's plain data keeps the
    coverage gate — and a base install's parse path — off the
    binding."""
    tree = ast.parse(Path(detail_mod.__file__).read_text())
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
