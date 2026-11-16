"""The GUI workspace-detail presenter (#944): daemon rows,
health docs, and event frames in; section models out — the
mounts section with its derived state, the health facts, and
the recent events, plain data the whole way. The presenter
imports nothing from PySide6, so these tests drive it as plain
Python."""

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
        # The stream's second id convention: the consent frames.
        {
            "event": "egress.request",
            "data": {"workspace_id": WS, "host": "example.com", "port": 443},
        },
        # A payload family the section keeps: the line reads the
        # payload, not the name alone.
        {
            "event": "proxy.failed",
            "data": {"id": WS, "listen_port": 8080, "reason": "refused"},
        },
    ]


def test_the_mounts_section_reads_the_derived_state() -> None:
    """The mounts section (#620, part #624): one entry per
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
    # A workspace that mounts nothing reads as an empty section,
    # and a skip without a detail reads as its kind alone.
    assert detail_mod.mount_entries(daemon_row(mounts=[])) == ()
    bare = detail_mod.mount_entries(
        daemon_row(
            mounts=[{"host": "/h", "guest": "/g", "mode": "ro"}],
            issues=[{"kind": "gone", "host": "/h", "guest": "/g"}],
        )
    )
    assert bare[0].reason == "gone"


def test_the_pending_note_follows_the_flag() -> None:
    """The mounts section's next-start note (#756): standing
    while the configured set differs from the attached set, gone
    when the two agree again."""
    assert (
        detail_mod.pending_note(daemon_row(mounts_pending=True))
        == detail_mod.PENDING_NOTE
    )
    assert detail_mod.pending_note(daemon_row()) is None


def test_the_health_section_reads_the_doc() -> None:
    """The health section (#944): the daemon's health doc as
    label/value facts, an absent image reading its dash, no doc
    yet an empty section."""
    doc = {
        "status": "ok",
        "version": "0.9.2",
        "machine": "x86_64",
        "fastapi": "0.141.1",
        "image": None,
    }
    assert detail_mod.health_pairs(doc) == (
        ("status", "ok"),
        ("version", "0.9.2"),
        ("machine", "x86_64"),
        ("image", "-"),
    )
    assert detail_mod.health_pairs(None) == ()
    with_image = dict(doc, image="debian:13.6")
    assert detail_mod.health_pairs(with_image)[-1] == (
        "image",
        "debian:13.6",
    )


def test_the_events_section_reads_the_workspace_tail() -> None:
    """The events section (#8, #944): the workspace's own frames
    only, newest last, each as its compact line — the status
    transition's word, the skipped mount's guest and kind, the
    stream name alone for anything else."""
    events = detail_mod.event_entries(frames(), WS)
    assert [entry.kind for entry in events] == [
        "workspace.status",
        "mount.skipped",
        "workspace.status",
        "egress.request",  # named under the second convention
        "proxy.failed",
    ]
    assert [entry.line for entry in events] == [
        "status starting",
        "/mnt/gone skipped",
        "status running",
        "egress.request host=example.com port=443",
        "proxy.failed listen_port=8080 reason=refused",
    ]
    # The tail keeps its limit; an empty payload reads the name
    # alone, and another workspace stays out under either id
    # convention.
    many = frames() + [{"event": "bare.tap", "data": {"id": WS}}]
    trimmed = detail_mod.event_entries(many, WS, limit=3)
    assert len(trimmed) == 3
    assert trimmed[-1] == detail_mod.EventEntry(
        kind="bare.tap", line="bare.tap"
    )
    other = [
        {"event": "workspace.status", "data": {"id": "ws-b", "status": "up"}},
        {
            "event": "egress.request",
            "data": {"workspace_id": "ws-b", "host": "x", "port": 1},
        },
    ]
    assert detail_mod.event_entries(other, WS) == ()


def test_the_sections_compose() -> None:
    """The whole model (#944): the label, the mounts with their
    note, the health facts, and the events tail — the plain data
    the widget renders, one call over the same reads the TUI's
    workspace page makes."""
    row = daemon_row(
        mounts=[{"host": "/srv/gone", "guest": "/mnt/gone"}],
        issues=[
            {"kind": "skipped", "host": "/srv/gone", "guest": "/mnt/gone"}
        ],
        mounts_pending=True,
    )
    model = detail_mod.detail_sections(row, {"status": "ok"}, frames())
    assert model.id == WS  # the key every action calls
    assert model.label == "alpha"
    assert model.mounts[0].state == detail_mod.SKIPPED
    assert model.pending_note == detail_mod.PENDING_NOTE
    assert model.health[0] == ("status", "ok")
    assert len(model.events) == 3
    nameless = detail_mod.detail_sections(
        daemon_row(name=None, mounts=[]), None, []
    )
    assert nameless.label == WS


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
