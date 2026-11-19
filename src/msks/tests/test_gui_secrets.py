"""The GUI secrets tab (#966): the presenter's models rendered
as the placeholder listing with the sentinel panel beside it
and the audit group beneath — the TTL countdown repainted from
the deadline epochs, the mint form, the renew picker, the
revoke gate, and the confirm-gated reveal, every daemon
exchange through the stub bridge's signal emits, the bridge's
own tests holding the real worker's contract. The widget runs
under the offscreen platform on every host (#939's fixture
shape)."""

import importlib.util
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client import ages as ages_mod  # noqa: E402
from msks.client.data import RestFailure  # noqa: E402
from msks.client.gui import secretrows as secretrows_mod  # noqa: E402
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

SCOPED = "mskssec1_" + "s" * 24
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


def test_activation_fetches_the_sentinel_masked(qt, monkeypatch) -> None:
    """An activated row fetches its sentinel on demand (#966,
    over #440): the on-demand read through the bridge, and the
    panel fills masked — the prefix names the reach, the body
    renders bullets, the token never prints."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    bridge.calls.clear()
    tab.table.activated.emit(tab.table.model().index(row_of_id(tab, 1), 0))
    assert bridge.calls == [(secrets_mod.SENTINEL_KEY, "secret", (1,))]
    tab.on_succeeded(
        secrets_mod.SENTINEL_KEY,
        dict(placeholder(), sentinel=WIDE),
    )
    assert tab.panel.label.text() == "*/github_api"
    assert tab.panel.sentinel.text() == "mskssec2_" + "\u2022" * 24
    assert tab.panel.reach.text() == "reach: every accepting workspace"
    assert WIDE not in tab.panel.sentinel.text()
    window.close()


def test_the_sentinel_reveal_asks_first(qt, monkeypatch) -> None:
    """The reveal's gate (#966): a No keeps the mask, a Yes
    puts the real token on the panel, and Hide masks it back —
    the token leaves the display the moment the operator turns
    it off."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.panel.show_row(dict(placeholder(), sentinel=SCOPED))
    answers = iter([False, True])
    monkeypatch.setattr(tab.panel, "confirm_reveal", lambda: next(answers))
    tab.panel.toggle_reveal()
    assert tab.panel.sentinel.text() == "mskssec1_" + "\u2022" * 24
    tab.panel.toggle_reveal()
    assert tab.panel.sentinel.text() == SCOPED
    assert tab.panel.show_button.text() == "Hide"
    tab.panel.toggle_reveal()
    assert tab.panel.sentinel.text() == "mskssec1_" + "\u2022" * 24
    assert tab.panel.show_button.text() == "Show"
    window.close()


def test_the_sentinel_copy_lands_the_real_token(qt, monkeypatch) -> None:
    """The copy (#966): masked or revealed, the clipboard gets
    the real sentinel, and the note says what happened. With no
    row on the panel, neither button answers."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    qt.app.clipboard().setText("")
    assert not tab.panel.copy_button.isEnabled()
    tab.panel.show_row(dict(placeholder(), sentinel=WIDE))
    tab.panel.copy()
    assert qt.app.clipboard().text() == WIDE
    assert tab.panel.note.text() == "copied to the clipboard"
    window.close()


def test_the_panel_clears_its_lines(qt, monkeypatch) -> None:
    """Clearing the panel (#966): every line empties — the
    sentinel leaves the widget tree with the row that carried
    it — and the note returns to its idle line."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.panel.show_row(dict(placeholder(), sentinel=WIDE))
    tab.panel.clear()
    assert tab.panel.label.text() == ""
    assert tab.panel.sentinel.text() == ""
    assert tab.panel.reach.text() == ""
    assert tab.panel.note.text() == cells.PANEL_IDLE
    assert not tab.panel.show_button.isEnabled()
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

    def fake_get_item(parent, title, label, choices, current, editable):
        picked["choices"] = choices
        picked["current"] = current
        return "1d", False

    monkeypatch.setattr(
        qt.QtWidgets.QInputDialog, "getItem", staticmethod(fake_get_item)
    )
    assert tab.pick_ttl(model) is None
    assert picked["choices"] == secretrows_mod.SECRET_TTLS
    assert picked["current"] == secretrows_mod.SECRET_TTLS.index("1h")
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


def test_the_row_menu_carries_the_three_actions(qt, monkeypatch) -> None:
    """A right-click on a placeholder row opens its menu
    (#966): the sentinel fetch, the renew, and the revoke —
    each driving its own action; a press on no row opens
    nothing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    bridge.calls.clear()
    menu = tab.row_menu(row_of_id(tab, 1))
    assert [action.text() for action in menu.actions()] == [
        "Show the sentinel",
        "Renew…",
        "Revoke…",
    ]
    assert tab.row_menu(99) is None
    menu.actions()[0].trigger()
    assert bridge.calls == [(secrets_mod.SENTINEL_KEY, "secret", (1,))]
    monkeypatch.setattr(tab, "pick_ttl", lambda model: "7d")
    menu.actions()[1].trigger()
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
    — and the tab refreshes, fills the panel with the fresh
    sentinel, and flashes the row's label. A cancel decides
    nothing."""
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
    assert tab.panel.sentinel.text() == "mskssec2_" + "\u2022" * 24
    assert tab.status.text() == "minted */github_api"
    window.close()


def test_a_refused_mint_keeps_the_fields(qt) -> None:
    """A refused mint (#966): the failure lands on the note
    line with the fields kept for a retry — the sentinel panel
    fills with nothing."""
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


def test_the_idle_panel_answers_no_action(qt, monkeypatch) -> None:
    """The idle panel's guards (#966): with no row on it, the
    reveal and the copy answer nothing — the clipboard keeps
    whatever it held, the ask never opens."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    qt.app.clipboard().setText("kept")
    asked: list[bool] = []

    def no_ask() -> bool:
        asked.append(True)
        return True

    monkeypatch.setattr(tab.panel, "confirm_reveal", no_ask)
    tab.panel.toggle_reveal()
    tab.panel.copy()
    assert asked == []
    assert qt.app.clipboard().text() == "kept"
    window.close()


def test_the_reveal_confirmation_answers_the_operator(qt, monkeypatch) -> None:
    """The reveal's own gate (#966): the real ask — No keeps the
    mask, Yes reveals — with No the default answer."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.panel.show_row(dict(placeholder(), sentinel=WIDE))
    answers = iter(
        [qt.QtWidgets.QMessageBox.StandardButton.No]
        + [qt.QtWidgets.QMessageBox.StandardButton.Yes]
    )
    monkeypatch.setattr(
        qt.QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: next(answers),
    )
    assert tab.panel.confirm_reveal() is False
    assert tab.panel.confirm_reveal() is True
    window.close()


def test_the_revoke_confirmation_answers_the_operator(qt, monkeypatch) -> None:
    """The revoke's own gate (#966): the real ask over the
    presenter's question — No keeps the row, Yes retires it —
    with No the default answer."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    model = next(m for m in tab.secret_models if m.id == 1)
    monkeypatch.setattr(cells, "revoke_question", lambda model: "the question")
    answers = iter(
        [qt.QtWidgets.QMessageBox.StandardButton.No]
        + [qt.QtWidgets.QMessageBox.StandardButton.Yes]
    )
    monkeypatch.setattr(
        qt.QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: next(answers),
    )
    assert tab.confirm_revoke(model) is False
    assert tab.confirm_revoke(model) is True
    window.close()


def test_a_sentinel_fetch_failure_flashes(qt, monkeypatch) -> None:
    """A refused sentinel fetch (#966): the failure flashes on
    the status line and the panel keeps whatever it held."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.on_failed(secrets_mod.SENTINEL_KEY, RestFailure("locked", reach=False))
    assert tab.status.text() == "locked"
    window.close()


def test_an_activation_below_the_rows_fetches_nothing(qt, monkeypatch) -> None:
    """An activation on a row the table does not hold (#966):
    the half-filled guard answers nothing — no sentinel
    flight."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    bridge.calls.clear()
    tab.sentinel_at(tab.table.model().index(99, 0))
    assert bridge.calls == []
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

    def fake_get_item(parent, title, label, choices, current, editable):
        return "30d", True

    monkeypatch.setattr(
        qt.QtWidgets.QInputDialog, "getItem", staticmethod(fake_get_item)
    )
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


def test_a_non_mapping_sentinel_answer_fills_nothing(qt, monkeypatch) -> None:
    """A sentinel answer that is not a mapping (#966): the
    panel keeps whatever it held — the guard answers nothing."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    tab.panel.show_row(dict(placeholder(), sentinel=WIDE))
    standing = tab.panel.sentinel.text()
    tab.on_succeeded(secrets_mod.SENTINEL_KEY, None)
    assert tab.panel.sentinel.text() == standing


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
    """A cancelled mint dialog (#966): no refresh, no flash, no
    panel fill — the cancel decides nothing."""
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
    refresh, no flash, no panel fill — the guard holds the
    body behind the reply."""
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


def test_a_revoke_clears_the_panels_row(qt, monkeypatch) -> None:
    """The revoke and the panel (#966, review round one): a
    revoke that retires the row whose sentinel stands on the
    panel clears the panel with it — the token leaves the widget
    tree with its row — and another row's revoke leaves the
    panel standing."""
    tab, window, bridge = make_tab(qt, monkeypatch)
    tab.panel.show_row(dict(placeholder(id=2), sentinel=SCOPED))
    monkeypatch.setattr(tab, "confirm_revoke", lambda model: True)
    model = next(m for m in tab.secret_models if m.id == 1)
    tab.revoke_row(model)
    tab.on_succeeded(secrets_mod.REVOKE_KEY, {"revoked": 1})
    assert tab.panel.row is not None  # another row's revoke: stands
    model = next(m for m in tab.secret_models if m.id == 2)
    tab.revoke_row(model)
    tab.on_succeeded(secrets_mod.REVOKE_KEY, {"revoked": 2})
    assert tab.panel.row is None
    assert tab.panel.sentinel.text() == ""
    window.close()


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


def test_the_listing_edges_cross_to_the_pane(qt, monkeypatch) -> None:
    """The listing's Left/Right edges (#966, review round two):
    Right on the last column lands on the pane's Show button —
    the explicit destination, measured where the focus actually
    went — Left on the first hands the keyboard back up the
    page, and the interior columns still walk cells. A pane with
    no row on it (its buttons disabled) falls to the chain."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    key = qt.QtCore.Qt.Key
    press = qt.QtCore.QEvent.Type.KeyPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier

    # The pane armed: its Show button holds the crossing's
    # destination (an idle pane falls to the chain — the last
    # assertion below).
    tab.panel.show_row(dict(placeholder(), sentinel=WIDE))

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
        is tab.panel.show_button
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
    # The disabled pane falls to the chain: the crossing's own
    # target holds nothing to focus.
    tab.panel.clear()
    table.setCurrentCell(0, len(cells.LIST_COLUMNS) - 1)
    assert (
        pressed(table, qt.QtGui.QKeyEvent(press, key.Key_Right, mods))
        is not tab.panel.show_button
    )
    window.close()


def test_the_panel_buttons_cross_by_arrows(qt, monkeypatch) -> None:
    """The pane's arrow bridges (#966, review round one): the
    Show and Copy buttons answer Up and Down themselves, and
    leave every other event to Qt's own handling."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    key = qt.QtCore.Qt.Key
    press = qt.QtCore.QEvent.Type.KeyPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier
    down = qt.QtGui.QKeyEvent(press, key.Key_Down, mods)
    up = qt.QtGui.QKeyEvent(press, key.Key_Up, mods)
    other = qt.QtGui.QKeyEvent(press, key.Key_Left, mods)
    for button in (tab.panel.show_button, tab.panel.copy_button):
        assert tab.panel.eventFilter(button, down) is True
        assert tab.panel.eventFilter(button, up) is True
        assert tab.panel.eventFilter(button, other) is False
    assert tab.panel.eventFilter(tab.table, down) is False
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


def test_the_confirmations_default_to_no(qt, monkeypatch) -> None:
    """The gates' defaults (#966, review round one): both asks
    carry No as the default answer — the token and the row stay
    unless the operator says otherwise."""
    tab, window, _bridge = make_tab(qt, monkeypatch)
    defaults: list[object] = []
    no = qt.QtWidgets.QMessageBox.StandardButton.No

    def recorder(*args, **kwargs):
        defaults.append(
            kwargs.get("defaultButton", args[-1] if args else None)
        )
        return no

    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "question", recorder)
    tab.panel.show_row(dict(placeholder(), sentinel=WIDE))
    tab.panel.confirm_reveal()
    model = next(m for m in tab.secret_models if m.id == 1)
    tab.confirm_revoke(model)
    assert defaults == [no, no]
    window.close()


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
