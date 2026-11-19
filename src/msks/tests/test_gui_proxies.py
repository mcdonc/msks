"""The workspace Proxies tab (#988): the presenter's plain
arithmetic — the spec grammar (parity with the TUI's own), the
parse through the CLI's parsers, the seeds — and the tab over a
stub bridge: the list from row fakes, add and edit through the
bridge, a refused spec, the removal's confirmation, and a pushed
proxy transition refreshing the open tab. The widgets run under
the offscreen platform on every host.
"""

import importlib.util
import os
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui import proxyrows  # noqa: E402
from msks.client.gui.proxyrows import (  # noqa: E402
    FORWARD_CHOICE,
    REMOTE_CHOICE,
    form_seeds,
    parsed_body,
    proxy_transition,
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
    every flight a real future."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object):
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
    from msks.client.gui import proxies as proxies_mod
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(
        app=app, QtWidgets=QtWidgets, keep=[], proxies=proxies_mod
    )
    yield ns
    del ns.keep[:]
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    from PySide6 import QtCore

    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def proxy_rows() -> list[dict]:
    """Two proxy rows, one of each direction, the forward's bind
    named and failed with its cause."""
    return [
        {
            "id": 7,
            "direction": "forward",
            "listen_host": "127.0.0.1",
            "listen_port": 8080,
            "target_host": "127.0.0.1",
            "target_port": 80,
            "label": "web",
            "status": "listening",
        },
        {
            "id": 9,
            "direction": "remote",
            "listen_host": "127.0.0.1",
            "listen_port": 2222,
            "target_host": "127.0.0.1",
            "target_port": 22,
            "label": None,
            "status": "failed",
            "reason": "bind refused",
        },
    ]


def make_tab(qt) -> tuple:
    """The tab over the stub bridge, hosted and listed, shown in
    a real window — the screens' own shape."""
    bridge = StubBridge()
    tab = qt.proxies.ProxiesTab(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.show()
    qt.keep.append(window)
    tab.host("ws-a")
    tab.poke()
    bridge.succeeded.emit(tab.list_key, proxy_rows())
    bridge.calls.clear()
    return tab, bridge


def fake_dialog(monkeypatch, qt, body: dict | None) -> None:
    """The form's stub (#988): the body handed in is the form's
    accepted answer, its exec returning at once."""

    class FakeDialog:
        def __init__(self, row: dict | None = None) -> None:
            self.body = body

        def exec(self) -> int:
            return 1 if body is not None else 0

    monkeypatch.setattr(qt.proxies, "ProxyDialog", FakeDialog)


def test_the_spec_grammar_matches_the_tui() -> None:
    """The spec text (#988): the TUI's own grammar over the same
    rows — the unnamed loopbacks left out, a named bind in, an
    IPv6 literal bracketed — held by parity test, the listing
    presenter's own rule."""
    from msks.client.tui import proxies as tui_proxies

    rows = [
        *proxy_rows(),
        {
            "id": 11,
            "direction": "forward",
            "listen_host": "::1",
            "listen_port": 9090,
            "target_port": 90,
        },
        {"id": 12, "direction": "remote", "listen_port": 1, "target_port": 2},
    ]
    for row in rows:
        assert proxyrows.spec_text(row) == tui_proxies.spec_text(row)
    assert proxyrows.spec_text(rows[2]) == "-L [::1]:9090:localhost:90"


def test_the_cells_read_the_row() -> None:
    """The cells (#988): the direction as ssh's letter, the spec
    minus it, the label, and the status word with its cause —
    the TUI's own words."""
    forward, failed = proxy_rows()
    assert proxyrows.direction_letter(forward) == FORWARD_CHOICE
    assert proxyrows.direction_spec(forward) == "8080:localhost:80"
    assert proxyrows.status_text(forward) == "listening"
    assert proxyrows.direction_letter(failed) == REMOTE_CHOICE
    assert proxyrows.direction_spec(failed) == "2222:127.0.0.1:22"
    assert proxyrows.status_text(failed) == "failed (bind refused)"


def test_the_parse_reads_the_clis_own_words() -> None:
    """The form's parse (#988): the CLI's own parsers for the
    picked direction — whole bodies landing, a blank spec saying
    itself, a shape refusal in the command's words, the forward
    host field held to the loopback spellings the daemon
    accepts."""
    body, error = parsed_body(FORWARD_CHOICE, "8080:localhost:80")
    assert error == ""
    assert body is not None and body["listen_port"] == 8080
    assert parsed_body(FORWARD_CHOICE, "") == (
        None,
        "the spec is the form's one required field",
    )
    assert parsed_body(FORWARD_CHOICE, "ssh")[0] is None
    assert "refused:" in parsed_body(FORWARD_CHOICE, "ssh")[1]
    assert parsed_body(FORWARD_CHOICE, "8080:example.com:80")[0] is None
    remote, _error = parsed_body(REMOTE_CHOICE, "2222:127.0.0.1:22")
    assert remote is not None and remote["direction"] == "remote"


def test_the_seeds_read_the_row() -> None:
    """The form's seeds (#988): the edited row's letter, spec,
    and label; the add path's blanks with the forward letter
    standing."""
    assert form_seeds(proxy_rows()[1]) == {
        "letter": REMOTE_CHOICE,
        "spec": "2222:127.0.0.1:22",
        "label": "",
    }
    assert form_seeds(None) == {
        "letter": FORWARD_CHOICE,
        "spec": "",
        "label": "",
    }


def test_the_transition_check_names_the_family() -> None:
    """The frame check (#988): every proxy transition refreshes,
    anything else is not this tab's state."""
    assert proxy_transition({"event": "proxy.listening"})
    assert proxy_transition({"event": "proxy.removed"})
    assert not proxy_transition({"event": "workspace.status"})
    assert not proxy_transition({})


def test_the_dialog_builds_seeded_and_submits(qt) -> None:
    """The form itself (#988): blank for the add with the
    forward letter standing, seeded for the edit — a whole
    submit accepts carrying the body with its label, a refused
    spec names itself on the note line and the form stands."""
    blank = qt.proxies.ProxyDialog()
    qt.keep.append(blank)
    assert blank.windowTitle() == "Add a proxy"
    assert blank.letter.currentText() == FORWARD_CHOICE
    assert blank.spec.text() == ""
    seeded = qt.proxies.ProxyDialog(proxy_rows()[1])
    qt.keep.append(seeded)
    assert seeded.windowTitle() == "Edit a proxy"
    assert seeded.letter.currentText() == REMOTE_CHOICE
    assert seeded.spec.text() == "2222:127.0.0.1:22"
    assert seeded.label.text() == ""
    seeded.submit()
    assert seeded.body == {
        "direction": "remote",
        "listen_port": 2222,
        "target_host": "127.0.0.1",
        "target_port": 22,
    }
    labeled = qt.proxies.ProxyDialog(proxy_rows()[0])
    qt.keep.append(labeled)
    labeled.spec.setText("8081:localhost:81")
    labeled.submit()
    assert labeled.body is not None and labeled.body["label"] == "web"
    refused = qt.proxies.ProxyDialog()
    qt.keep.append(refused)
    refused.submit()  # blank: the required field says itself
    assert refused.body is None
    assert refused.note.text() == "the spec is the form's one required field"
    refused.spec.setText("not a spec")
    refused.submit()
    assert refused.body is None
    assert "refused:" in refused.note.text()


def test_the_guards_send_nothing(qt, monkeypatch) -> None:
    monkeypatch.setattr(
        qt.QtWidgets.QMessageBox,
        "question",
        lambda *args: qt.QtWidgets.QMessageBox.StandardButton.No,
    )
    """The guards (#988): a cancelled form composes nothing, a
    tab with no selection edits and removes nothing, and a
    mutation already out gates the next."""
    tab, bridge = make_tab(qt)
    fake_dialog(monkeypatch, qt, None)  # exec answers a cancel
    tab.add()
    assert bridge.calls == []
    tab.edit()
    tab.remove()
    assert bridge.calls == []
    tab.table.selectRow(0)
    tab.edit()  # the form answered a cancel: nothing composes
    tab.remove()  # the confirmation refuses: nothing sends
    assert bridge.calls == []
    fake_dialog(
        monkeypatch,
        qt,
        {"direction": "forward", "listen_port": 1, "target_port": 2},
    )
    tab.add()
    key = bridge.calls[0][0]
    tab.add()  # a mutation already out gates the next
    assert len([c for c in bridge.calls if c[1] == "create_proxy"]) == 1
    bridge.failed.emit(key, "refused once")


def test_a_refused_listing_stands_silent(qt) -> None:
    """A refused listing (#988): the rows stand, the pending
    flag clears, and the note keeps its standing text — the
    listing is a read, its failure says itself nowhere else."""
    tab, bridge = make_tab(qt)
    tab.poke()
    key = bridge.calls[0][0]
    bridge.failed.emit(key, "the daemon is gone")
    assert tab.table.rowCount() == 2  # the rows stand
    bridge.failed.emit("detail-health", "not this tab's")
    bridge.succeeded.emit("workspaces-list", [])
    assert tab.table.rowCount() == 2


def test_a_switch_mid_flight_cancels(qt) -> None:
    """The switch mid-flight (#988): the flights the old
    workspace had out cancel — their late answers reach nothing,
    the stale mutation's included."""
    tab, bridge = make_tab(qt)
    from concurrent.futures import Future

    class ChangeBridge(StubBridge):
        def __init__(self) -> None:
            super().__init__()
            self.change_future = Future()

        def call(self, key, method, *args):
            self.calls.append((key, method, args))
            if method != "workspace_proxies":
                return self.change_future
            return Future()

    change_bridge = ChangeBridge()
    change_bridge.succeeded = bridge.succeeded
    change_bridge.failed = bridge.failed
    tab.bridge = change_bridge
    tab.change("create_proxy", {"direction": "forward"})
    change_key = tab.change_key
    tab.host("ws-b")
    assert change_bridge.change_future.cancelled()
    # The stale answer reaches nothing: the table stands blank.
    change_bridge.succeeded.emit(change_key, {"id": 11})
    change_bridge.succeeded.emit(change_key, {"id": 11})  # twice: nothing
    assert tab.table.rowCount() == 0


def test_the_tab_lists_the_rows(qt) -> None:
    """The tab's table (#988): one row per proxy with the cells
    the presenter builds."""
    tab, _bridge = make_tab(qt)
    assert tab.table.rowCount() == 2
    cells = [
        tab.table.item(row, column).text()
        for row in range(2)
        for column in range(4)
    ]
    assert cells == [
        "-L",
        "8080:localhost:80",
        "web",
        "listening",
        "-R",
        "2222:127.0.0.1:22",
        "",
        "failed (bind refused)",
    ]


def test_an_add_posts_the_body_and_refreshes(qt, monkeypatch) -> None:
    """The Add action (#988): the accepted form's body posts one
    proxy row; the mutation's answer notes its landing and
    refreshes the listing behind it."""
    tab, bridge = make_tab(qt)
    body = {"direction": "forward", "listen_port": 9090, "target_port": 90}
    fake_dialog(monkeypatch, qt, body)
    tab.add()
    method_call = bridge.calls[0]
    assert method_call[1:] == ("create_proxy", ("ws-a", body))
    bridge.succeeded.emit(method_call[0], {"id": 11, "status": "listening"})
    assert tab.note.text() == "saved"
    assert tab.add_button.isEnabled()
    assert [call[1] for call in bridge.calls[1:]] == ["workspace_proxies"]


def test_an_edit_posts_the_whole_body(qt, monkeypatch) -> None:
    """The Edit action (#988): the form seeded with the selected
    row, an accepted body rewriting it whole through the PATCH
    route."""
    tab, bridge = make_tab(qt)
    body = {"direction": "remote", "listen_port": 2223, "target_port": 22}
    fake_dialog(monkeypatch, qt, body)
    tab.table.selectRow(1)
    tab.edit()
    assert bridge.calls[0][1:] == (
        "update_proxy",
        ("ws-a", 9, body),
    )


def test_a_refused_spec_names_itself(qt, monkeypatch) -> None:
    """A refused save (#988): the daemon's named message lands
    whole on the note line, the rows standing."""
    tab, bridge = make_tab(qt)
    body = {"direction": "forward", "listen_port": 1, "target_port": 2}
    fake_dialog(monkeypatch, qt, body)
    tab.add()
    key = bridge.calls[0][0]
    bridge.failed.emit(key, "workspace ws-a has no NIC: its tap is not up")
    assert tab.note.text() == (
        "refused: workspace ws-a has no NIC: its tap is not up"
    )
    assert tab.table.rowCount() == 2
    assert tab.add_button.isEnabled()  # the refusal re-enables


def test_remove_asks_and_removes(qt, monkeypatch) -> None:
    """The Remove action (#988): the question names the spec with
    No as the default; a confirmation removes the row through the
    DELETE route, and a refusal sends nothing."""
    asked: list[tuple] = []

    def refuse_or_allow(parent, title, text, buttons=None, default=None):
        asked.append((title, text, default))
        no = qt.QtWidgets.QMessageBox.StandardButton.No
        yes = qt.QtWidgets.QMessageBox.StandardButton.Yes
        return no if len(asked) == 1 else yes

    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "question", refuse_or_allow)
    tab, bridge = make_tab(qt)
    tab.table.selectRow(0)
    tab.remove()
    assert bridge.calls == []  # the refusal sent nothing
    assert asked[0][0] == "Remove proxy"
    assert "-L 8080:localhost:80" in asked[0][1]
    assert asked[0][2] is qt.QtWidgets.QMessageBox.StandardButton.No
    tab.remove()
    assert bridge.calls[0][1:] == ("remove_proxy", ("ws-a", 7))
    key = bridge.calls[0][0]
    bridge.succeeded.emit(key, {"id": 7})
    assert tab.note.text() == "removed"


def test_a_pushed_transition_refreshes_the_open_tab(qt) -> None:
    """The events feed (#988): a proxy transition the page pushes
    refreshes the tab's listing without a manual action — a bind
    that fails while the tab stands paints itself."""
    tab, bridge = make_tab(qt)
    tab.frame({"event": "proxy.failed", "data": {"id": "ws-a"}})
    key = bridge.calls[0][0]
    # A frame that is not a transition pokes nothing.
    bridge.calls.clear()
    tab.frame({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert bridge.calls == []
    # The poke is gated while the first listing stands.
    tab.frame({"event": "proxy.closed"})
    assert bridge.calls == []
    bridge.succeeded.emit(key, proxy_rows())
    tab.frame({"event": "proxy.closed"})
    assert [call[1] for call in bridge.calls] == ["workspace_proxies"]
    tab.frame({"event": "proxy.closed"})
    assert len(bridge.calls) == 1


def test_a_workspace_switch_resets_the_tab(qt) -> None:
    """The open's reset (#988): a switch cancels the flights the
    old workspace had out — their keys reach nothing after —
    blanks the table, and the first listing waits for the poke."""
    tab, bridge = make_tab(qt)
    stale_list = tab.list_key
    tab.host("ws-b")
    assert tab.table.rowCount() == 0
    assert tab.add_button.isEnabled()  # an add appends, it replaces nothing
    bridge.succeeded.emit(stale_list, proxy_rows())  # the stale answer
    assert tab.table.rowCount() == 0
    tab.poke()
    assert [call[1] for call in bridge.calls] == ["workspace_proxies"]
    bridge.succeeded.emit(tab.list_key, proxy_rows())
    assert tab.table.rowCount() == 2
    assert tab.add_button.isEnabled()
