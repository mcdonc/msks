"""The workspace Mounts tab (#987): the presenter's plain
arithmetic — the spec parse through the CLI's own parser, the
add/replace/remove composition, the save and pending notes —
and the tab over a stub bridge: the list from a row fake, an
edit through the bridge, a named refusal keeping the edited
list. The widgets run under the offscreen platform on every
host.
"""

import importlib.util
import os
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.mountrows import (  # noqa: E402
    PENDING_WORDS,
    SAVED,
    SAVED_PENDING,
    added_mount,
    mount_entry,
    pending_words,
    removed_mount,
    replaced_mount,
    saved_note,
)


class FakeSignal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once, in the GUI thread
    the real signal's queued delivery would land them in."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def disconnect(self, slot) -> None:
        self.slots.remove(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class StubBridge:
    """The bridge's surface for the tab's tests: the two signals
    it reads and the one verb it calls, every call recorded,
    every flight a real future — the tab holds it for the
    cancel a workspace switch takes."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object):
        from concurrent.futures import Future

        self.calls.append((key, method, args))
        return Future()


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the tab needs, the dialog
    tests' own fixture shape (#962)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import mounts as mounts_mod
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(
        app=app,
        QtWidgets=QtWidgets,
        QtCore=QtCore,
        keep=[],
        mounts=mounts_mod,
    )
    yield ns
    del ns.keep[:]
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def daemon_row() -> dict:
    """One workspace's row carrying two mounts, one of them
    skipped by a gate, and a pending flag standing."""
    return {
        "id": "ws-a",
        "name": "alpha",
        "status": "running",
        "mounts": [
            {"host": "/srv/ok", "guest": "/mnt/ok", "mode": "rw"},
            {"host": "/srv/gone", "guest": "/mnt/gone"},
        ],
        "mount_issues": [
            {"host": "/srv/gone", "guest": "/mnt/gone", "kind": "missing"}
        ],
        "mounts_pending": True,
        "sizes_pending": False,
    }


def make_tab(qt) -> tuple:
    """The tab over the stub bridge, hosted on the row, shown in
    a real window — the screens' own shape."""
    bridge = StubBridge()
    tab = qt.mounts.MountsTab(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.show()
    qt.keep.append(window)
    tab.host("ws-a", daemon_row())
    return tab, bridge


def fake_dialog(monkeypatch, qt, entry: dict | None) -> list:
    """The form's stub (#987): the entry handed in is the form's
    accepted answer, its exec returning at once — the opener
    the tab passed as the form's parent recorded (#1014)."""
    parents: list[object] = []

    class FakeDialog:
        def __init__(self, seed: dict | None = None, parent=None) -> None:
            self.entry = entry
            parents.append(parent)

        def exec(self) -> int:
            return 1 if entry is not None else 0

    monkeypatch.setattr(qt.mounts, "MountDialog", FakeDialog)
    return parents


def test_the_parse_reads_the_clis_own_words() -> None:
    """The form's parse (#987, the TUI form's own path): the
    CLI's parser over the composed spec — whole entries landing
    with the mode defaulting to rw, the blank required fields
    saying themselves, and a shape refusal naming itself in the
    command's own words (the ``msks: `` prefix aside)."""
    entry, error = mount_entry("/srv/refs", "/mnt/refs", "rw")
    assert (entry, error) == (
        {"host": "/srv/refs", "guest": "/mnt/refs", "mode": "rw"},
        "",
    )
    entry, _error = mount_entry("/srv/refs", "/mnt/refs", "ro")
    assert entry == {
        "host": "/srv/refs",
        "guest": "/mnt/refs",
        "mode": "ro",
    }
    assert mount_entry("", "/mnt/refs", "rw") == (
        None,
        "the host path is the form's one required field",
    )
    assert mount_entry("/srv/refs", "", "rw") == (
        None,
        "the guest path is the form's one required field",
    )
    assert mount_entry("srv/refs", "/mnt/refs", "rw")[0] is None
    assert "refused:" in mount_entry("srv/refs", "/mnt/refs", "rw")[1]
    assert mount_entry("/srv/refs", "mnt/refs", "rw")[0] is None
    assert mount_entry("/srv/refs", "/mnt/refs", "rx")[0] is None


def test_the_list_arithmetic_composes_the_next_whole_list() -> None:
    """The composition (#987): the add joins at the end, the
    replace takes its slot, the remove drops its slot — and an
    index the list no longer carries leaves the list
    unchanged."""
    mounts = [
        {"host": "/srv/a", "guest": "/mnt/a"},
        {"host": "/srv/b", "guest": "/mnt/b"},
    ]
    fresh = {"host": "/srv/c", "guest": "/mnt/c", "mode": "ro"}
    assert added_mount(mounts, fresh) == [*mounts, fresh]
    assert replaced_mount(mounts, 1, fresh) == [mounts[0], fresh]
    assert removed_mount(mounts, 0) == [mounts[1]]
    assert replaced_mount(mounts, 9, fresh) == mounts
    assert removed_mount(mounts, -1) == mounts


def test_the_notes_read_the_pending_flag() -> None:
    """The notes (#987, the TUI editor's own words): the save's
    next-start half while the refreshed row says the configured
    set waits, and the standing words for the tab's note — both
    gone when the attached set and the configured one agree."""
    assert saved_note(daemon_row()) == SAVED_PENDING
    assert pending_words(daemon_row()) == PENDING_WORDS
    settled = {**daemon_row(), "mounts_pending": False}
    assert saved_note(settled) == SAVED
    assert pending_words(settled) is None


def test_the_tab_lists_the_rows_mounts(qt) -> None:
    """The tab's table (#987): one row per mount with the cells
    the TUI's editor renders — the host path, the guest path,
    the mode, the state word with the skip's reason — and the
    standing pending note in words while the configured set
    waits for a start."""
    tab, _bridge = make_tab(qt)
    assert tab.table.rowCount() == 2
    cells = [
        tab.table.item(row, column).text()
        for row in range(2)
        for column in range(5)
    ]
    assert cells == [
        "/srv/ok",
        "/mnt/ok",
        "rw",
        "healthy",
        "-",
        "/srv/gone",
        "/mnt/gone",
        "rw",
        "skipped",
        "missing",
    ]
    assert tab.pending_label.isVisibleTo(tab)
    assert tab.pending_label.text() == PENDING_WORDS
    # A settled row clears the note.
    tab.host("ws-a", {**daemon_row(), "mounts_pending": False})
    assert not tab.pending_label.isVisibleTo(tab)


def test_the_table_shares_the_panes_whole_width(qt) -> None:
    """The tab's width rule (#1025): the mode and the state
    hug their text, the host, the guest, and the reason share
    the pane's whole remainder — the sections' total is the
    viewport itself, with no blank tail and no single column
    pooling the space."""
    tab, _bridge = make_tab(qt)
    # a real pane's width, not the offscreen window's own sizeHint
    tab.window().resize(900, 500)
    qt.app.processEvents()
    header = tab.table.horizontalHeader()
    modes = [header.sectionResizeMode(c) for c in range(5)]
    assert modes == [
        qt.QtWidgets.QHeaderView.ResizeMode.Stretch,
        qt.QtWidgets.QHeaderView.ResizeMode.Stretch,
        qt.QtWidgets.QHeaderView.ResizeMode.ResizeToContents,
        qt.QtWidgets.QHeaderView.ResizeMode.ResizeToContents,
        qt.QtWidgets.QHeaderView.ResizeMode.Stretch,
    ]
    total = sum(header.sectionSize(c) for c in range(5))
    assert total == tab.table.viewport().width()
    assert header.sectionSize(0) > header.sectionSize(2)


def test_an_add_posts_the_whole_list_and_the_answer_lands(qt) -> None:
    """The Add action (#987): an accepted entry joins the
    working list and the next whole list posts through the
    bridge; the daemon's refreshed row repaints the table, the
    note names what the save waits for, and the page behind
    takes the row through the signal."""
    tab, bridge = make_tab(qt)
    seen: list[dict] = []
    tab.row_updated.connect(seen.append)
    entry = {"host": "/srv/new", "guest": "/mnt/new", "mode": "ro"}
    tab.change(added_mount(tab.working, entry))
    assert bridge.calls == [
        (
            tab.flight_key,
            "set_mounts",
            ("ws-a", [*daemon_row()["mounts"], entry]),
        )
    ]
    assert not tab.add_button.isEnabled()  # one flight
    fresh = {
        **daemon_row(),
        "mounts": [*daemon_row()["mounts"], entry],
    }
    bridge.succeeded.emit(tab.flight_key, fresh)
    assert seen == [fresh]
    assert tab.table.rowCount() == 3
    assert tab.note.text() == SAVED_PENDING
    assert tab.add_button.isEnabled()


def test_an_edit_and_a_remove_run_through_the_bridge(qt, monkeypatch) -> None:
    """The Edit and Remove actions (#987): the form seeded with
    the selected row's mount, an accepted entry taking its slot;
    the remove dropping the selected slot — both posting the
    whole list."""
    tab, bridge = make_tab(qt)
    entry = {"host": "/srv/changed", "guest": "/mnt/ok", "mode": "ro"}
    parents = fake_dialog(monkeypatch, qt, entry)
    tab.table.selectRow(0)
    tab.edit()
    assert parents == [tab]  # the form parents to the tab (#1014)
    assert bridge.calls == [
        (
            tab.flight_key,
            "set_mounts",
            ("ws-a", [entry, daemon_row()["mounts"][1]]),
        )
    ]
    bridge.succeeded.emit(tab.flight_key, daemon_row())
    bridge.calls.clear()
    tab.table.selectRow(1)
    tab.remove()
    assert bridge.calls == [
        (
            tab.flight_key,
            "set_mounts",
            ("ws-a", [daemon_row()["mounts"][0]]),
        )
    ]


def test_the_row_menu_carries_edit_and_remove(qt) -> None:
    """A right-click on a row opens its menu (#1098): Edit and
    Remove — the buttons' own paths, the clicked row taking the
    selection first — with Add staying on the button row. A
    press below every row opens nothing."""
    tab, bridge = make_tab(qt)
    assert tab.row_menu(99) is None  # a row the table does not hold
    before = tab.findChildren(qt.QtWidgets.QMenu)
    tab.menu_requested(qt.QtCore.QPoint(2, 10_000))  # below every row
    assert tab.findChildren(qt.QtWidgets.QMenu) == before  # nothing popped
    row = 1
    pos = qt.QtCore.QPoint(2, tab.table.rowViewportPosition(row) + 2)
    tab.menu_requested(pos)
    assert tab.selected() == row  # the click took the selection
    menu = tab.findChildren(qt.QtWidgets.QMenu)[-1]
    assert [action.text() for action in menu.actions()] == [
        "Edit",
        "Remove",
    ]
    assert [action.isEnabled() for action in menu.actions()] == [
        True,
        True,
    ]
    menu.actions()[1].trigger()  # Remove: the clicked row's slot drops
    assert bridge.calls == [
        (
            tab.flight_key,
            "set_mounts",
            ("ws-a", [daemon_row()["mounts"][0]]),
        )
    ]


def test_the_row_menus_edit_runs_the_buttons_own_path(qt, monkeypatch) -> None:
    """The menu's Edit (#1098): the form seeded with the
    clicked row's mount — the selection the click took — and an
    accepted entry taking its slot, the whole list posting
    through the bridge, the button's own path."""
    tab, bridge = make_tab(qt)
    entry = {"host": "/srv/changed", "guest": "/mnt/ok", "mode": "ro"}
    fake_dialog(monkeypatch, qt, entry)
    pos = qt.QtCore.QPoint(2, tab.table.rowViewportPosition(0) + 2)
    tab.menu_requested(pos)
    menu = tab.findChildren(qt.QtWidgets.QMenu)[-1]
    menu.actions()[0].trigger()  # Edit: the clicked row's slot takes it
    assert bridge.calls == [
        (
            tab.flight_key,
            "set_mounts",
            ("ws-a", [entry, daemon_row()["mounts"][1]]),
        )
    ]


def test_a_flight_gates_the_row_menu_like_the_buttons(qt) -> None:
    """The menu's gating (#1098): a flight already out disables
    both entries — the buttons' own rule — and a press on a row
    still opens the menu, its actions dark."""
    tab, _bridge = make_tab(qt)
    tab.change(added_mount(tab.working, {"host": "/x", "guest": "/y"}))
    pos = qt.QtCore.QPoint(2, tab.table.rowViewportPosition(0) + 2)
    tab.menu_requested(pos)
    menu = tab.findChildren(qt.QtWidgets.QMenu)[-1]
    assert [action.isEnabled() for action in menu.actions()] == [
        False,
        False,
    ]


def test_a_named_refusal_keeps_the_edited_list(qt) -> None:
    """A refused save (#987): the daemon's named 400 lands its
    whole message on the note line, and the working list rolls
    back to the snapshot the change took — the refused edit
    rides no later save."""
    tab, bridge = make_tab(qt)
    settled = [dict(mount) for mount in daemon_row()["mounts"]]
    gone = removed_mount(settled, 1)
    tab.change(gone)
    assert tab.table.rowCount() == 1  # the composition shows
    bridge.failed.emit(
        tab.flight_key,
        "host path /srv/gone is outside the allowed roots",
    )
    assert "outside the allowed roots" in tab.note.text()
    assert tab.note.text().startswith("refused: ")
    assert tab.note.kind == "error"  # the error tint (#1160)
    assert tab.table.rowCount() == 2  # the rollback
    assert tab.working == settled
    assert tab.add_button.isEnabled()


def test_the_dialog_builds_seeded_and_submits(qt) -> None:
    """The form itself (#987): blank for the add with rw
    standing, seeded for the edit with the mount's own mode — a
    whole submit accepts carrying the entry, a refused one
    names itself on the note line and the form stands."""
    blank = qt.mounts.MountDialog()
    qt.keep.append(blank)
    assert blank.windowTitle() == "Add a mount"
    assert blank.host.text() == ""
    assert blank.mode.currentText() == "rw"
    seeded = qt.mounts.MountDialog(
        {"host": "/srv/ok", "guest": "/mnt/ok", "mode": "ro"}
    )
    qt.keep.append(seeded)
    assert seeded.windowTitle() == "Edit a mount"
    assert seeded.host.text() == "/srv/ok"
    assert seeded.mode.currentText() == "ro"
    seeded.submit()
    assert seeded.entry == {
        "host": "/srv/ok",
        "guest": "/mnt/ok",
        "mode": "ro",
    }
    refused = qt.mounts.MountDialog()
    qt.keep.append(refused)
    refused.submit()  # blank: the required field says itself
    assert refused.entry is None
    assert refused.note.text() == (
        "the host path is the form's one required field"
    )
    foreign = qt.mounts.MountDialog(
        {"host": "/s", "guest": "/g", "mode": "rx"}
    )
    qt.keep.append(foreign)
    assert (
        foreign.mode.currentText() == "rw"
    )  # an unknown mode reads as the default


def test_a_cancelled_form_sends_nothing(qt, monkeypatch) -> None:
    """A cancelled form (#987): the add and the edit compose
    nothing — the bridge hears no call — and a tab with no
    selection edits and removes nothing either."""
    tab, bridge = make_tab(qt)
    fake_dialog(monkeypatch, qt, None)  # exec answers a cancel
    tab.add()
    assert bridge.calls == []
    tab.table.selectRow(0)
    tab.edit()  # the form answered a cancel: nothing composes
    assert bridge.calls == []
    tab.table.setCurrentCell(-1, -1)
    tab.edit()
    tab.remove()
    assert bridge.calls == []


def test_the_adds_own_path_posts_the_entry(qt, monkeypatch) -> None:
    """The Add action's own path (#987): the accepted form's
    entry joins the working list and posts through the bridge —
    the button's click and the form's answer composing the same
    whole list the change path posts."""
    tab, bridge = make_tab(qt)
    entry = {"host": "/srv/new", "guest": "/mnt/new"}
    fake_dialog(monkeypatch, qt, entry)
    tab.add()
    assert bridge.calls == [
        (
            tab.flight_key,
            "set_mounts",
            ("ws-a", [*daemon_row()["mounts"], entry]),
        )
    ]


def test_another_keys_answer_is_not_the_tabs_to_take(qt) -> None:
    """The bridge is shared (#987): a foreign key's answer and a
    foreign key's failure change nothing — the screens that own
    those keys take them."""
    tab, bridge = make_tab(qt)
    note = tab.note.text()
    bridge.succeeded.emit("workspaces-list", [])
    bridge.failed.emit("detail-health", "not this tab's")
    assert tab.note.text() == note
    assert tab.table.rowCount() == 2


def test_a_refresh_inside_the_dialog_re_resolves_the_edit(
    qt, monkeypatch
) -> None:
    """The edit's wait (#987, the TUI editor's own rule): the
    page's tick can land inside the modal form and re-create
    every dict — the edit re-resolves its mount by the (host,
    guest) pair on the working list it finds after the form,
    and a mount that left the list decides nothing."""
    tab, bridge = make_tab(qt)
    settled = [dict(mount) for mount in daemon_row()["mounts"]]
    entry = {"host": "/srv/changed", "guest": "/mnt/ok"}

    class RefreshingDialog:
        """The form whose wait lands a tick: the daemon's list
        shrank while the operator edited row 0."""

        def __init__(self, seed: dict | None = None, parent=None) -> None:
            self.entry = entry

        def exec(self) -> int:
            tab.host("ws-a", {**daemon_row(), "mounts": settled[1:]})
            return 1

    monkeypatch.setattr(qt.mounts, "MountDialog", RefreshingDialog)
    tab.table.selectRow(0)
    tab.edit()
    assert bridge.calls == []  # the edited mount left: nothing composes
    assert tab.table.rowCount() == 1

    class ReorderingDialog(RefreshingDialog):
        """The form whose wait lands a tick that reorders: the
        edited pair sits at another slot, and the edit finds
        it."""

        def exec(self) -> int:
            tab.host("ws-a", {**daemon_row(), "mounts": settled[::-1]})
            return 1

    tab.host("ws-a", daemon_row())  # the whole list again
    tab.table.selectRow(0)  # /srv/ok, slot 0
    monkeypatch.setattr(qt.mounts, "MountDialog", ReorderingDialog)
    tab.edit()  # the tick moved /srv/ok to slot 1: the edit finds it
    assert bridge.calls == [
        (
            tab.flight_key,
            "set_mounts",
            ("ws-a", [settled[1], entry]),
        )
    ]


def test_a_late_refusal_from_the_prior_workspace_decides_nothing(
    qt,
) -> None:
    """The switch's generation (#987): a refusal the prior
    workspace's finished flight delivers past the reset — the
    cancel a no-op on it — neither names itself on the fresh
    page nor rolls a live flight's list back under it."""
    tab, bridge = make_tab(qt)
    tab.change(added_mount(tab.working, {"host": "/x", "guest": "/y"}))
    ws_a_key = tab.flight_key  # ws-a's flight, its cancel a no-op once done
    tab.host("ws-b", {**daemon_row(), "id": "ws-b"})
    assert tab.add_button.isEnabled()
    entry = {"host": "/srv/new", "guest": "/mnt/new"}
    tab.change(added_mount(tab.working, entry))  # ws-b's own flight
    # ws-a's refusal delivers late: the note holds, the flight
    # holds, the composed list holds.
    bridge.failed.emit(ws_a_key, "host path /x refused")
    assert tab.note.text() == ""
    assert not tab.add_button.isEnabled()
    assert tab.table.rowCount() == 3
    # ws-b's own answer lands whole.
    bridge.succeeded.emit(
        tab.flight_key,
        {
            **daemon_row(),
            "id": "ws-b",
            "mounts": [*daemon_row()["mounts"], entry],
        },
    )
    assert tab.note.text() == "saved — applies at the next start"
    assert tab.table.rowCount() == 3


def test_a_failure_without_a_snapshot_keeps_the_list(qt) -> None:
    """A refusal nothing composed (#987): the note carries it
    and the standing list stands — there is no snapshot to roll
    back to, and none is needed."""
    tab, bridge = make_tab(qt)
    bridge.failed.emit(tab.flight_key, "the daemon is gone")
    assert tab.note.text() == "refused: the daemon is gone"
    assert tab.table.rowCount() == 2


def test_a_new_workspace_cancels_the_old_flight(qt) -> None:
    """The open's reset (#987): a workspace switch mid-flight
    cancels the old workspace's save — its answer is not the new
    workspace's to take — the tab blanks, the prior one's
    refusal leaves the note, and the Add stays gated until the
    new workspace's own row lands."""
    tab, bridge = make_tab(qt)
    bridge.failed.emit(tab.flight_key, "host path refused")
    assert "host path refused" in tab.note.text()
    tab.change(added_mount(tab.working, {"host": "/x", "guest": "/y"}))
    assert not tab.add_button.isEnabled()
    stale_key = tab.flight_key  # ws-a's flight, cancelled at the switch
    tab.host("ws-b", {})
    assert not tab.add_button.isEnabled()  # nothing landed for ws-b
    assert tab.note.text() == ""  # the prior refusal left with it
    assert tab.table.rowCount() == 0
    # The old workspace's late answer changes nothing here.
    bridge.succeeded.emit(stale_key, daemon_row())
    assert tab.table.rowCount() == 0
    assert tab.workspace_id == "ws-b"
    tab.host("ws-b", {**daemon_row(), "id": "ws-b"})
    assert tab.add_button.isEnabled()


def test_a_flight_gates_the_changes(qt) -> None:
    """One flight at a time (#987): a change already out gates
    the buttons and the next composition alike — the listing's
    own rule, so a late reply cannot roll a later change back
    out of the working list."""
    tab, _bridge = make_tab(qt)
    tab.change(added_mount(tab.working, {"host": "/x", "guest": "/y"}))
    tab.change(removed_mount(tab.working, 0))  # gated: nothing sent
    assert len(_bridge.calls) == 1


def test_a_tick_under_a_flight_keeps_the_posted_list(qt) -> None:
    """The page's row landing mid-flight (#987): the tick's
    older snapshot cannot roll the posted list back out from
    under the flight — the answer repaints."""
    tab, bridge = make_tab(qt)
    entry = {"host": "/srv/new", "guest": "/mnt/new"}
    tab.change(added_mount(tab.working, entry))
    tab.host("ws-a", daemon_row())  # the tick's older row
    assert tab.table.rowCount() == 3  # the posted list stands
    bridge.succeeded.emit(
        tab.flight_key, {**daemon_row(), "mounts_pending": False}
    )
    assert tab.table.rowCount() == 2
    assert tab.note.text() == SAVED


def test_the_state_cell_carries_its_kind(qt) -> None:
    """The mount state cell's tint (#1160): a mount the last
    start attached reads ok, one it skipped warn, any other word
    the chrome's own text."""
    assert qt.mounts.state_kind("healthy") == "ok"
    assert qt.mounts.state_kind("skipped") == "warn"
    assert qt.mounts.state_kind("attached") is None


def test_the_tab_tags_the_state_cells(qt) -> None:
    """The table's state cells (#1160): the kinds ride the items
    under the semantic role — the healthy mount ok, the skipped
    one warn — and the reason column carries none."""
    from msks.client.gui.scheme import SEMANTIC_ROLE

    tab, _bridge = make_tab(qt)
    assert tab.table.item(0, 3).data(SEMANTIC_ROLE) == "ok"
    assert tab.table.item(1, 3).data(SEMANTIC_ROLE) == "warn"
    assert tab.table.item(1, 4).data(SEMANTIC_ROLE) is None
