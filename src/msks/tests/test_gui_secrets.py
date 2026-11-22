"""The GUI secrets tab (#966, #997): the presenter's models
rendered as the placeholder listing with the audit group
beneath, both tables' columns filling the pane's whole width
— the TTL countdown repainted from the deadline epochs, the
mint form, the renew picker, the revoke gate, every daemon
exchange through the stub bridge's signal emits, the bridge's
own tests holding the real worker's contract. The sentinel
pane is gone (#997). The widget runs
under the offscreen platform on every host (#939's fixture
shape)."""

import importlib.util
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client import ages as ages_mod  # noqa: E402
from msks.client.data import RestFailure  # noqa: E402
from msks.client.gui import secretrows as secretrows_mod  # noqa: E402
from msks.client.gui.listtable import fill_columns  # noqa: E402
from msks.client.gui.secrets import (  # noqa: E402
    cells as cells,
)
from msks.client.gui.secrets import (
    mint as mint_mod,
)
from msks.client.gui.secrets import (
    tab as secrets_mod,
)

#: The pinned clock the countdown tests read (#966's fakes): an
#: aware local moment, so every label under test is
#: deterministic on every host's timezone.
NOW = datetime(2030, 1, 2, 3, 4, 5).astimezone()

WIDE = "mskssec2_" + "w" * 24


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
    the tab reads and the one verb it calls, every call recorded
    — no worker, no network."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the tab needs, the list's
    own fixture shape (#939): the silent fontconfig, the platform
    pin, and the Qt names imported inside the fixture (a
    collection-time controller has no business loading the
    binding)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        QtTest=QtTest,
    )
    # The teardown every GUI test owes its worker (#959): close
    # and delete every top-level widget the test left, then
    # flush Qt's deferred deletions and posted events.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def placeholder(
    id: int = 1,
    name: str = "github_api",
    workspaces: list[str] | None = None,
    dests: list[str] | None = None,
    expires_at: str | None = None,
    created_at: str | None = None,
    locked: bool = False,
) -> dict:
    """One placeholder row as the listing serves it (#966's
    fakes): the fields the tab's gathered read carries, the
    creation a day behind the pinned clock."""
    return {
        "id": id,
        "workspaces": list(workspaces or []),
        "name": name,
        "dests": list(dests) if dests is not None else ["api.github.com"],
        "created_at": created_at or (NOW - timedelta(days=1)).isoformat(),
        "expires_at": expires_at,
        "locked": locked,
    }


def recorded(
    id: int,
    kind: str,
    *,
    workspaces: list[str] | None = None,
    name: str = "github_api",
    created_at: str = "2030-01-02T03:04:05",
) -> dict:
    """One recorded audit row as the listing serves it (#966's
    fakes): newest first is the endpoint's order — the caller
    stacks them newest first, as the daemon does."""
    return {
        "id": id,
        "kind": kind,
        "workspaces": list(workspaces or []),
        "name": name,
        "dests": [],
        "created_at": created_at,
    }


def page_answer(
    secrets: list[dict] | None = None, audit: list[dict] | None = None
) -> dict:
    """The gathered read's answer shape (#966)."""
    if secrets is None:
        secrets = [
            placeholder(),
            placeholder(
                id=2,
                name="deploy_key",
                workspaces=["ws2", "ws1"],
                dests=[],
                expires_at=(NOW + timedelta(hours=2)).isoformat(),
                locked=True,
            ),
        ]
    if audit is None:
        audit = [
            recorded(3, "mint", created_at="2030-01-02T03:06:07"),
            recorded(2, "revoke", created_at="2030-01-02T03:05:07"),
        ]
    return {"secrets": secrets, "audit": audit}


def pin_clock(monkeypatch, when) -> None:
    """The presenter's clock pinned at both modules that read it
    (#966's fakes): the countdown reads the presenter's own
    binding, the created label the shared leaf's — one pin for
    both, so every label under test is deterministic."""
    monkeypatch.setattr(secretrows_mod, "clock_now", lambda: when)
    monkeypatch.setattr(ages_mod, "clock_now", lambda: when)


def make_tab(qt, monkeypatch) -> tuple:
    """The tab over the stub bridge, shown in a real window —
    the shape every test reads: open, first refresh issued and
    answered, both groups standing, the clock pinned."""
    pin_clock(monkeypatch, NOW)
    bridge = StubBridge()
    tab = secrets_mod.SecretsTab(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.resize(1600, 1000)
    window.show()
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer())
    return tab, window, bridge


def rows_texts(table) -> list[list[str]]:
    """A group table's rows as their cell texts (#966's fakes
    read): every cell of every row, in table order."""
    return [
        [
            table.item(row, column).text()
            for column in range(table.columnCount())
        ]
        for row in range(table.rowCount())
    ]


def row_of_id(tab, secret_id: int) -> int:
    """The listing row an id shows on (#966's fakes read):
    resolved through the row's own id key, wherever the
    standing sort lands it."""
    for row in range(tab.table.rowCount()):
        item = tab.table.item(row, 0)
        if item is not None and item.data(cells.ROW_KEY_ROLE) == secret_id:
            return row
    raise AssertionError(f"id {secret_id!r} shows on no row")


def test_the_open_tab_issues_its_first_refresh(qt) -> None:
    """The tab fetches when it is shown, not when the strip
    builds it (#966): a background tab asks nothing, the show
    issues the one gathered read, and the countdown's tick
    starts with it."""
    bridge = StubBridge()
    tab = secrets_mod.SecretsTab(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    assert bridge.calls == []  # construction alone asks nothing
    window.show()
    assert bridge.calls == [(secrets_mod.PAGE_KEY, "secrets_page", ())]
    assert tab.tick_timer.isActive()
    window.close()


def test_the_page_answer_renders_both_groups(qt, monkeypatch) -> None:
    """The two groups from the presenter's models (#966): the
    placeholder rows with their coverage, name, destinations,
    countdown, created label, and lock state; the audit rows
    newest first with their marker, time, kind, identity, and
    destination — the standing counts on the bottom line."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    assert rows_texts(tab.table) == [
        [
            "*",
            "github_api",
            "api.github.com",
            "never",
            "yesterday",
            "open",
        ],
        [
            "ws1,ws2",
            "deploy_key",
            "-",
            "2h",
            "yesterday",
            "locked",
        ],
    ]
    assert [row[2:4] for row in rows_texts(tab.audit_table)] == [
        ["mint", "*/github_api"],
        ["revoke", "*/github_api"],
    ]
    assert tab.counts.text() == "2 placeholders · 2 audit rows"
    assert not tab.list_empty.isVisibleTo(tab)
    assert not tab.audit_empty.isVisibleTo(tab)
    window.close()


def test_the_empty_states_name_their_absence(qt, monkeypatch) -> None:
    """The empty groups (#966): the listing's own advice — the
    Mint action's name — and the audit view's own line, with the
    tables carrying nothing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    tab.refresh()
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer(secrets=[], audit=[]))
    assert tab.table.rowCount() == 0
    assert tab.audit_table.rowCount() == 0
    assert tab.list_empty.text() == "No placeholders — Mint mints one."
    assert tab.list_empty.isVisibleTo(tab)
    assert tab.audit_empty.isVisibleTo(tab)
    assert tab.counts.text() == "0 placeholders · 0 audit rows"
    window.close()


def table_spare(table) -> int:
    """A table's viewport width beyond its columns' total
    (#997's fakes read): what the fill has left to give."""
    return table.viewport().width() - sum(
        table.columnWidth(column) for column in range(table.columnCount())
    )


def test_both_tables_fill_the_window_width(qt, monkeypatch) -> None:
    """The tables fill their panes (#997): after a window resize
    re-flows the columns, each table's columns take the whole
    viewport width — no dead margin beside either table — on
    the window the test sized and on a wider one alike."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    for width in (1200, 2000):
        window.resize(width, 900)
        qt.app.processEvents()
        for table in (tab.table, tab.audit_table):
            assert 0 <= table_spare(table) <= table.columnCount()
    window.close()


def test_the_tables_never_owe_a_horizontal_bar(qt, monkeypatch) -> None:
    """A vertical scrollbar appearing after a fill (#997): the
    viewport's own resize re-flows the columns against the
    narrower pane — a pane never owes the operator a horizontal
    bar for it, the images tab's own rule (#964)."""
    rows = [placeholder(id=i, name=f"key_{i}") for i in range(1, 60)]
    tab, window, _bridge = make_tab(qt, monkeypatch)
    window.resize(900, 300)
    qt.app.processEvents()
    tab.refresh()
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer(secrets=rows))
    qt.app.processEvents()
    for table in (tab.table, tab.audit_table):
        assert table.horizontalScrollBar().maximum() == 0
        assert 0 <= table_spare(table) <= table.columnCount()
    window.close()


def test_a_window_resized_behind_a_hidden_tab_reflows_on_show(
    qt, monkeypatch
) -> None:
    """A window resized while the tab stands hidden (#997): the
    pane's resize reaches the tables at the tab's next show,
    the columns re-flowing against the window's current width —
    no dead margin beside the tables on the return."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.setVisible(False)
    window.resize(1900, 900)
    qt.app.processEvents()
    tab.setVisible(True)
    qt.app.processEvents()
    for table in (tab.table, tab.audit_table):
        assert 0 <= table_spare(table) <= table.columnCount()
    window.close()


def test_a_wider_window_widens_the_columns(qt, monkeypatch) -> None:
    """The reflow follows the window (#997): a wider window
    widens the columns' total with it, the spare shared out
    again — each table keeps filling all the horizontal space
    the window gives."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    window.resize(1100, 900)
    qt.app.processEvents()
    narrow = sum(
        tab.table.columnWidth(column)
        for column in range(tab.table.columnCount())
    )
    window.resize(2100, 900)
    qt.app.processEvents()
    wide = sum(
        tab.table.columnWidth(column)
        for column in range(tab.table.columnCount())
    )
    assert wide > narrow + 400
    assert 0 <= table_spare(tab.table) <= tab.table.columnCount()
    window.close()


def test_fill_columns_shares_the_spare_exactly(qt) -> None:
    """The shared fill's arithmetic (#997): a table wider than
    its contents lands its columns' total exactly on the
    viewport's width, the odd pixel to the earlier columns."""
    table = qt.QtWidgets.QTableWidget(1, 3)
    for column, text in enumerate(("one", "a-longer-cell", "x")):
        table.setItem(0, column, qt.QtWidgets.QTableWidgetItem(text))
    table.setHorizontalHeaderLabels(("FIRST", "SECOND", "THIRD"))
    # The pane's geometry must be real before the fill reads it
    # (#997): a widget the platform never showed carries a
    # placeholder viewport size that varies by host.
    table.show()
    qt.app.processEvents()
    table.resize(700, 120)
    qt.app.processEvents()
    table.resizeColumnsToContents()
    contents = [
        table.columnWidth(column) for column in range(table.columnCount())
    ]
    fill_columns(table)
    widths = [table.columnWidth(column) for column in range(3)]
    assert sum(widths) == table.viewport().width()
    shares = [width - content for width, content in zip(widths, contents)]
    assert max(shares) - min(shares) <= 1  # the odd pixel to the earlier


def test_fill_columns_keeps_the_contents_on_a_tight_pane(qt) -> None:
    """A pane tighter than the contents (#997): the columns
    keep their contents' widths — nothing crushes below what
    the values ask, and the table scrolls instead."""
    table = qt.QtWidgets.QTableWidget(1, 3)
    for column, text in enumerate(
        (
            "a-very-long-cell-value-here",
            "another-long-one-here",
            "third-long-value-here",
        )
    ):
        table.setItem(0, column, qt.QtWidgets.QTableWidgetItem(text))
    # The pane's geometry must be real before the fill reads it
    # (#997): a widget the platform never showed carries a
    # placeholder viewport size that varies by host.
    table.show()
    qt.app.processEvents()
    table.resize(160, 100)
    qt.app.processEvents()
    table.resizeColumnsToContents()
    contents = [
        table.columnWidth(column) for column in range(table.columnCount())
    ]
    fill_columns(table)
    widths = [table.columnWidth(column) for column in range(3)]
    assert widths == contents
    assert sum(widths) > table.viewport().width()


def test_the_ttl_column_counts_down_without_a_daemon_call(
    qt, monkeypatch
) -> None:
    """The countdown's own read (#966): the TTL cells repaint
    from the deadline epochs the presenter landed — paint-time
    math over the cached models, the bridge asked nothing per
    tick — and a row without an epoch keeps its frozen cell."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    bridge.calls.clear()
    assert tab.table.item(row_of_id(tab, 2), cells.EXPIRES_COLUMN).text() == (
        "2h"
    )
    pin_clock(monkeypatch, NOW + timedelta(hours=3))
    tab.on_tick()
    assert bridge.calls == []  # no daemon call per tick
    assert tab.table.item(row_of_id(tab, 2), cells.EXPIRES_COLUMN).text() == (
        "expired"
    )
    # The unbounded row keeps its frozen cell.
    assert tab.table.item(row_of_id(tab, 1), cells.EXPIRES_COLUMN).text() == (
        "never"
    )
    window.close()


def test_the_manual_refresh_skips_a_flight_already_out(
    qt, monkeypatch
) -> None:
    """The refresh guard (#966, the listing's own): a refresh
    already in flight is skipped — and the gate carries the
    ask: the landing takes the flight the gate refused, so a
    refresh is never dropped either."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    bridge.calls.clear()
    tab.refresh()
    tab.refresh()
    assert bridge.calls == [(secrets_mod.PAGE_KEY, "secrets_page", ())]
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer())
    # The carried ask flew on the landing.
    assert bridge.calls[-1] == (secrets_mod.PAGE_KEY, "secrets_page", ())
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer())
    bridge.calls.clear()
    tab.refresh()
    assert len(bridge.calls) == 1
    window.close()


def test_an_answer_for_another_key_answers_nothing(qt, monkeypatch) -> None:
    """A stranger's key answers nothing (#966): the tab took no
    such flight, so its answer belongs to whoever did — the
    groups and the status line stand."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    standing = rows_texts(tab.table)
    tab.on_succeeded("someone-elses", {"secrets": [], "audit": []})
    assert rows_texts(tab.table) == standing
    tab.on_failed("someone-elses", RestFailure("nope", reach=False))
    assert tab.status.text() == ""
    window.close()


def test_a_failed_page_fetch_flashes_and_the_groups_stand(
    qt, monkeypatch
) -> None:
    """A refused gathered read (#966): the failure flashes on
    the status line — an outage stands until an answer resolves
    it — and the groups already on screen stand."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    standing = rows_texts(tab.table)
    tab.refresh()
    tab.on_failed(secrets_mod.PAGE_KEY, RestFailure("daemon gone", reach=True))
    assert not tab.pending
    assert tab.status.text() == "daemon gone"
    assert tab.flash_outage
    assert rows_texts(tab.table) == standing
    window.close()


def test_the_selection_carries_across_a_rebuild(qt, monkeypatch) -> None:
    """A refresh carries the operator's hold by row content
    (#966, the listing's own rule): the held row follows its id
    wherever the rebuild lands it, and a row the new listing
    dropped simply loses its hold."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.table.selectRow(row_of_id(tab, 2))
    tab.refresh()
    tab.on_succeeded(
        secrets_mod.PAGE_KEY,
        page_answer(secrets=[placeholder(id=2, name="deploy_key")]),
    )
    assert tab.table.rowCount() == 1
    assert (
        tab.table.item(tab.table.currentRow(), 0).data(cells.ROW_KEY_ROLE) == 2
    )
    tab.refresh()
    tab.on_succeeded(
        secrets_mod.PAGE_KEY, page_answer(secrets=[placeholder()])
    )
    assert tab.table.selectedItems() == []
    window.close()


def test_the_listing_columns_sort_by_their_ranks(qt, monkeypatch) -> None:
    """The listing's header clicks sort (#966): the countdown by
    its deadline epoch — the unbounded row last — the created
    label by its epoch, the plain cells by their words."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.table.sortByColumn(
        cells.EXPIRES_COLUMN,
        qt.QtCore.Qt.SortOrder.AscendingOrder,
    )
    assert [
        item.data(cells.ROW_KEY_ROLE)
        for item in (tab.table.item(row, 0) for row in range(2))
    ] == [2, 1]
    tab.table.sortByColumn(
        cells.EXPIRES_COLUMN,
        qt.QtCore.Qt.SortOrder.DescendingOrder,
    )
    assert [
        item.data(cells.ROW_KEY_ROLE)
        for item in (tab.table.item(row, 0) for row in range(2))
    ] == [1, 2]
    window.close()


def test_the_page_carries_no_sentinel_pane(qt, monkeypatch) -> None:
    """The sentinel pane is gone (#997): the page's groups are
    the placeholder listing and the audit table alone, and an
    activated row sends nothing — the tab took no sentinel
    flight."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    groups = [box.title() for box in tab.findChildren(qt.QtWidgets.QGroupBox)]
    assert groups == [cells.LIST_GROUP, cells.AUDIT_GROUP]
    assert not hasattr(secrets_mod, "SENTINEL_KEY")
    bridge.calls.clear()
    tab.table.activated.emit(tab.table.model().index(row_of_id(tab, 1), 0))
    tab.on_succeeded("secrets-sentinel", dict(placeholder(), sentinel=WIDE))
    tab.on_failed("secrets-sentinel", RestFailure("nope", reach=False))
    assert bridge.calls == []
    assert tab.status.text() == ""
    window.close()


def test_the_action_buttons_sit_at_the_bottom(qt, monkeypatch) -> None:
    """The page's action row (#1011): Refresh and Mint sit in a
    row at the bottom of the page — the detail pages' Mounts
    and Proxies pattern — below the two groups and above the
    standing counts, the flash line taking the row's rest."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    layout = tab.layout()
    assert layout.itemAt(0).widget().title() == cells.LIST_GROUP
    assert layout.itemAt(1).widget().title() == cells.AUDIT_GROUP
    bar = layout.itemAt(2)  # the page's bottom row
    assert bar.itemAt(0).widget() is tab.refresh_button
    assert bar.itemAt(1).widget() is tab.mint_button
    assert bar.itemAt(2).widget() is tab.status
    assert layout.itemAt(3).widget() is tab.counts
    window.close()


def test_renew_picks_the_ttl_and_posts_it(qt, monkeypatch) -> None:
    """The renew action (#966): the picker opens over the
    presenter's span with its own default — the nearest choice
    to the row's remaining lifetime — and the pick posts the
    renewal with its seconds; the landing flashes the extended
    lifetime in the countdown's own vocabulary and refreshes
    the listing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 2)
    monkeypatch.setattr(tab, "pick_ttl", lambda model: "6h")
    bridge.calls.clear()
    tab.renew_row(model)
    assert bridge.calls == [
        (secrets_mod.RENEW_KEY, "renew_secret", (2, 21600))
    ]
    tab.on_succeeded(
        secrets_mod.RENEW_KEY,
        {"id": 2, "expires_at": (NOW + timedelta(hours=6)).isoformat()},
    )
    assert tab.status.text() == ("renewed ws1,ws2/deploy_key · expires 6h")
    assert bridge.calls[-1] == (secrets_mod.PAGE_KEY, "secrets_page", ())
    window.close()


def test_the_renew_picker_reads_the_default(qt, monkeypatch) -> None:
    """The renew picker's opening choice (#966): the
    presenter's nearest choice over the row's raw deadline — a
    cancelled picker decides nothing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 2)
    picked: dict = {}

    def fake_pick(parent, title, label, choices, current):
        picked["choices"] = choices
        picked["current"] = current
        return None

    monkeypatch.setattr(secrets_mod, "pick_item", fake_pick)
    assert tab.pick_ttl(model) is None
    assert picked["choices"] == list(secretrows_mod.SECRET_TTLS)
    assert picked["current"] == "1h"
    window.close()


def test_revoke_asks_first_and_posts_the_delete(qt, monkeypatch) -> None:
    """The revoke action (#966): a No on the confirmation sends
    nothing to the daemon; a Yes posts the DELETE ``secret
    revoke`` makes, the landing flashing the retired row's label
    and refreshing the listing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)
    bridge.calls.clear()
    monkeypatch.setattr(tab, "confirm_revoke", lambda model: False)
    tab.revoke_row(model)
    assert bridge.calls == []
    monkeypatch.setattr(tab, "confirm_revoke", lambda model: True)
    tab.revoke_row(model)
    assert bridge.calls == [(secrets_mod.REVOKE_KEY, "revoke_secret", (1,))]
    tab.on_succeeded(secrets_mod.REVOKE_KEY, {"revoked": 1})
    assert tab.status.text() == "revoked */github_api"
    assert bridge.calls[-1] == (secrets_mod.PAGE_KEY, "secrets_page", ())
    window.close()


def test_one_action_flight_at_a_time(qt, monkeypatch) -> None:
    """The action gate (#966, the listing's own rule): while a
    renew or revoke flight is out, the next action sends
    nothing; a refusal releases the gate and flashes."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)
    bridge.calls.clear()
    monkeypatch.setattr(tab, "pick_ttl", lambda model: "1h")
    monkeypatch.setattr(tab, "confirm_revoke", lambda model: True)
    tab.renew_row(model)
    tab.revoke_row(model)
    assert len(bridge.calls) == 1
    tab.on_failed(secrets_mod.RENEW_KEY, RestFailure("refused", reach=False))
    assert not tab.action_pending
    assert tab.status.text() == "refused"
    tab.revoke_row(model)
    assert len(bridge.calls) == 2
    window.close()


def test_the_row_menu_carries_the_two_actions(qt, monkeypatch) -> None:
    """A right-click on a placeholder row opens its menu
    (#966, #997): the renew and the revoke — each driving its
    own action; a press on no row opens nothing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    bridge.calls.clear()
    menu = tab.row_menu(row_of_id(tab, 1))
    assert [action.text() for action in menu.actions()] == [
        "Renew…",
        "Revoke…",
    ]
    assert tab.row_menu(99) is None
    monkeypatch.setattr(tab, "pick_ttl", lambda model: "7d")
    menu.actions()[0].trigger()
    assert bridge.calls[-1] == (
        secrets_mod.RENEW_KEY,
        "renew_secret",
        (1, 604800),
    )
    window.close()


def test_the_mint_form_checks_its_fields_locally(qt) -> None:
    """The mint form's local checks (#966, the TUI form's own
    rules): each refusal names itself on the note line and
    sends nothing — the name's shape, the destinations, the
    scoped pick, the value."""
    bridge = StubBridge()
    mint_mod.MintDialog(bridge)
    cases = [
        (
            {},
            "a name is required",
        ),
        (
            {"name": "1bad"},
            "name: letters, numbers, or underscores, no leading digit",
        ),
        (
            {"name": "ok"},
            "at least one destination is required",
        ),
        (
            {"name": "ok", "dests": "bad one, api.github.com"},
            "dest bad one: an exact hostname or a suffix like .example.com",
        ),
        (
            {"name": "ok", "dests": "api.github.com"},
            "a value is required",
        ),
        (
            {
                "name": "ok",
                "dests": "api.github.com",
                "coverage": "scoped",
                "workspaces": [],
                "value": "x",
            },
            "scoped coverage picks at least one workspace",
        ),
    ]
    for values, note in cases:
        body, error = cells.mint_body(values)
        assert body is None
        assert error == note
    assert bridge.calls == []


def test_the_mint_body_carries_the_picked_ttl(qt) -> None:
    """The whole mint body (#966): the scoped pick's sorted
    workspaces, the value, and the picked ``ttl_s`` — the
    unbounded pick sending none, the daemon's own default."""
    body, error = cells.mint_body(
        {
            "name": " github_api ",
            "dests": " api.github.com , .example.com ",
            "coverage": "scoped",
            "workspaces": ["ws2", "ws1"],
            "ttl": "6h",
            "value": " the real secret ",
        }
    )
    assert error == ""
    assert body == {
        "name": "github_api",
        "dests": ["api.github.com", ".example.com"],
        "workspaces": ["ws1", "ws2"],
        "ttl_s": 21600,
        "value": "the real secret",
    }
    body, _error = cells.mint_body(
        {
            "name": "github_api",
            "dests": "api.github.com",
            "coverage": "daemon-wide",
            "workspaces": [],
            "ttl": "unbounded",
            "value": "x",
        }
    )
    assert "ttl_s" not in body
    assert "workspaces" not in body


def test_the_mint_dialog_posts_and_lands(qt, monkeypatch) -> None:
    """The mint dialog's flight (#966): the submit posts the
    body through the bridge with the Mint button held; a landed
    mint accepts with its reply — the row carrying the sentinel
    the page no longer displays (#997) — and the tab refreshes
    and flashes the row's label. A cancel decides nothing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    dialog = mint_mod.MintDialog(bridge)
    dialog.show()
    assert (
        mint_mod.WORKSPACES_KEY,
        "workspaces",
        (),
    ) in bridge.calls
    dialog.on_succeeded(
        mint_mod.WORKSPACES_KEY,
        [{"id": "ws1", "name": "web"}, {"id": "ws2"}],
    )
    assert dialog.workspaces.count() == 2
    dialog.name.setText("github_api")
    dialog.dests.setText("api.github.com")
    dialog.value.setText("the real secret")
    dialog.ttl.setCurrentIndex(3)  # 1d
    bridge.calls.clear()
    dialog.submit()
    assert bridge.calls == [
        (
            mint_mod.MINT_KEY,
            "mint_secret",
            (
                {
                    "name": "github_api",
                    "dests": ["api.github.com"],
                    "ttl_s": 86400,
                    "value": "the real secret",
                },
            ),
        )
    ]
    assert not dialog.mint_button.isEnabled()
    reply = dict(placeholder(id=9), sentinel=WIDE)
    dialog.on_succeeded(mint_mod.MINT_KEY, reply)
    assert dialog.reply == reply
    bridge.calls.clear()
    landed = SimpleNamespace(reply=reply, exec=lambda: 1)
    monkeypatch.setattr(
        secrets_mod, "MintDialog", lambda bridge, parent=None: landed
    )
    tab.open_mint()
    assert bridge.calls == [(secrets_mod.PAGE_KEY, "secrets_page", ())]
    assert tab.status.text() == "minted */github_api"
    window.close()


def test_a_refused_mint_keeps_the_fields(qt) -> None:
    """A refused mint (#966): the failure lands on the note
    line with the fields kept for a retry."""
    bridge = StubBridge()
    dialog = mint_mod.MintDialog(bridge)
    dialog.name.setText("github_api")
    dialog.dests.setText("api.github.com")
    dialog.value.setText("x")
    dialog.submit()
    dialog.on_failed(mint_mod.MINT_KEY, RestFailure("name taken", reach=False))
    assert dialog.mint_button.isEnabled()
    assert dialog.note.text() == "name taken"
    assert dialog.name.text() == "github_api"
    dialog.on_failed(
        mint_mod.WORKSPACES_KEY, RestFailure("no list", reach=False)
    )
    assert dialog.note.text() == "workspace list failed: no list"


def test_the_flash_clears_after_its_span(qt, monkeypatch) -> None:
    """The flash machinery (#939's, the tab's own): a refusal's
    message clears after its span, the standing counts retire a
    timeout still pending."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.flash("a refusal")
    assert tab.status.text() == "a refusal"
    tab.clear_stale(tab.flash_seq)
    assert tab.status.text() == ""
    tab.flash("another")
    stale = tab.flash_seq
    tab.standing()
    tab.clear_stale(stale)
    assert tab.status.text() == ""
    window.close()


def test_a_half_built_wrapper_fetches_nothing(qt) -> None:
    """A wrapper without its ``bridge`` (#959): Qt can deliver
    events to a widget whose construction never finished — the
    show fetches nothing and starts no tick."""
    bridge = StubBridge()
    tab = secrets_mod.SecretsTab(bridge)
    del tab.bridge
    tab.showEvent(qt.QtGui.QShowEvent())
    assert bridge.calls == []
    assert not tab.pending


def test_a_half_built_wrapper_sizes_nothing_on_resize(qt) -> None:
    """A wrapper without its tables (#959, #997): a resize event
    Qt delivers mid-construction sizes nothing — the guard
    answers for the tables that do not exist yet."""
    bridge = StubBridge()
    tab = secrets_mod.SecretsTab(bridge)
    del tab.audit_table
    size = qt.QtCore.QSize(800, 600)
    tab.resizeEvent(qt.QtGui.QResizeEvent(size, size))
    assert bridge.calls == []


def test_the_package_presents_its_parts_lazily() -> None:
    """The package's lazy presentation (#966, the images
    package's own pattern): importing ``gui.secrets`` alone
    loads no Qt, and a name the package does not carry answers
    its own AttributeError."""
    import sys

    for name in list(sys.modules):
        if name.startswith("msks.client.gui.secrets"):
            del sys.modules[name]
    import msks.client.gui.secrets as package

    assert not any(
        name.startswith("msks.client.gui.secrets.") for name in sys.modules
    )
    assert package.SecretsTab is not None  # the lazy load happens
    import pytest

    with pytest.raises(AttributeError):
        package.no_such_part


def test_the_mint_checks_match_the_tui_form() -> None:
    """The mirrored mint vocabulary keeps parity with the TUI
    form (#966): the name and destination patterns, the
    comma-splitting, and the refusals agree over the same
    corpus — a rule change on one side must fail here rather
    than drift the two forms apart."""
    from msks.client.tui.secrets import cells as tui_cells

    assert cells.MINT_NAME.pattern == tui_cells.MINT_NAME.pattern
    assert cells.DEST_ENTRY.pattern == tui_cells.DEST_ENTRY.pattern
    assert cells.COVERAGE_WIDE == tui_cells.COVERAGE_WIDE
    assert cells.COVERAGE_SCOPED == tui_cells.COVERAGE_SCOPED
    for field in ("a, b , , c", " x ", ",", "single"):
        assert cells.split_entries(field) == tui_cells.split_entries(field)
    for entry in (
        "api.github.com",
        ".example.com",
        "a.b.example",
        "no space allowed",
        "-leading",
        "trailing-",
        "UPPER",
        "dot.",
    ):
        assert (cells.refused_dest([entry]) is None) == (
            tui_cells.refused_dest([entry]) is None
        )


def test_the_bar_buttons_cross_by_arrows(qt, monkeypatch) -> None:
    """The bar's arrow bridges (#962's rule, the tab's own): the
    refresh and the Mint buttons answer Up and Down themselves —
    handing the keyboard onward from where the operator stands —
    and leave every other event to Qt's own handling."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    key = qt.QtCore.Qt.Key
    press = qt.QtCore.QEvent.Type.KeyPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier
    down = qt.QtGui.QKeyEvent(press, key.Key_Down, mods)
    up = qt.QtGui.QKeyEvent(press, key.Key_Up, mods)
    other = qt.QtGui.QKeyEvent(press, key.Key_Left, mods)
    for button in (tab.refresh_button, tab.mint_button):
        assert tab.eventFilter(button, down) is True
        assert tab.eventFilter(button, up) is True
        assert tab.eventFilter(button, other) is False
    assert tab.eventFilter(tab.table, down) is False
    window.close()


def test_the_revoke_confirmation_answers_the_operator(qt, monkeypatch) -> None:
    """The revoke's own gate (#966): the ask over the
    presenter's question — No keeps the row, Yes retires it.
    The ask's No default is the shared question's own rule
    (#1014), pinned beside the seam that builds it."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)
    monkeypatch.setattr(cells, "revoke_question", lambda model: "the question")
    answers = iter([False, True])
    monkeypatch.setattr(secrets_mod, "confirm", lambda *args: next(answers))
    assert tab.confirm_revoke(model) is False
    assert tab.confirm_revoke(model) is True
    window.close()


def test_a_press_below_the_rows_opens_no_menu(qt, monkeypatch) -> None:
    """A right-click below the rows (#966): rowAt reads no row,
    the guard opens nothing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    bridge.calls.clear()
    tab.menu_requested(qt.QtCore.QPoint(0, 10000))
    assert bridge.calls == []
    window.close()


def test_a_cancelled_renew_picker_sends_nothing(qt, monkeypatch) -> None:
    """A cancelled renew picker (#966): no flight, the gate left
    open — and a pick that answers a choice posts it."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)
    bridge.calls.clear()
    monkeypatch.setattr(tab, "pick_ttl", lambda model: None)
    tab.renew_row(model)
    assert bridge.calls == []
    assert not tab.action_pending
    monkeypatch.setattr(tab, "pick_ttl", lambda model: "7d")
    tab.renew_row(model)
    assert bridge.calls == [
        (secrets_mod.RENEW_KEY, "renew_secret", (1, 604800))
    ]
    window.close()


def test_the_renew_picker_posts_a_made_choice(qt, monkeypatch) -> None:
    """The picker's made choice (#966): an ok pick answers
    itself, the choice the flight carries."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)

    def fake_pick(parent, title, label, choices, current):
        return "30d"

    monkeypatch.setattr(secrets_mod, "pick_item", fake_pick)
    bridge.calls.clear()
    assert tab.pick_ttl(model) == "30d"
    tab.renew_row(model)
    assert bridge.calls == [
        (secrets_mod.RENEW_KEY, "renew_secret", (1, 2592000))
    ]
    window.close()


def test_the_tick_skips_a_half_filled_row(qt, monkeypatch) -> None:
    """The tick's half-filled guard (#966): a row the table
    counts but holds no cells for — a fill the repaint raced —
    answers nothing, and the filled rows still repaint."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    row_of_2 = row_of_id(tab, 2)
    tab.table.setRowCount(tab.table.rowCount() + 1)
    pin_clock(monkeypatch, NOW + timedelta(hours=3))
    tab.on_tick()
    assert tab.table.item(row_of_2, cells.EXPIRES_COLUMN).text() == "expired"
    window.close()


def test_a_rebuild_with_no_hold_keeps_the_rows(qt, monkeypatch) -> None:
    """A rebuild with nothing held (#966): the restore answers
    nothing — the rows stand, the first row keeps its hold from
    the group's first fill only."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.table.clearSelection()
    tab.table.selectionModel().clearCurrentIndex()
    tab.refresh()
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer())
    assert tab.table.rowCount() == 2
    assert tab.table.selectedItems() == []
    window.close()


def test_the_revoke_question_names_the_row(qt, monkeypatch) -> None:
    """The revoke's question (#966): the row's coverage and name
    beside what a yes does."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 2)
    assert cells.revoke_question(model) == (
        "revoke ws1,ws2/deploy_key? the row retires everywhere at once"
    )
    window.close()


def test_the_mint_dialog_refuses_local_junk(qt) -> None:
    """The dialog's own submit (#966): a local refusal lands on
    the note line and posts nothing — the checks the plain body
    builder owns, driven through the form itself."""
    bridge = StubBridge()
    dialog = mint_mod.MintDialog(bridge)
    dialog.submit()
    assert dialog.note.text() == "a name is required"
    assert bridge.calls == []  # construction alone asks nothing
    dialog.name.setText("github_api")
    dialog.dests.setText("api.github.com")
    dialog.submit()
    assert dialog.note.text() == "a value is required"
    assert bridge.calls == []


def test_the_mint_dialog_cancels_its_own_flight(qt) -> None:
    """The dialog's close (#966): a dialog closed mid-flight
    cancels its own call — the late answer reaches no one — and
    the value field clears with the form."""
    cancelled: list[bool] = []

    class Flight:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            cancelled.append(True)

    class FlightBridge(StubBridge):
        def call(self, key, method, *args):
            self.calls.append((key, method, args))
            return Flight()

    bridge = FlightBridge()
    dialog = mint_mod.MintDialog(bridge)
    dialog.name.setText("github_api")
    dialog.dests.setText("api.github.com")
    dialog.value.setText("x")
    dialog.submit()
    assert len(bridge.calls) == 1
    dialog.done(0)
    assert cancelled == [True]
    assert dialog.value.text() == ""


def test_the_mint_dialog_seeds_once_and_ignores_strangers(qt) -> None:
    """The seed's once guard and the stranger keys (#966): a
    second show seeds nothing, and an answer for a key the
    dialog never took changes nothing."""
    bridge = StubBridge()
    dialog = mint_mod.MintDialog(bridge)
    dialog.show()
    dialog.showEvent(qt.QtGui.QShowEvent())
    assert bridge.calls == [(mint_mod.WORKSPACES_KEY, "workspaces", ())]
    standing = dialog.note.text()
    dialog.on_succeeded("someone-else", {})
    dialog.on_failed("someone-else", RestFailure("no", reach=False))
    assert dialog.note.text() == standing


def test_a_second_renew_while_a_flight_is_out_sends_nothing(
    qt, monkeypatch
) -> None:
    """The renew's own gate arm (#966): while a renew flight is
    out, the next renew sends nothing — the revoke's gate beside
    it."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)
    monkeypatch.setattr(tab, "pick_ttl", lambda model: "1h")
    bridge.calls.clear()
    tab.renew_row(model)
    tab.renew_row(model)
    assert len(bridge.calls) == 1


def test_a_cancelled_mint_dialog_decides_nothing(qt, monkeypatch) -> None:
    """A cancelled mint dialog (#966): no refresh, no flash —
    the cancel decides nothing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    monkeypatch.setattr(
        secrets_mod,
        "MintDialog",
        lambda bridge, parent=None: SimpleNamespace(
            reply=None, exec=lambda: 0
        ),
    )
    bridge.calls.clear()
    tab.open_mint()
    assert bridge.calls == []
    assert tab.status.text() == ""


def test_a_dialog_that_accepts_without_a_reply_decides_nothing(
    qt, monkeypatch
) -> None:
    """A mint dialog that accepted without a reply (#966): no
    refresh, no flash — the guard holds the body behind the
    reply."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    monkeypatch.setattr(
        secrets_mod,
        "MintDialog",
        lambda bridge, parent=None: SimpleNamespace(
            reply=None, exec=lambda: 1
        ),
    )
    bridge.calls.clear()
    tab.open_mint()
    assert bridge.calls == []
    assert tab.status.text() == ""


def test_a_press_on_a_row_pops_the_menu(qt, monkeypatch) -> None:
    """A right-click on a row (#966): the menu the row builds
    pops at the press's global place."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    popped: list = []
    monkeypatch.setattr(
        tab, "row_menu", lambda row: SimpleNamespace(popup=popped.append)
    )
    tab.menu_requested(qt.QtCore.QPoint(30, 40))
    assert len(popped) == 1


def test_an_actions_refresh_is_never_dropped(qt, monkeypatch) -> None:
    """The gate carries an action's refresh (#966, review round
    one): a page flight already out when the action lands does
    not swallow the re-read — the stale answer (a read issued
    before the action) lands once, then the carried flight
    replaces it with the fresh rows."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)
    monkeypatch.setattr(tab, "confirm_revoke", lambda model: True)
    tab.refresh()  # the stale read goes out
    bridge.calls.clear()
    tab.revoke_row(model)
    assert bridge.calls == [(secrets_mod.REVOKE_KEY, "revoke_secret", (1,))]
    tab.on_succeeded(secrets_mod.REVOKE_KEY, {"revoked": 1})
    assert tab.refresh_due  # the gate carried the ask
    # The stale page answer lands — the revoked row still shows.
    tab.on_succeeded(
        secrets_mod.PAGE_KEY, page_answer(secrets=[placeholder()])
    )
    assert {m.id for m in tab.secret_models} == {1}
    assert not tab.refresh_due  # the carried flight flew
    assert bridge.calls[-1] == (secrets_mod.PAGE_KEY, "secrets_page", ())
    # Its landing shows the fresh rows.
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer(secrets=[]))
    assert tab.secret_models == []
    window.close()


def test_a_queued_mint_answer_after_the_close_reaches_no_one(qt) -> None:
    """The close race (#966, review round one): an answer already
    queued when the dialog closes is ignored — the closed dialog
    accepts nothing, keeps no reply, and the value field stays
    cleared."""
    bridge = StubBridge()
    dialog = mint_mod.MintDialog(bridge)
    dialog.name.setText("github_api")
    dialog.dests.setText("api.github.com")
    dialog.value.setText("x")
    dialog.submit()
    dialog.done(0)
    dialog.on_succeeded(
        mint_mod.MINT_KEY, dict(placeholder(id=9), sentinel=WIDE)
    )
    dialog.on_failed(mint_mod.MINT_KEY, RestFailure("late", reach=False))
    assert dialog.reply is None
    assert dialog.value.text() == ""


def test_the_listing_keeps_the_daemons_order_until_a_click(
    qt, monkeypatch
) -> None:
    """No standing sort (#966, review round one): the listing
    renders the daemon's own order — the insertion order the
    presenter carries — and a header click orders it on
    request."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    assert [
        tab.table.item(row, 0).data(cells.ROW_KEY_ROLE)
        for row in range(tab.table.rowCount())
    ] == [1, 2]
    tab.table.sortByColumn(
        cells.EXPIRES_COLUMN,
        qt.QtCore.Qt.SortOrder.DescendingOrder,
    )
    assert [
        tab.table.item(row, 0).data(cells.ROW_KEY_ROLE)
        for row in range(tab.table.rowCount())
    ] == [1, 2]  # unbounded last on the descending countdown
    window.close()


def test_the_listing_edges_hand_the_page_order(qt, monkeypatch) -> None:
    """The listing's Left/Right edges (#966, #997): Right on the
    last column hands the keyboard down to the audit group —
    the page's reading order, measured where the focus actually
    went — Left on the first hands it back up the page to the
    bar, and the interior columns still walk cells."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    key = qt.QtCore.Qt.Key
    press = qt.QtCore.QEvent.Type.KeyPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier

    def pressed(table, key_event):
        table.setFocus()
        qt.app.processEvents()
        table.keyPressEvent(key_event)
        qt.app.processEvents()
        return qt.app.focusWidget()

    table = tab.table
    table.setCurrentCell(0, len(cells.LIST_COLUMNS) - 1)
    assert (
        pressed(table, qt.QtGui.QKeyEvent(press, key.Key_Right, mods))
        is tab.audit_table
    )
    table.setCurrentCell(0, 0)
    assert (
        pressed(table, qt.QtGui.QKeyEvent(press, key.Key_Left, mods))
        is tab.mint_button
    )
    # An interior column's Right still walks the cells — the
    # base table's own move, handed back to Qt.
    table.setCurrentCell(0, 1)
    table.setFocus()
    table.keyPressEvent(qt.QtGui.QKeyEvent(press, key.Key_Right, mods))
    assert table.currentColumn() == 2
    window.close()


def test_the_mint_picker_edges_hand_the_walk_back(qt) -> None:
    """The mint picker's edge bridges (#966, review round one):
    Up from the first option and Down from the last leave the
    list for the form — the interior options still walk."""
    bridge = StubBridge()
    dialog = mint_mod.MintDialog(bridge)
    dialog.on_succeeded(
        mint_mod.WORKSPACES_KEY,
        [{"id": "ws1", "name": "web"}, {"id": "ws2"}],
    )
    picker = dialog.workspaces
    key = qt.QtCore.Qt.Key
    press = qt.QtCore.QEvent.Type.KeyPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier
    picker.setCurrentRow(0)
    assert (
        picker.keyPressEvent(qt.QtGui.QKeyEvent(press, key.Key_Up, mods))
        is None
    )
    picker.setCurrentRow(1)
    assert (
        picker.keyPressEvent(qt.QtGui.QKeyEvent(press, key.Key_Down, mods))
        is None
    )
    picker.setCurrentRow(0)
    picker.keyPressEvent(qt.QtGui.QKeyEvent(press, key.Key_Down, mods))
    assert picker.currentRow() == 1


def test_a_hidden_tab_stops_its_tick(qt, monkeypatch) -> None:
    """The tick's lifetime (#966, review round one): a hidden tab
    repaints nothing — the timer stops with the hide and starts
    again with the show."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    assert tab.tick_timer.isActive()
    tab.hideEvent(qt.QtGui.QHideEvent())
    assert not tab.tick_timer.isActive()
    tab.showEvent(qt.QtGui.QShowEvent())
    assert tab.tick_timer.isActive()
    window.close()


def test_a_failed_page_flight_releases_the_carried_ask(
    qt, monkeypatch
) -> None:
    """The carried ask dies with its flight (#966, review round
    two): a page flight that fails clears the carry — a later
    manual retry lands once, with no refresh nobody asked for
    firing after it."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    tab.refresh_due = True
    tab.on_failed(secrets_mod.PAGE_KEY, RestFailure("outage", reach=True))
    assert not tab.refresh_due
    bridge.calls.clear()
    tab.refresh()
    tab.on_succeeded(secrets_mod.PAGE_KEY, page_answer())
    assert bridge.calls == [(secrets_mod.PAGE_KEY, "secrets_page", ())]
    window.close()
