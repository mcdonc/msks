"""The GUI images tab (#964): the presenter's models rendered as
the two labeled groups, the import behind its progress dialog,
the remove behind its confirmation, and the default designation
— every daemon exchange through the stub bridge's signal
emits, the bridge's own tests holding the real worker's
contract. The widget runs under the offscreen platform on every
host (#939's fixture shape).
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

from msks.client.config import DEFAULT_IMAGE_MANIFEST_URL  # noqa: E402
from msks.client.data import RestFailure  # noqa: E402
from msks.client.gui.imagerows import IMPORTED_MARKER  # noqa: E402
from msks.client.gui.images import cells as cells  # noqa: E402
from msks.client.gui.images import layout as layout_mod  # noqa: E402
from msks.client.gui.images import tab as images_mod  # noqa: E402

CHANNEL = "https://ch.example/manifest.json"


class FakeSignal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once, in the GUI thread
    the real signal's queued delivery would land them in."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

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


def catalog_raw() -> list[dict]:
    """The daemon's catalog as the gathered read carries it: the
    designated default first in reading order, a second row
    beside it."""
    return [
        {
            "name": "debian",
            "version": "13.6",
            "hash": "a" * 64,
            "default": True,
            "imported": "2026-01-02T03:04:05",
            "kernel_version": "6.1.0",
            "kernel_format": "gz",
            "cmdline": "console=ttyS0",
            "description": "Debian 13 trixie",
        },
        {
            "name": "ubuntu",
            "version": "24.04",
            "hash": "b" * 64,
            "default": False,
            "imported": "2026-01-03T03:04:05",
        },
    ]


def published_raw() -> list[dict]:
    """The channel's manifest as the gathered read carries it:
    the exact Debian build the catalog holds, an Alpine row the
    catalog does not."""
    return [
        {
            "ref": "debian:13.6",
            "url": "https://ch.example/debian.tar.zst",
            "hash": "a" * 64,
            "description": "Debian 13 trixie",
            "size": 104_857_600,
            "built_at": "2026-01-01T00:00:00Z",
            "arch": "x86_64",
        },
        {
            "ref": "alpine:3.21",
            "url": "https://ch.example/alpine.tar.zst",
            "hash": "c" * 64,
            "size": 52_428_800,
        },
    ]


def page_answer(
    catalog: list[dict] | None = None, published: list[dict] | None = None
) -> dict:
    """The gathered read's answer shape (#964)."""
    return {
        "catalog": catalog_raw() if catalog is None else catalog,
        "published": published_raw() if published is None else published,
    }


def make_tab(qt) -> tuple:
    """The tab over the stub bridge, shown in a real window —
    the shape every test reads: open, first refresh issued and
    answered, both groups standing. The window opens wide
    enough that every whole value fits and nothing ellipsizes."""
    bridge = StubBridge()
    tab = images_mod.ImagesTab(bridge, CHANNEL)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    # Wide enough that every whole value fits under any CI
    # host's font metrics — the mac runner's faces run wider
    # than the Linux ones, and the elide arithmetic is the
    # narrow-pane tests' own subject, not this fixture's.
    window.resize(3200, 1200)
    window.show()
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer())
    return tab, window, bridge


def rows_texts(table) -> list[list[str]]:
    """A group table's rows as their cell texts (#964's fakes
    read): every cell of every row, in table order."""
    return [
        [
            table.item(row, column).text()
            for column in range(table.columnCount())
        ]
        for row in range(table.rowCount())
    ]


def row_of(tab, ref: str) -> int:
    """The row a published ref shows on (#964's fakes read):
    resolved through the row's own url key — the pane's display
    text ellipsizes under a narrow one, the key never does, and
    the tests follow the ref wherever its sort lands it."""
    url = next(model.url for model in tab.published_models if model.ref == ref)
    table = tab.published_table
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and (item.data(images_mod.ROW_KEY_ROLE) == url):
            return row
    raise AssertionError(f"{ref!r} shows on no row")


def test_the_open_tab_issues_its_first_refresh(qt) -> None:
    """The tab fetches when it is shown, not when the strip
    builds it (#964): a background tab asks nothing, the show
    issues the one gathered read, and no standing tick runs —
    the channel's rows do not move on their own."""
    bridge = StubBridge()
    tab = images_mod.ImagesTab(bridge, CHANNEL)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    assert bridge.calls == []  # construction alone asks nothing
    window.show()
    assert bridge.calls == [(images_mod.PAGE_KEY, "images_page", (CHANNEL,))]
    assert not tab.import_timer.isActive()
    window.close()


def test_the_page_answer_renders_both_groups(qt) -> None:
    """The two labeled groups from the presenter's models
    (#964): the catalog rows with the default marker, the hash
    prefix, and the import date; the published rows with their
    descriptions, sizes, build dates, and membership markers —
    the counts standing on the status line."""
    tab, window, _bridge = make_tab(qt)
    boxes = {
        box.title(): box for box in tab.findChildren(qt.QtWidgets.QGroupBox)
    }
    assert set(boxes) == {cells.CATALOG_GROUP, cells.PUBLISHED_GROUP}
    assert rows_texts(tab.catalog_table) == [
        [
            "debian:13.6",
            "default",
            "a" * 12,
            "2026-01-02 03:04",
        ],
        ["ubuntu:24.04", "-", "b" * 12, "2026-01-03 03:04"],
    ]
    assert rows_texts(tab.published_table) == [
        [
            "alpine:3.21",
            "-",
            "50 MiB",
            "c" * 12,
            "-",
            "",
        ],
        [
            "debian:13.6",
            "Debian 13 trixie",
            "100 MiB",
            "a" * 12,
            "2026-01-01",
            IMPORTED_MARKER,
        ],
    ]
    assert tab.counts.text() == "2 in the catalog · 2 published"
    window.close()


def test_a_later_answer_replaces_the_groups(qt) -> None:
    """Each gathered answer rebuilds the page whole (#964): the
    second answer replaces the first — no accumulation — and an
    empty catalog beside an empty channel answers the TUI page's
    own advice on the status line."""
    tab, window, _bridge = make_tab(qt)
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer([], []))
    assert tab.catalog_table.rowCount() == 0
    assert tab.published_table.rowCount() == 0
    assert tab.counts.text() == (
        "the catalog and the channel both carry nothing — "
        "import from a channel URL once one is published"
    )
    window.close()


def test_the_manual_refresh_skips_a_flight_already_out(qt) -> None:
    """The refresh guard (#964, the listing's own #470 rule): a
    refresh already in flight swallows the button's click, and
    the flight's landing releases the guard for the next one."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.pending = True
    tab.refresh_button.click()
    assert bridge.calls == []
    tab.pending = False
    tab.refresh_button.click()
    assert bridge.calls == [(images_mod.PAGE_KEY, "images_page", (CHANNEL,))]
    window.close()


def test_an_answer_for_another_key_answers_nothing(qt) -> None:
    """The tab answers only its own keys (#939's rule, the
    tab's own): a stranger's answer and a stranger's failure
    land on no group, no flash, no flight state."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    standing = tab.status.text()
    tab.on_succeeded("someone-elses-key", {"anything": 1})
    tab.on_failed("someone-elses-key", RestFailure("nope", reach=True))
    assert tab.status.text() == standing
    assert bridge.calls == []
    assert tab.catalog_table.rowCount() == 2
    window.close()


def test_a_failed_page_fetch_flashes_and_the_groups_stand(qt) -> None:
    """A refused refresh (#964, #904's contract): the failure
    flashes — an outage standing until an answer resolves it —
    while both groups keep the content they hold, and the guard
    releases so a retry can go out."""
    tab, window, _bridge = make_tab(qt)
    tab.pending = True
    tab.on_failed(
        images_mod.PAGE_KEY, RestFailure("the daemon is gone", reach=True)
    )
    assert not tab.pending
    assert tab.status.text() == "the daemon is gone"
    assert tab.catalog_table.rowCount() == 2
    tab.refresh()
    assert tab.status.text() == (
        "fetching the catalog and the published manifest…"
    )
    window.close()


def test_a_failed_catalog_read_flashes_only(qt) -> None:
    """A refused catalog refresh (#964): the failure flashes and
    the guard releases; the groups stand as they were."""
    tab, window, _bridge = make_tab(qt)
    tab.catalog_pending = True
    tab.on_failed(images_mod.CATALOG_KEY, RestFailure("locked", reach=False))
    assert not tab.catalog_pending
    assert tab.status.text() == "locked"
    assert tab.catalog_table.rowCount() == 2
    window.close()


def test_a_catalog_answer_moves_the_markers(qt) -> None:
    """The catalog-only refresh (#964): the read an action's
    landing takes rebuilds the catalog group and the published
    markers against the new rows — an imported build's marker
    turns ``[imported]`` — without refetching the channel."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.refresh_catalog()  # the import's landing flight
    landed = page_answer()["catalog"] + [
        {
            "name": "alpine",
            "version": "3.21",
            "hash": "c" * 64,
            "default": False,
            "imported": "2026-01-04T00:00:00",
        }
    ]
    tab.on_succeeded(images_mod.CATALOG_KEY, landed)
    assert bridge.calls == [
        (images_mod.CATALOG_KEY, "images", ())
    ]  # the flight this test issued; the channel was not refetched
    assert rows_texts(tab.published_table)[0][-1] == IMPORTED_MARKER
    assert tab.counts.text() == "3 in the catalog · 2 published"
    window.close()


def test_activation_on_a_catalog_row_opens_the_detail(qt, monkeypatch) -> None:
    """Enter or a double-click on a catalog row opens its detail
    (#964): the raw daemon row behind the model — the boot facts
    the row models do not carry — rendered by the shared
    ``image info`` lines."""
    opened: list[dict] = []

    class FakeDialog:
        def __init__(self, row: dict, parent=None) -> None:
            opened.append(row)

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(images_mod, "ImageDetailDialog", FakeDialog)
    tab, window, _bridge = make_tab(qt)
    tab.catalog_table.activated.emit(tab.catalog_table.model().index(1, 0))
    assert opened == [catalog_raw()[1]]
    window.close()


def test_the_detail_dialog_renders_the_info_lines(qt) -> None:
    """The detail dialog's content (#964): the shared leaf's own
    record — every line ``msks image info`` prints — selectable
    as one block, under the image's own title."""
    from msks.client.images import info_lines

    dialog = images_mod.ImageDetailDialog(catalog_raw()[0])
    label = dialog.findChild(qt.QtWidgets.QLabel)
    assert label is not None
    assert label.text() == "\n".join(info_lines(catalog_raw()[0]))
    assert label.textInteractionFlags() == (
        qt.QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
    )
    assert dialog.windowTitle() == "Image debian:13.6"


def test_activation_on_a_published_row_imports_behind_the_dialog(
    qt,
) -> None:
    """The import action (#964): the row's URL posts through the
    bridge while the progress dialog holds the wait — the tick
    running — and the answer closes the dialog, flashes the
    registered reference, and refreshes the catalog."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.published_table.activated.emit(
        tab.published_table.model().index(row_of(tab, "alpine:3.21"), 0)
    )
    assert bridge.calls == [
        (
            images_mod.IMPORT_KEY,
            "import_image",
            ("https://ch.example/alpine.tar.zst",),
        )
    ]
    assert tab.progress is not None and tab.progress.isVisible()
    assert (
        tab.progress.geometry().center() == window.geometry().center()
    )  # the wait sits on the main window's center (#1014)
    assert tab.import_timer.isActive()
    tab.on_succeeded(
        images_mod.IMPORT_KEY,
        {"ref": "alpine:3.21", "hash": "c" * 64},
    )
    assert tab.progress is None
    assert not tab.import_timer.isActive()
    assert bridge.calls[-1] == (images_mod.CATALOG_KEY, "images", ())
    assert tab.status.text() == f"imported alpine:3.21 ({'c' * 12})"
    window.close()


def test_the_exact_build_imports_nothing(qt) -> None:
    """A published row whose build already sits in the catalog
    imports nothing (#964, #565's rule): the daemon would
    download the archive again to verify bytes it holds, and the
    flash names the ref so the skip reads as intentional."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.published_table.activated.emit(
        tab.published_table.model().index(row_of(tab, "debian:13.6"), 0)
    )
    assert bridge.calls == []
    assert tab.progress is None
    assert tab.status.text() == "debian:13.6 is already in the catalog"
    window.close()


def test_a_refused_import_closes_the_dialog_and_flashes(qt) -> None:
    """A refused import (#964): the dialog closes, the tick
    stops, and the refusal flashes on the status line — the
    groups standing as they were."""
    tab, window, bridge = make_tab(qt)
    tab.start_import(tab.published_models[1])
    tab.on_failed(
        images_mod.IMPORT_KEY, RestFailure("the archive is short", reach=False)
    )
    assert tab.progress is None
    assert not tab.import_timer.isActive()
    assert tab.status.text() == "the archive is short"
    assert bridge.calls[-1] == (
        images_mod.IMPORT_KEY,
        "import_image",
        ("https://ch.example/alpine.tar.zst",),
    )
    window.close()


def test_the_import_tick_moves_the_elapsed(qt) -> None:
    """The wait's own tick (#964): the dialog's elapsed second
    follows the flight's start, the TUI importing modal's own
    cadence — a dialog already closed ticks nothing."""
    tab, window, _bridge = make_tab(qt)
    tab.start_import(tab.published_models[1])
    tab.import_started -= 3
    tab.import_tick()
    assert tab.progress.labelText().endswith("3s")
    tab.land_import()
    tab.import_tick()  # the closed dialog answers nothing
    window.close()


def test_the_default_choice_posts_the_designation(qt) -> None:
    """The default action (#964): the row's full hash posts the
    designation — the same exchange the CLI's ``image default``
    makes — behind the one-flight gate, the landing flashing the
    designation and refreshing the catalog; the row that already
    holds it flashes so and sends nothing."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.default_row(tab.catalog_models[1])
    assert bridge.calls == [
        (images_mod.DEFAULT_KEY, "set_default_image", ("b" * 64,))
    ]
    tab.on_succeeded(images_mod.DEFAULT_KEY, {"ref": "ubuntu:24.04"})
    assert tab.status.text() == "designated ubuntu:24.04 the default image"
    assert bridge.calls[-1] == (images_mod.CATALOG_KEY, "images", ())
    bridge.calls.clear()
    tab.default_row(tab.catalog_models[0])
    assert bridge.calls == []
    assert tab.status.text() == "debian:13.6 is already the default"
    window.close()


def test_remove_asks_first_and_posts_the_delete(qt, monkeypatch) -> None:
    """The remove action (#964): a No on the confirmation sends
    nothing to the daemon; a Yes posts the DELETE ``image rm``
    makes, behind the one-flight gate, the landing flashing the
    removal and refreshing the catalog."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    monkeypatch.setattr(tab, "confirm_remove", lambda ref: False)
    tab.remove_row(tab.catalog_models[1])
    assert bridge.calls == []
    monkeypatch.setattr(tab, "confirm_remove", lambda ref: True)
    tab.remove_row(tab.catalog_models[1])
    assert bridge.calls == [
        (images_mod.REMOVE_KEY, "remove_image", ("b" * 64,))
    ]
    tab.on_succeeded(images_mod.REMOVE_KEY, {"removed": "ubuntu:24.04"})
    assert tab.status.text() == "deleted ubuntu:24.04"
    assert bridge.calls[-1] == (images_mod.CATALOG_KEY, "images", ())
    window.close()


def test_one_action_flight_at_a_time(qt, monkeypatch) -> None:
    """The action gate (#964, the workspaces screen's own rule):
    while a remove or a default flight is out, the next action
    sends nothing; a refusal releases the gate and flashes."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    monkeypatch.setattr(tab, "confirm_remove", lambda ref: True)
    tab.remove_row(tab.catalog_models[1])
    tab.remove_row(tab.catalog_models[0])
    tab.default_row(tab.catalog_models[0])
    assert len(bridge.calls) == 1
    tab.on_failed(images_mod.REMOVE_KEY, RestFailure("in use", reach=False))
    assert not tab.action_pending
    assert tab.status.text() == "in use"
    tab.default_row(tab.catalog_models[1])
    assert len(bridge.calls) == 2
    window.close()


def test_the_row_menu_carries_the_three_actions(qt, monkeypatch) -> None:
    """A right-click on a catalog row opens its menu (#964): the
    detail, the default designation, and the remove — each
    driving its own action; a press on no row opens nothing."""
    opened: list[dict] = []
    monkeypatch.setattr(
        images_mod,
        "ImageDetailDialog",
        lambda row, parent=None: (
            opened.append(row) or SimpleNamespace(exec=lambda: 0)
        ),
    )
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    menu = tab.catalog_menu(0)
    assert [action.text() for action in menu.actions()] == [
        "Open the detail",
        "Set as the default",
        "Remove…",
    ]
    assert tab.catalog_menu(99) is None
    menu.actions()[0].trigger()
    assert opened == [catalog_raw()[0]]
    # The default action on a row that does not hold the
    # designation: the menu's own row, the second catalog row.
    tab.catalog_menu(1).actions()[1].trigger()
    assert bridge.calls == [
        (images_mod.DEFAULT_KEY, "set_default_image", ("b" * 64,))
    ]
    monkeypatch.setattr(tab, "confirm_remove", lambda ref: False)
    menu.actions()[2].trigger()
    assert len(bridge.calls) == 1
    window.close()


def test_the_selection_carries_across_a_rebuild(qt) -> None:
    """A refresh carries the operator's hold by row content
    (#964, the listing's own rule): the held catalog row follows
    its image wherever the rebuild lands it, and a row the new
    listing dropped simply loses its hold."""
    tab, window, _bridge = make_tab(qt)
    tab.catalog_table.selectRow(1)
    assert (
        tab.catalog_table.item(tab.catalog_table.currentRow(), 0).text()
        == "ubuntu:24.04"
    )
    # The answer drops Debian: the held image moves to the top
    # slot — the hold follows the row's content, not its slot.
    tab.refresh_catalog()
    tab.on_succeeded(images_mod.CATALOG_KEY, [catalog_raw()[1]])
    assert tab.catalog_table.rowCount() == 1
    assert tab.catalog_table.currentRow() == 0
    assert (
        tab.catalog_table.item(tab.catalog_table.currentRow(), 0).text()
        == "ubuntu:24.04"
    )
    # A rebuild that drops the held image clears the hold.
    tab.refresh_catalog()
    tab.on_succeeded(images_mod.CATALOG_KEY, [catalog_raw()[0]])
    assert tab.catalog_table.selectedItems() == []
    window.close()


def test_the_flash_clears_after_its_span(qt) -> None:
    """The flash machinery (#939's, the tab's own): a refusal's
    message clears after its span — only its own timeout clears
    it, a later flash's timeout standing down — and the standing
    counts retire a timeout still pending."""
    tab, window, _bridge = make_tab(qt)
    tab.flash("a first refusal")
    first = tab.flash_seq
    tab.flash("a second refusal")
    tab.clear_stale(first)  # the first flash's late timeout
    assert tab.status.text() == "a second refusal"
    tab.clear_stale(tab.flash_seq)
    assert tab.status.text() == ""
    tab.flash("a third")
    third = tab.flash_seq
    tab.standing("2 in the catalog · 2 published")
    tab.clear_stale(third)  # retired by the standing line
    assert tab.counts.text() == "2 in the catalog · 2 published"
    window.close()


def test_the_refresh_button_crosses_by_arrows(qt) -> None:
    """The refresh button's arrow bridges (#962's rule): the
    filter answers the Up and the Down itself — handing the
    keyboard onward from where the operator stands — and
    leaves every other event to Qt's own handling."""
    tab, window, _bridge = make_tab(qt)
    key = qt.QtCore.Qt.Key
    press = qt.QtCore.QEvent.Type.KeyPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier
    down = qt.QtGui.QKeyEvent(press, key.Key_Down, mods)
    up = qt.QtGui.QKeyEvent(press, key.Key_Up, mods)
    assert tab.eventFilter(tab.refresh_button, down) is True
    assert tab.eventFilter(tab.refresh_button, up) is True
    assert tab.eventFilter(tab.catalog_table, down) is False
    window.close()


def test_a_half_built_wrapper_fetches_nothing(qt) -> None:
    """The open's guard (#959's hardening, the tab's own read):
    a wrapper without its ``bridge`` — Qt can deliver the show
    to a widget whose construction never finished — fetches
    nothing and crashes no one."""
    bridge = StubBridge()
    tab = images_mod.ImagesTab(bridge, CHANNEL)
    del tab.bridge
    tab.showEvent(qt.QtGui.QShowEvent())
    assert bridge.calls == []
    assert not tab.pending


def test_the_channel_url_reads_the_config_override() -> None:
    """The channel source (#964): the configured override when
    one resolved, else the default the TUI page reads — one
    source both clients share."""
    assert cells.channel_url(None) == DEFAULT_IMAGE_MANIFEST_URL
    assert (
        cells.channel_url(SimpleNamespace(image_manifest_url=None))
        == DEFAULT_IMAGE_MANIFEST_URL
    )
    assert (
        cells.channel_url(
            SimpleNamespace(image_manifest_url="https://fork.example/ch.json")
        )
        == "https://fork.example/ch.json"
    )


def test_a_catalog_refresh_skips_a_flight_already_out(qt) -> None:
    """The catalog refresh's own guard (#964): a read already in
    flight swallows the next ask until its answer lands."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.catalog_pending = True
    tab.refresh_catalog()
    assert bridge.calls == []
    tab.catalog_pending = False
    tab.refresh_catalog()
    assert bridge.calls == [(images_mod.CATALOG_KEY, "images", ())]


def test_activation_on_an_empty_row_answers_nothing(qt, monkeypatch) -> None:
    """An activation the table cannot place (#964): an invalid
    index carries no row, so no detail opens and no import
    starts."""
    opened: list[object] = []
    monkeypatch.setattr(
        images_mod,
        "ImageDetailDialog",
        lambda row, parent=None: opened.append(row),
    )
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.catalog_table.activated.emit(qt.QtCore.QModelIndex())
    tab.published_table.activated.emit(qt.QtCore.QModelIndex())
    assert opened == []
    assert bridge.calls == []


def test_a_model_without_its_raw_row_opens_no_detail(qt, monkeypatch) -> None:
    """The detail's defensive arm (#964): a model whose raw row
    the cache no longer holds — a half-answered rebuild — opens
    nothing rather than a dialog over a missing record."""
    opened: list[object] = []
    monkeypatch.setattr(
        images_mod,
        "ImageDetailDialog",
        lambda row, parent=None: opened.append(row),
    )
    tab, window, _bridge = make_tab(qt)
    tab.catalog_raw = []
    tab.open_detail_row(tab.catalog_models[0])
    assert opened == []


def test_an_import_answer_without_a_record_refreshes_only(qt) -> None:
    """An import answer that carries no registered record
    (#964): nothing flashes — the counts stand — but the dialog
    closes and the catalog still refreshes."""
    tab, window, bridge = make_tab(qt)
    standing = tab.status.text()
    tab.start_import(tab.published_models[1])
    tab.on_succeeded(images_mod.IMPORT_KEY, None)
    assert tab.progress is None
    assert tab.status.text() == standing
    assert bridge.calls[-1] == (images_mod.CATALOG_KEY, "images", ())


def test_landing_an_import_that_never_started_is_safe(qt) -> None:
    """The import flight's end on an empty tab (#964): no dialog
    stands, nothing crashes, and the tick stops regardless."""
    tab, window, _bridge = make_tab(qt)
    tab.land_import()
    assert tab.progress is None
    assert not tab.import_timer.isActive()


def test_the_gated_default_sends_nothing(qt) -> None:
    """The default action behind the gate (#964): a flight
    already out swallows the designation ask from a row that
    does not hold it."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.action_pending = True
    tab.default_row(tab.catalog_models[1])
    assert bridge.calls == []


def test_the_remove_confirmation_answers_the_operator(qt) -> None:
    """Remove's confirmation (#964): the real question dialog
    under the modal loop — the operator's No answers False and
    sends nothing, the Yes answers True."""
    tab, window, _bridge = make_tab(qt)

    def answer(role) -> None:
        box = qt.app.activeModalWidget()
        if isinstance(box, qt.QtWidgets.QMessageBox):
            for button in box.buttons():
                if box.buttonRole(button) == role:
                    button.click()

    no = qt.QtWidgets.QMessageBox.ButtonRole.NoRole
    yes = qt.QtWidgets.QMessageBox.ButtonRole.YesRole
    qt.QtCore.QTimer.singleShot(0, lambda: answer(no))
    assert tab.confirm_remove("debian:13.6") is False
    qt.QtCore.QTimer.singleShot(0, lambda: answer(yes))
    assert tab.confirm_remove("debian:13.6") is True
    window.close()


def test_a_press_below_the_rows_opens_no_menu(qt) -> None:
    """A context-menu press on no row (#964): nothing opens, and
    the table's rows answer nothing."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.menu_requested(qt.QtCore.QPoint(2, 10_000))
    tab.published_menu_requested(qt.QtCore.QPoint(2, 10_000))
    assert bridge.calls == []


def test_the_button_filter_leaves_other_keys_alone(qt) -> None:
    """The refresh button's filter answers only the arrows
    (#962's bridge): every other key on the button reaches Qt's
    own handling."""
    tab, window, _bridge = make_tab(qt)
    press = qt.QtCore.QEvent.Type.KeyPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier
    other = qt.QtGui.QKeyEvent(press, qt.QtCore.Qt.Key.Key_Return, mods)
    assert tab.eventFilter(tab.refresh_button, other) is False
    window.close()


def test_a_press_on_a_row_opens_the_menu(qt) -> None:
    """A context-menu press on a catalog row (#964): the menu
    opens over the row, its three actions on it."""
    tab, window, _bridge = make_tab(qt)
    tab.menu_requested(qt.QtCore.QPoint(10, 5))
    menus = [
        widget
        for widget in qt.app.topLevelWidgets()
        if isinstance(widget, qt.QtWidgets.QMenu)
    ]
    assert len(menus) == 1
    assert menus[0].isVisible()
    assert [action.text() for action in menus[0].actions()] == [
        "Open the detail",
        "Set as the default",
        "Remove…",
    ]
    menus[0].close()
    window.close()


def widths_total(table) -> int:
    """A group table's columns' total width (#964's fakes
    read)."""
    return sum(
        table.columnWidth(column) for column in range(table.columnCount())
    )


def test_the_catalog_columns_consume_the_pane(qt) -> None:
    """The catalog group's columns (#964): every column at least
    its content and the spare shared across them, so the group
    consumes the pane's whole horizontal space — no horizontal
    scrollbar, whatever the pane's width."""
    tab, window, _bridge = make_tab(qt)
    table = tab.catalog_table
    for width in (1600, 900, 700):
        window.resize(width, 700)
        qt.app.processEvents()
        total = widths_total(table)
        assert total >= table.viewport().width() - 4  # the pane filled
        assert total <= table.viewport().width()
        assert table.horizontalScrollBar().maximum() == 0
    window.close()


def test_the_published_pane_never_scrolls_horizontally(qt) -> None:
    """The published group's columns (#964): the identity
    columns keep their whole values and the description absorbs
    the spare — a wide pane shows it whole — while a narrow pane
    ellipsizes the description first, the hash whole beside it,
    and the horizontal scrollbar never appears: every row's
    cells read within the pane whatever its width."""
    tab, window, _bridge = make_tab(qt)
    table = tab.published_table
    row = row_of(tab, "debian:13.6")
    desc = table.item(row, cells.DESC_COLUMN)
    whole = next(
        model.description
        for model in tab.published_models
        if model.ref == "debian:13.6"
    )
    window.resize(3200, 1200)
    qt.app.processEvents()
    assert desc.text() == whole  # a wide pane: the whole line
    window.resize(1200, 700)
    qt.app.processEvents()
    assert table.horizontalScrollBar().maximum() == 0
    assert widths_total(table) <= table.viewport().width()
    assert (
        table.item(row_of(tab, "debian:13.6"), cells.HASH_COLUMN).text()
        == "a" * 12
    )  # the hash held its whole prefix
    window.resize(420, 700)
    qt.app.processEvents()
    assert table.horizontalScrollBar().maximum() == 0
    # The description gave ground first — the elide itself is
    # the deficit's evidence, whatever the host's fonts:
    assert desc.text() != whole and "…" in desc.text()
    window.close()


def test_the_hash_ellipsizes_only_after_the_description_floor(
    qt,
) -> None:
    """The published pane's last resort (#964): with the
    description already at its floor, the hash gives ground —
    its ellipsis in the middle of what the pane left it — and
    still no horizontal scrollbar. The fakes keep the identity
    cells small, so the floors alone decide the pane."""
    narrow = [
        {
            "ref": "a:1",
            "url": "https://ch.example/a.tar",
            "hash": "a" * 64,
            "description": "x" * 400,
        }
    ]
    bridge = StubBridge()
    tab = images_mod.ImagesTab(bridge, CHANNEL)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.show()
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer([], narrow))
    table = tab.published_table
    window.resize(360, 700)
    qt.app.processEvents()
    assert table.horizontalScrollBar().maximum() == 0
    desc_cell = table.item(0, cells.DESC_COLUMN).text()
    assert "…" in desc_cell and not desc_cell.startswith("x" * 10)
    # The description paid the budget: its column sits under a
    # tenth of its natural width (400 characters), wherever the
    # host's fonts leave the rest — above its floor or at it.
    natural = table.fontMetrics().horizontalAdvance("x" * 400)
    assert table.columnWidth(cells.DESC_COLUMN) < natural // 10
    window.close()


def test_the_headers_read_as_one_tables_chrome(qt) -> None:
    """Both panes' header text (#964): the same bold weight on
    the catalog and the published headers — one definition,
    whatever the platform style would draw on its own."""
    tab, window, _bridge = make_tab(qt)
    for table in (tab.catalog_table, tab.published_table):
        assert table.horizontalHeader().font().bold()
    window.close()


def test_a_half_built_wrapper_sizes_nothing(qt) -> None:
    """The re-flow's guard (#959's hardening): a wrapper without
    its tables — Qt can deliver the resize to a widget whose
    construction never finished — sizes nothing and crashes no
    one."""
    tab, _window, _bridge = make_tab(qt)
    del tab.published_table
    sizes = qt.QtCore.QSize(1, 1)
    tab.resizeEvent(qt.QtGui.QResizeEvent(sizes, sizes))
    assert not tab.pending


def test_one_import_flight_at_a_time(qt) -> None:
    """The import's own gate (#964, the review's orphaned
    dialog): while the progress dialog stands over a flight, a
    second import starts nothing — no second dialog, no second
    POST — and the first landing closes the one that stands.
    The dialog itself is modal: the window behind it answers no
    activation while the wait holds."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.start_import(tab.published_models[1])
    first = tab.progress
    assert first is not None
    assert first.windowModality() == qt.QtCore.Qt.WindowModality.WindowModal
    tab.start_import(tab.published_models[0])
    assert tab.progress is first
    assert bridge.calls == [
        (
            images_mod.IMPORT_KEY,
            "import_image",
            ("https://ch.example/alpine.tar.zst",),
        )
    ]
    tab.on_succeeded(
        images_mod.IMPORT_KEY, {"ref": "alpine:3.21", "hash": "c" * 64}
    )
    assert tab.progress is None and not first.isVisible()
    window.close()


def test_a_stale_page_answer_keeps_the_fresher_catalog(qt) -> None:
    """The catalog's epoch (#964, the review's stale revert): a
    page flight that predates a catalog refresh an action landed
    cannot land its older catalog half over the fresher rows —
    the channel's half still takes, the catalog keeps what the
    action showed."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.refresh()  # the page flight goes out, its epoch read
    assert tab.pending
    # The import lands under it: its catalog refresh bumps the
    # epoch and goes out.
    tab.start_import(tab.published_models[1])
    tab.on_succeeded(
        images_mod.IMPORT_KEY, {"ref": "alpine:3.21", "hash": "c" * 64}
    )
    assert bridge.calls[-1] == (images_mod.CATALOG_KEY, "images", ())
    landed = [dict(catalog_raw()[1], default=True)]
    tab.on_succeeded(images_mod.CATALOG_KEY, landed)  # the fresh catalog
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer())  # the stale page
    assert not tab.pending
    # The fresher catalog stands: Ubuntu, now the default.
    assert rows_texts(tab.catalog_table) == [
        ["ubuntu:24.04", "default", "b" * 12, "2026-01-03 03:04"]
    ]
    # The channel's half took: the published rows still show.
    assert tab.published_table.rowCount() == 2
    assert tab.counts.text() == "1 in the catalog · 2 published"
    window.close()


def test_the_hold_follows_the_build_not_the_ref(qt) -> None:
    """The catalog's carry key (#964, the review's twin builds):
    a rebuilt archive shares the ref under another hash, and the
    hold follows the build the operator held — its hash cell
    after the rebuild, whatever slot the reading order moved it
    to."""
    twins = [
        dict(catalog_raw()[0], hash="a" * 64, imported="2026-01-02T03:04:05"),
        dict(catalog_raw()[0], hash="b" * 64, imported="2026-01-01T03:04:05"),
    ]
    bridge = StubBridge()
    tab = images_mod.ImagesTab(bridge, CHANNEL)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.resize(3200, 1200)
    window.show()
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer(twins))
    tab.catalog_table.selectRow(1)  # the older build of the ref
    held = tab.catalog_table.item(1, 2).text()
    assert held == "b" * 12
    # The rebuild flips the import stamps: the held build now
    # leads the block — the hold follows it to the top slot.
    flipped = [dict(twins[1], imported="2026-01-03T03:04:05"), twins[0]]
    tab.refresh_catalog()
    tab.on_succeeded(images_mod.CATALOG_KEY, flipped)
    assert tab.catalog_table.currentRow() == 0
    assert tab.catalog_table.item(0, 2).text() == held
    window.close()


def test_the_first_rows_hold_the_first_row(qt) -> None:
    """The first fill's own hold (#964, the listing's rule): a
    group that first shows rows holds its first row — the
    keyboard and the eye start somewhere."""
    tab, window, _bridge = make_tab(qt)
    assert tab.catalog_table.currentRow() == 0
    assert tab.published_table.currentRow() == 0
    assert tab.catalog_table.item(0, 0).text() == "debian:13.6"
    window.close()


def test_the_columns_sort_with_the_arrow(qt) -> None:
    """Both panes' columns sort (#964): the REF column holds the
    standing ascending sort with its arrow, a header click sorts
    by the column — the rank cells by their carried ranks, the
    dates by their moments and the sizes by their whole MiB, a
    text order the labels disagree with — and the arrow moves
    with the click, across a refresh too."""
    tab, window, _bridge = make_tab(qt)
    order = qt.QtCore.Qt.SortOrder
    for table in (tab.catalog_table, tab.published_table):
        header = table.horizontalHeader()
        assert header.isSortIndicatorShown()
        assert header.sortIndicatorSection() == 0
        assert header.sortIndicatorOrder() == order.AscendingOrder

    def click(table, column: int) -> None:
        header = table.horizontalHeader()
        center = qt.QtCore.QPoint(
            header.sectionViewportPosition(column)
            + header.sectionSize(column) // 2,
            header.height() // 2,
        )
        qt.QtTest.QTest.mouseClick(
            header.viewport(),
            qt.QtCore.Qt.MouseButton.LeftButton,
            pos=center,
        )

    published = tab.published_table
    click(published, cells.SIZE_COLUMN)
    sizes = [
        published.item(row, cells.SIZE_COLUMN).text()
        for row in range(published.rowCount())
    ]
    assert sizes == ["50 MiB", "100 MiB"]  # whole MiB, not text
    assert (
        published.horizontalHeader().sortIndicatorSection()
        == cells.SIZE_COLUMN
    )
    # A refresh keeps the operator's sort: the fill re-applies
    # the indicator the click left.
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer())
    assert (
        published.horizontalHeader().sortIndicatorSection()
        == cells.SIZE_COLUMN
    )
    assert [
        published.item(row, cells.SIZE_COLUMN).text()
        for row in range(published.rowCount())
    ] == ["50 MiB", "100 MiB"]

    catalog = tab.catalog_table
    click(catalog, cells.IMPORTED_COLUMN)
    imported = [
        catalog.item(row, cells.IMPORTED_COLUMN).text()
        for row in range(catalog.rowCount())
    ]
    assert imported == ["2026-01-02 03:04", "2026-01-03 03:04"]
    click(catalog, cells.IMPORTED_COLUMN)  # the toggle
    assert [
        catalog.item(row, cells.IMPORTED_COLUMN).text()
        for row in range(catalog.rowCount())
    ] == ["2026-01-03 03:04", "2026-01-02 03:04"]
    window.close()


def test_a_missing_cell_sizes_past(qt) -> None:
    """The sizing pass over a half-filled row (#964): a cell the
    table does not hold restores nothing and fits nothing — the
    pass sizes past it and crashes no one."""
    tab, window, _bridge = make_tab(qt)
    tab.published_table.takeItem(0, cells.SIZE_COLUMN)
    tab.size_columns()
    assert tab.published_table.item(0, cells.SIZE_COLUMN) is None
    window.close()


def test_a_dash_import_date_sorts_as_the_oldest(qt) -> None:
    """The import-date rank's floor (#964): a row without a
    usable stamp — the honest dash — carries the oldest rank
    and sinks on the column's newest-first read."""
    stamped = dict(catalog_raw()[0], imported=None)
    bridge = StubBridge()
    tab = images_mod.ImagesTab(bridge, CHANNEL)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.resize(3200, 1200)
    window.show()
    tab.on_succeeded(
        images_mod.PAGE_KEY, page_answer([stamped, catalog_raw()[1]], [])
    )
    catalog = tab.catalog_table
    catalog.sortItems(
        cells.IMPORTED_COLUMN,
        qt.QtCore.Qt.SortOrder.DescendingOrder,
    )
    assert catalog.item(0, cells.IMPORTED_COLUMN).text() == "2026-01-03 03:04"
    assert catalog.item(1, cells.IMPORTED_COLUMN).text() == "-"
    window.close()


def test_a_press_on_the_blank_header_stretch_sorts_nothing(qt) -> None:
    """A header press on no section (#964): the blank stretch
    past the last column reaches Qt's own handling — the tab
    sorts nothing and the standing indicator stands."""
    tab, window, _bridge = make_tab(qt)
    header = tab.catalog_table.horizontalHeader()
    press = qt.QtCore.QEvent.Type.MouseButtonPress
    mods = qt.QtCore.Qt.KeyboardModifier.NoModifier
    blank = qt.QtGui.QMouseEvent(
        press,
        qt.QtCore.QPointF(9000, 4),
        qt.QtCore.QPointF(9000, 4),
        qt.QtCore.Qt.MouseButton.LeftButton,
        qt.QtCore.Qt.MouseButton.LeftButton,
        mods,
    )
    assert tab.eventFilter(header.viewport(), blank) is False
    assert header.sortIndicatorSection() == 0
    window.close()


def test_the_third_click_returns_to_ascending(qt) -> None:
    """The sort's full cycle (#964): a new column ascending, the
    same column toggled — and the third click back to
    ascending, the arrow reading the order the rows hold
    throughout."""
    tab, window, _bridge = make_tab(qt)
    catalog = tab.catalog_table
    header = catalog.horizontalHeader()
    order = qt.QtCore.Qt.SortOrder

    def click(column: int) -> None:
        center = qt.QtCore.QPoint(
            header.sectionViewportPosition(column)
            + header.sectionSize(column) // 2,
            header.height() // 2,
        )
        qt.QtTest.QTest.mouseClick(
            header.viewport(),
            qt.QtCore.Qt.MouseButton.LeftButton,
            pos=center,
        )

    click(cells.IMPORTED_COLUMN)
    assert header.sortIndicatorOrder() == order.AscendingOrder
    click(cells.IMPORTED_COLUMN)
    assert header.sortIndicatorOrder() == order.DescendingOrder
    click(cells.IMPORTED_COLUMN)
    assert header.sortIndicatorOrder() == order.AscendingOrder
    assert [
        catalog.item(row, cells.IMPORTED_COLUMN).text()
        for row in range(catalog.rowCount())
    ] == ["2026-01-02 03:04", "2026-01-03 03:04"]
    window.close()


def test_a_row_without_its_first_cell_sizes_past(qt) -> None:
    """The sizing over a row whose first cell left (#964): the
    row's model reads None and both text passes skip it — no
    crash, the rest of the pane sizes whole."""
    tab, window, _bridge = make_tab(qt)
    tab.published_table.takeItem(0, 0)
    tab.size_columns()
    assert tab.published_table.item(0, 1) is not None
    window.close()


def test_the_package_presents_its_parts_lazily() -> None:
    """The package's lazy presentation (#964, the terminal
    package's own pattern): importing ``gui.images`` alone
    loads no Qt, and a name the package does not carry answers
    its own AttributeError."""
    import sys

    for name in list(sys.modules):
        if name.startswith("msks.client.gui.images"):
            del sys.modules[name]
    import msks.client.gui.images as package

    assert not any(
        name.startswith("msks.client.gui.images.") for name in sys.modules
    )
    assert package.ImagesTab is not None  # the lazy load happens
    import pytest

    with pytest.raises(AttributeError):
        package.no_such_part


def test_a_stale_catalog_answer_keeps_the_fresher_page(qt) -> None:
    """The reader sequence's other half (#964, the review's
    mirror): a catalog flight an action landed, then the
    operator's own refresh — the page answers first, the older
    catalog answer lands second and wipes nothing: the fresher
    page's rows stand."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    tab.refresh_catalog()  # the action's flight, out first
    tab.refresh()  # the operator's, out after
    assert tab.pending and tab.catalog_pending
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer())  # fresher
    tab.on_succeeded(
        images_mod.CATALOG_KEY, [catalog_raw()[1]]
    )  # the older read
    assert rows_texts(tab.catalog_table) == [
        ["debian:13.6", "default", "a" * 12, "2026-01-02 03:04"],
        ["ubuntu:24.04", "-", "b" * 12, "2026-01-03 03:04"],
    ]
    assert not tab.catalog_pending
    window.close()


def test_the_panes_reflow_when_the_vertical_bar_appears(qt) -> None:
    """The pane's own resize re-flow (#964, the review's
    scrollbar): rows enough to scroll shrink the viewport after
    the fill sized against the wider one, and the columns
    re-flow — no horizontal scrollbar on either pane, whatever
    the row count."""
    many = [
        {
            "name": "distro",
            "version": f"{index}",
            "hash": f"{index:02x}" * 32,
            "default": index == 0,
            "imported": f"2026-01-0{index % 9 + 1}T03:04:05",
        }
        for index in range(12)
    ]
    channel_rows = [
        {
            "ref": f"distro:{index}",
            "url": f"https://ch.example/{index}.tar",
            "hash": f"{index:02x}" * 32,
            "description": f"build {index} of the channel",
            "size": 104_857_600 + index,
            "built_at": "2026-01-01T00:00:00Z",
        }
        for index in range(12)
    ]
    tab, window, _bridge = make_tab(qt)
    window.resize(1400, 760)
    qt.app.processEvents()
    tab.on_succeeded(images_mod.PAGE_KEY, page_answer(many, channel_rows))
    qt.app.processEvents()
    for table in (tab.catalog_table, tab.published_table):
        assert table.verticalScrollBar().isVisible()
        assert table.horizontalScrollBar().maximum() == 0
    window.close()


def channel_rows() -> list[dict]:
    """The channel as the operator's own screenshot reads it
    (#964's fakes): a long description, a long ref, sizes and
    markers beside them."""
    return [
        {
            "ref": "nixos:26.05pre-git-lasqn0h9",
            "url": "https://ch.example/nixos.tar",
            "hash": "f" * 64,
            "description": (
                "NixOS 26.05 cloud image with the full module set "
                "and the consent stack baked in"
            ),
            "size": 63_500_000,
            "built_at": "2026-11-14T00:00:00Z",
        },
        {
            "ref": "debian:13.6",
            "url": "https://ch.example/debian.tar",
            "hash": "a" * 64,
            "description": "Debian 13.6 cloud image, kernel, consent stack",
            "size": 33_500_000,
            "built_at": "2026-11-14T00:00:00Z",
        },
    ]


def make_channel_tab(qt, width: int) -> tuple:
    """The tab over the screenshot-shaped channel, at the width
    the pane reads (#964's fakes)."""
    bridge = StubBridge()
    tab = images_mod.ImagesTab(bridge, CHANNEL)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.resize(width, 700)
    window.show()
    tab.on_succeeded(
        images_mod.PAGE_KEY, page_answer(catalog_raw(), channel_rows())
    )
    qt.app.processEvents()
    return tab, window


def test_the_budget_comes_from_the_description(qt) -> None:
    """The tight pane's character budget (#964, the operator's
    screenshot): with a description long enough to pay, the
    size and the marker read whole — every identity cell whole —
    and the description's middle ellipsis carries what the pane
    could not fit."""
    tab, window = make_channel_tab(qt, 560)
    table = tab.published_table
    assert table.horizontalScrollBar().maximum() == 0
    mib = 1024 * 1024
    assert [
        table.item(row, cells.SIZE_COLUMN).text()
        for row in range(table.rowCount())
    ] == [
        # the standing REF sort: debian leads nixos
        f"{33_500_000 / mib:.0f} MiB",
        f"{63_500_000 / mib:.0f} MiB",
    ]
    markers = [
        table.item(row, cells.MARKER_COLUMN).text()
        for row in range(table.rowCount())
    ]
    assert markers == [IMPORTED_MARKER, ""]
    nixos = next(
        model
        for model in tab.published_models
        if model.ref.startswith("nixos")
    )
    desc = table.item(row_of(tab, nixos.ref), cells.DESC_COLUMN)
    assert desc.text() != nixos.description and "…" in desc.text()
    window.close()


def test_the_size_and_marker_give_ground_last(qt) -> None:
    """The giving order's tail (#964): the ref and the build
    date spend their ground before the size and the marker give
    any — a pane deep enough in deficit never shows the size or
    the marker narrower than the columns ahead of them in the
    order."""
    tab, window = make_channel_tab(qt, 240)
    table = tab.published_table
    minimum = table.horizontalHeader().minimumSectionSize()
    # Deep enough in deficit that every stage spent: the ref and
    # the build date sit at the header's minimum, and the size
    # and the marker gave only what the ref and the build date
    # could not — never below the minimum themselves.
    assert table.columnWidth(cells.REF_COLUMN) <= minimum + 1
    assert table.columnWidth(cells.BUILT_COLUMN) <= minimum + 1
    assert table.columnWidth(cells.SIZE_COLUMN) >= minimum
    assert table.columnWidth(cells.MARKER_COLUMN) >= minimum
    window.close()


def test_a_spare_pane_spreads_the_room(qt) -> None:
    """The spare pane's sharing (#964, the operator's ask): the
    description keeps its whole value — no empty tail parking
    the pane's room — and the spare spreads across the size,
    the hash, the build date, and the marker, the group filling
    the pane's whole width."""
    tab, window = make_channel_tab(qt, 2400)
    qt.app.processEvents()
    table = tab.published_table
    assert widths_total(table) >= table.viewport().width() - 4
    assert table.horizontalScrollBar().maximum() == 0
    # The description stands at its natural width, not wider,
    # and the room went to the four fact columns instead —
    # measured against a fresh re-measure of the naturals.
    sized = [table.columnWidth(c) for c in range(table.columnCount())]
    table.setSortingEnabled(False)
    layout_mod.restore_whole_texts(table, tab.published_models)
    table.resizeColumnsToContents()
    naturals = [table.columnWidth(c) for c in range(table.columnCount())]
    table.setSortingEnabled(True)
    assert sized[cells.DESC_COLUMN] == naturals[cells.DESC_COLUMN]
    # The room went to the four fact columns instead.
    grew = [
        sized[c] - naturals[c]
        for c in (
            cells.SIZE_COLUMN,
            cells.HASH_COLUMN,
            cells.BUILT_COLUMN,
            cells.MARKER_COLUMN,
        )
    ]
    assert all(extra > 0 for extra in grew)
    window.close()


def test_the_refresh_button_leads_the_bar(qt) -> None:
    """The refresh action's place (#964, the operator's ask;
    the bar at the page's bottom since #1011, the Mounts and
    Proxies pattern): the button on the bar's left, the flash
    line taking the rest beside it, and the bar below the two
    groups it reads."""
    tab, window, _bridge = make_tab(qt)
    layout = tab.layout()
    assert layout.itemAt(0).widget().title() == cells.CATALOG_GROUP
    assert layout.itemAt(1).widget().title() == cells.PUBLISHED_GROUP
    bar = layout.itemAt(2)  # the page's bottom bar
    assert bar.itemAt(0).widget() is tab.refresh_button
    assert bar.itemAt(1).widget() is tab.status
    assert layout.itemAt(3).widget() is tab.counts
    window.close()


def test_a_right_click_on_a_published_row_offers_the_import(
    qt,
) -> None:
    """A right-click on a published row (#964, the operator's
    ask): the import action on it, driving the same gated
    flight an activation starts. A press on no row opens
    nothing."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    menu = tab.published_menu(row_of(tab, "alpine:3.21"))
    assert [action.text() for action in menu.actions()] == ["Import…"]
    menu.actions()[0].trigger()
    assert bridge.calls == [
        (
            images_mod.IMPORT_KEY,
            "import_image",
            ("https://ch.example/alpine.tar.zst",),
        )
    ]
    assert tab.published_menu(99) is None
    window.close()


def test_the_imported_builds_menu_offers_the_skip(qt) -> None:
    """The already-imported build's right-click (#964): the
    menu still offers the import, and taking it flashes the
    skip the activation path names — the exact build is in the
    catalog."""
    tab, window, bridge = make_tab(qt)
    bridge.calls.clear()
    menu = tab.published_menu(row_of(tab, "debian:13.6"))
    menu.actions()[0].trigger()
    assert bridge.calls == []
    assert tab.status.text() == "debian:13.6 is already in the catalog"
    window.close()


def test_a_right_click_opens_the_published_menu(qt) -> None:
    """A right-click on a published row opens its menu (#964):
    the import action on it, over the row pressed."""
    tab, window, _bridge = make_tab(qt)
    row = row_of(tab, "alpine:3.21")
    tab.published_menu_requested(qt.QtCore.QPoint(10, row * 40 + 5))
    menus = [
        widget
        for widget in qt.app.topLevelWidgets()
        if isinstance(widget, qt.QtWidgets.QMenu)
    ]
    assert len(menus) == 1
    assert menus[0].isVisible()
    assert [action.text() for action in menus[0].actions()] == ["Import…"]
    menus[0].close()
    window.close()
