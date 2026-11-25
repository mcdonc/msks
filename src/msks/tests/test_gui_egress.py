"""The workspace Egress tab (#1042): the posture surface over
a stub bridge — the rules read painting the mode, the switch
through the policy route with its effect note, the empty-static
gate's question before the send, the route's own refusal asking
the same question again, and the pushed rules frame repainting
a switch another surface made — and the static allowlist
editor (#1050): the read painting the list, add and remove
posting the whole new list through the policy route, the
optimistic row and its restore on a refusal, the route's
reason for a rejected entry naming itself inline, and the
empty-static gate's two arms over a write that empties the
list. The audit surface (#1051) beside them: the in-effect
verdicts the frame carries with their question-gated revoke
through the revoke route, the deny-to-allowlist ask composing
through the editor's own write path, and the cadence forwarding
to the trail. The widgets run under the offscreen platform on
every host.
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

from msks.client.gui import egress as egress_mod  # noqa: E402
from msks.client.tui.consent import (  # noqa: E402
    EMPTY_STATIC_QUESTION,
    effective_allows,
    parse_rules,
)
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

#: The route's nothing-effectively-allowed refusal, verbatim
#: (#280) — the message the tab's refusal arm matches.
EMPTY_REFUSAL = (
    "static mode with nothing effectively allowed answers every "
    "name NXDOMAIN (an offline workspace); pass allow_list "
    "entries, or set confirm_empty to switch anyway"
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
    every flight a real future — the tab holds it for the cancel
    a workspace switch takes."""

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
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(app=app, QtWidgets=QtWidgets, keep=[])
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


def rules_frame(
    mode: str = "interactive",
    allow_list: tuple = (".debian.org",),
    allowed: tuple = (
        {
            "id": "req-1",
            "dest_host": "example.com",
            "dest_port": 443,
            "decision": "allowed",
            "duration": "forever",
            "decided_at": 1.0,
            "decided_by": "token",
        },
    ),
) -> dict:
    """One workspace's rules frame as the daemon's GET serves
    it: the mode, the static allowlist, and the in-effect
    verdicts beside it."""
    return {
        "workspace_id": "ws-a",
        "mode": mode,
        "allow_list": list(allow_list),
        "allowed": list(allowed),
        "denied": [],
    }


def make_tab(qt) -> tuple:
    """The tab over the stub bridge, hosted on the workspace,
    shown in a real window — the screens' own shape — with its
    first read landed."""
    bridge = StubBridge()
    tab = egress_mod.EgressTab(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.show()
    qt.keep.append(window)
    tab.host("ws-a")
    tab.poke()
    bridge.succeeded.emit(tab.list_key, rules_frame())
    bridge.calls.clear()
    return tab, bridge


def fake_confirm(monkeypatch, answer: bool) -> list:
    """The question's stub: one ask recorded — the parent, the
    title, and the text — and the given answer returned."""
    asks: list[tuple] = []

    def ask(parent, title: str, text: str) -> bool:
        asks.append((parent, title, text))
        return answer

    monkeypatch.setattr(egress_mod, "confirm", ask)
    return asks


def pick_mode(tab, mode: str) -> None:
    """A programmatic index change — the read's own follow shape:
    the words follow the pick, and no switch fires."""
    tab.pick.setCurrentIndex(egress_mod.EGRESS_MODES.index(mode))


def choose(tab, mode: str) -> None:
    """The operator's pick on the switcher (#1049 review): the
    index moves and the combo's own ``activated`` signal fires —
    the pick that commits at once."""
    index = egress_mod.EGRESS_MODES.index(mode)
    tab.pick.setCurrentIndex(index)
    tab.pick.activated.emit(index)


def entry_rows(tab) -> list[str]:
    """The editor's list as a plain list of strings — the rows
    the operator reads."""
    return [tab.entries.item(row).text() for row in range(tab.entries.count())]


def fake_entry_dialog(monkeypatch, entry: str) -> list:
    """The Add form's stub (#1050): the entry handed in is the
    form's accepted answer, its exec returning at once — an
    empty entry answers a cancel — and each construction
    recorded with the parent the tab passed (#1014)."""
    built: list[object] = []

    class FakeDialog:
        def __init__(self, parent=None) -> None:
            self.entry = entry
            built.append(parent)

        def exec(self) -> int:
            return 1 if entry else 0

    monkeypatch.setattr(egress_mod, "EntryDialog", FakeDialog)
    return built


def test_effective_allows_reads_the_shared_rule() -> None:
    """The shared gate (#280): nothing effectively allowed under
    a None snapshot or an empty one, and an allowlist entry or an
    in-effect allowed verdict each count — the parse the tab's
    standing read feeds it."""
    assert not effective_allows(None)
    empty = parse_rules(rules_frame(allow_list=(), allowed=()))
    assert not effective_allows(empty)
    assert effective_allows(parse_rules(rules_frame(allow_list=(".d.org",))))
    assert effective_allows(parse_rules(rules_frame()))


def test_a_wheel_over_the_switcher_changes_nothing(qt) -> None:
    """The wheel's pass-through (#1049 review): a scroll across
    the pane never switches a live posture — the event ignores
    itself for the parent to take, the pick stands, and nothing
    posts through the bridge."""
    tab, bridge = make_tab(qt)
    event = QtGui.QWheelEvent(
        QtCore.QPointF(1.0, 1.0),
        QtCore.QPointF(1.0, 1.0),
        QtCore.QPoint(0, 0),
        QtCore.QPoint(0, 120),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    tab.pick.wheelEvent(event)
    assert not event.isAccepted()
    assert tab.pick.currentText() == "interactive"
    assert bridge.calls == []


def test_the_refusal_marker_matches_the_routes_message() -> None:
    """The marker's contract (#1042): the route's actual refusal
    — copied verbatim from the policy route's detail — carries
    the marker the tab matches on, and the server's own refusal
    test pins the same token into the same message, so the
    match cannot drift from the message it matches."""
    assert egress_mod.CONFIRM_MARKER in EMPTY_REFUSAL


def test_the_read_paints_the_mode(qt) -> None:
    """The tab's read (#1042): the landed frame names the current
    mode, the switcher follows it, its meaning stands beside it —
    and the follow decides nothing: a programmatic index change
    sends no switch, and the operator's pick on the standing
    mode sends none either."""
    tab, bridge = make_tab(qt)
    assert tab.mode_line.text() == "current mode: interactive"
    assert tab.pick.currentText() == "interactive"
    assert tab.words.text() == egress_mod.MODE_WORDS["interactive"]
    pick_mode(tab, "allow")  # the read's own follow shape
    assert tab.words.text() == egress_mod.MODE_WORDS["allow"]
    assert bridge.calls == []  # a programmatic change decides nothing
    choose(tab, "interactive")  # the standing mode: nothing to switch
    assert bridge.calls == []


def test_the_switch_posts_and_lands_with_its_effect(qt) -> None:
    """The pick's own action (#1042): the picked mode posts through
    the bridge at once, one flight gating the next pick, and the
    reply — the fresh rules frame with ``applied`` beside it —
    repaints the posture and names its effect with the reply's
    fact."""
    tab, bridge = make_tab(qt)
    choose(tab, "allow")
    assert bridge.calls == [
        (tab.switch_key, "set_egress_mode", ("ws-a", "allow", False))
    ]
    assert tab.note.text() == "switching …"
    choose(tab, "static")  # one flight: the pick gates
    assert len(bridge.calls) == 1
    bridge.succeeded.emit(
        tab.switch_key, {**rules_frame(mode="allow"), "applied": True}
    )
    assert tab.mode_line.text() == "current mode: allow"
    assert tab.pick.currentText() == "allow"
    assert tab.note.text() == "mode allow — in effect now"
    choose(tab, "static")
    bridge.succeeded.emit(
        tab.switch_key, {**rules_frame(mode="static"), "applied": False}
    )
    assert tab.note.text() == "mode static — takes effect at next start"


def test_a_reply_without_a_mode_names_the_sent_one(qt) -> None:
    """The landing note's stand-in (#1042): a reply that carries
    no mode — no route shape, only the tab's own tolerance —
    names the mode the operator picked."""
    tab, bridge = make_tab(qt)
    choose(tab, "allow")
    bridge.succeeded.emit(tab.switch_key, {"applied": True})
    assert tab.note.text() == "mode allow — in effect now"


def test_the_empty_static_pick_asks_before_it_sends(qt, monkeypatch) -> None:
    """The offline gate before the send (#280, #1042): a static
    pick over a read with nothing effectively allowed asks the
    shared question first — a No sends nothing, a Yes sends the
    confirmed switch through the policy route."""
    frame = rules_frame(mode="allow", allow_list=(), allowed=())
    tab, bridge = make_tab(qt)
    bridge.succeeded.emit(tab.list_key, frame)
    asks = fake_confirm(monkeypatch, False)
    choose(tab, "static")
    assert asks == [(tab, "Switch to static", EMPTY_STATIC_QUESTION)]
    assert bridge.calls == []  # the No decided nothing
    asks = fake_confirm(monkeypatch, True)
    choose(tab, "static")  # the pick still holds static
    assert bridge.calls == [
        (tab.switch_key, "set_egress_mode", ("ws-a", "static", True))
    ]


def test_the_routes_refusal_asks_and_a_yes_resends_confirmed(
    qt, monkeypatch
) -> None:
    """The route's own refusal (#1042): a switch that passed the
    local gate — the read held an allowed verdict — can still
    meet the daemon's nothing-effectively-allowed refusal when
    the verdict left between the read and the send. The refusal
    surfaces as the shared question: a Yes resends confirmed, a
    No keeps the standing mode, and neither reads as a raw
    error."""
    tab, bridge = make_tab(qt)  # the read holds an allowed verdict
    choose(tab, "static")
    assert bridge.calls[-1] == (
        tab.switch_key,
        "set_egress_mode",
        ("ws-a", "static", False),
    )
    asks = fake_confirm(monkeypatch, True)
    bridge.failed.emit(tab.switch_key, EMPTY_REFUSAL)
    assert asks == [(tab, "Switch to static", EMPTY_STATIC_QUESTION)]
    assert bridge.calls[-1] == (
        tab.switch_key,
        "set_egress_mode",
        ("ws-a", "static", True),
    )
    asks = fake_confirm(monkeypatch, False)
    bridge.failed.emit(tab.switch_key, EMPTY_REFUSAL)
    assert len(asks) == 1
    assert tab.note.text() == ""  # the No decided nothing
    assert tab.pick.currentText() == "interactive"  # the pick returned


def test_a_plain_refusal_names_itself(qt) -> None:
    """A switch the daemon refuses for any other reason (#1042):
    the whole message lands on the note line, the pick returns to
    the standing mode, and the switcher stands ready."""
    tab, bridge = make_tab(qt)
    choose(tab, "allow")
    bridge.failed.emit(tab.switch_key, "egress_mode must be one of …")
    assert tab.note.text() == "refused: egress_mode must be one of …"
    assert tab.pick.currentText() == "interactive"
    choose(tab, "allow")  # the switcher stands ready
    assert len(bridge.calls) == 2


def test_a_pushed_rules_frame_repaints(qt) -> None:
    """The pushed frame (#1042): a fresh ``egress.rules`` — the
    daemon broadcasts one on every policy change — repaints the
    posture, whatever made the change; a foreign event and a
    foreign workspace's rules frame change nothing."""
    tab, _bridge = make_tab(qt)
    tab.push(
        {
            "event": "egress.rules",
            "data": rules_frame(mode="static"),
        }
    )
    assert tab.mode_line.text() == "current mode: static"
    assert tab.pick.currentText() == "static"
    tab.push({"event": "egress.request", "data": {"workspace_id": "ws-a"}})
    tab.push(
        {
            "event": "egress.rules",
            "data": {**rules_frame(mode="allow"), "workspace_id": "ws-b"},
        }
    )
    assert tab.mode_line.text() == "current mode: static"


def test_a_pushed_frame_under_a_pending_switch_keeps_the_pick(qt) -> None:
    """A pushed frame while the switch is out (#1042): the pick
    holds the sent mode — the answer repaints, and a frame the
    page raced cannot un-pick the operator's send."""
    tab, bridge = make_tab(qt)
    choose(tab, "allow")
    tab.push({"event": "egress.rules", "data": rules_frame(mode="allow")})
    assert tab.pick.currentText() == "allow"
    choose(tab, "interactive")  # the flight holds the gate
    assert len(bridge.calls) == 1
    bridge.succeeded.emit(
        tab.switch_key, {**rules_frame(mode="allow"), "applied": True}
    )
    assert tab.note.text() == "mode allow — in effect now"


def test_a_read_refusal_stands_the_posture_for_the_next_poke(
    qt,
) -> None:
    """A refused read (#1042): the posture stands — the note
    keeps its words — and the next poke flies once the refusal
    cleared the pending flag."""
    tab, bridge = make_tab(qt)
    bridge.failed.emit(tab.list_key, "the daemon is gone")
    assert tab.mode_line.text() == "current mode: interactive"
    assert tab.note.text() == ""
    tab.poke()
    assert [c for c in bridge.calls if c[1] == "egress_rules"]
    bridge.succeeded.emit(tab.list_key, rules_frame(mode="static"))
    assert tab.mode_line.text() == "current mode: static"


def test_a_malformed_answer_blanks_the_posture(qt) -> None:
    """An answer that is not a frame (#1042): the posture blanks
    — the tab's own tolerance, the daemon's shape never sending
    one — and the pick decides nothing until a real read lands."""
    tab, bridge = make_tab(qt)
    tab.poke()
    bridge.succeeded.emit(tab.list_key, "not a frame")
    assert tab.mode_line.text() == "current mode: —"
    assert tab.pick.currentText() == "allow"  # the blank reset
    choose(tab, "static")  # no posture: the pick decides nothing
    assert not [c for c in bridge.calls if c[1] == "set_egress_mode"]
    bridge.succeeded.emit(tab.list_key, rules_frame())
    choose(tab, "allow")  # a read stands: the pick switches
    assert [c[1] for c in bridge.calls if c[1] == "set_egress_mode"]


def test_a_workspace_switch_cancels_and_blanks(qt) -> None:
    """The open's reset (#1042): a workspace switch cancels the
    flights the old workspace still had out — their answers are
    not the new workspace's to take — blanks the posture and the
    note, and the new workspace's own read fills it."""
    tab, bridge = make_tab(qt)
    tab.poke()  # a read the switch cancels mid-flight
    stale_read = tab.list_key
    choose(tab, "allow")
    stale_switch = tab.switch_key
    tab.host("ws-b")
    assert tab.mode_line.text() == "current mode: —"
    assert tab.note.text() == ""
    choose(tab, "static")  # nothing landed for ws-b: no switch
    assert len(bridge.calls) == 2  # ws-a's stale read and switch only
    bridge.succeeded.emit(stale_read, rules_frame(mode="allow"))
    bridge.failed.emit(stale_switch, "refused")
    assert tab.mode_line.text() == "current mode: —"
    assert tab.note.text() == ""
    tab.poke()
    bridge.succeeded.emit(
        tab.list_key,
        {**rules_frame(mode="static"), "workspace_id": "ws-b"},
    )
    assert tab.mode_line.text() == "current mode: static"


def test_a_done_flight_and_a_rehosted_workspace(qt) -> None:
    """The reset's own arms (#1042): a flight already finished —
    its cancel a no-op — and the same workspace re-hosted change
    nothing: the rehost is the same tab, and the finished flight
    leaves no state behind."""
    from concurrent.futures import Future

    tab, bridge = make_tab(qt)
    done = Future()
    done.set_result(None)
    tab.list_flight = done  # the deliver race: answered, not landed
    tab.host("ws-b")
    assert tab.workspace_id == "ws-b"
    tab.poke()
    bridge.succeeded.emit(
        tab.list_key, {**rules_frame(), "workspace_id": "ws-b"}
    )
    tab.host("ws-b")  # the same workspace: the tab stands
    assert tab.mode_line.text() == "current mode: interactive"


def test_a_poke_before_a_host_flies_nothing(qt) -> None:
    """The unhosted tab (#1042): no workspace stands, and the
    poke and the pick both guard — the page hosts before it
    pokes, and a pick under no workspace sends nothing."""
    bridge = StubBridge()
    tab = egress_mod.EgressTab(bridge)
    qt.keep.append(tab)
    tab.poke()
    choose(tab, "static")
    assert bridge.calls == []
    tab.poke()  # the pending flag never stood: still nothing
    assert bridge.calls == []


def test_one_switch_at_a_time(qt) -> None:
    """One flight at a time (#1042): a switch already out gates
    the next pick — the listing's own rule, so a late reply
    cannot answer a second send."""
    tab, bridge = make_tab(qt)
    choose(tab, "allow")
    key = tab.switch_key
    choose(tab, "static")  # gated: nothing sent
    assert len(bridge.calls) == 1
    bridge.succeeded.emit(key, {**rules_frame(mode="allow"), "applied": True})
    choose(tab, "allow")  # the pick names the standing mode now: nothing
    assert len(bridge.calls) == 1


def test_another_keys_answer_is_not_the_tabs_to_take(qt) -> None:
    """The bridge is shared (#1042): a foreign key's answer and a
    foreign key's failure change nothing — the screens that own
    those keys take them."""
    tab, bridge = make_tab(qt)
    bridge.succeeded.emit("workspaces-list", [])
    bridge.failed.emit("detail-row", "not this tab's")
    bridge.succeeded.emit("egress-switch-99", rules_frame(mode="allow"))
    bridge.failed.emit("egress-switch-99", "not this tab's")
    assert tab.mode_line.text() == "current mode: interactive"
    assert tab.note.text() == ""


def test_the_read_paints_the_allowlist(qt) -> None:
    """The editor's read (#1050): the landed frame's allowlist is
    the list the operator reads, the empty state's stand-in under
    a frame with no entries, and the buttons follow the ground —
    Add on a landed read, Remove on a selected row besides. A
    blanked read blanks the list the same way."""
    tab, _bridge = make_tab(qt)
    assert entry_rows(tab) == [".debian.org"]
    assert not tab.empty.isVisibleTo(tab)
    assert tab.add_button.isEnabled()
    assert not tab.remove_button.isEnabled()  # nothing selected
    tab.push(
        {
            "event": "egress.rules",
            "data": rules_frame(allow_list=(".debian.org", "example.com")),
        }
    )
    assert entry_rows(tab) == [".debian.org", "example.com"]
    tab.entries.setCurrentRow(1)  # the selection gates Remove
    assert tab.remove_button.isEnabled()
    tab.take("not a frame")  # the blanked read
    assert entry_rows(tab) == []
    assert tab.empty.isVisibleTo(tab)
    assert not tab.add_button.isEnabled()


def test_add_posts_the_whole_list_and_lands_with_its_effect(
    qt, monkeypatch
) -> None:
    """The Add action (#1050): an accepted entry joins the list's
    end and the whole new list posts through the policy route
    with the standing mode — the row shows the operator's edit at
    once, the reply's fresh frame repaints it, and the note names
    the write's effect with the reply's fact. A write already out
    gates the next add: the form never opens."""
    tab, bridge = make_tab(qt)
    built = fake_entry_dialog(monkeypatch, "example.com:443")
    tab.add()
    assert built == [tab]  # the opener lands as the parent (#1014)
    assert bridge.calls == [
        (
            tab.write_key,
            "set_egress_mode",
            ("ws-a", "interactive", False, [".debian.org", "example.com:443"]),
        )
    ]
    assert entry_rows(tab) == [".debian.org", "example.com:443"]
    assert tab.note.text() == "saving …"
    tab.add()  # one write at a time: the form never opens again
    assert len(built) == 1
    assert len(bridge.calls) == 1
    bridge.succeeded.emit(
        tab.write_key,
        {
            **rules_frame(allow_list=(".debian.org", "example.com:443")),
            "applied": True,
        },
    )
    assert entry_rows(tab) == [".debian.org", "example.com:443"]
    assert tab.note.text() == "allowlist saved — in effect now"
    assert tab.add_button.isEnabled()  # the flight cleared


def test_a_cancelled_add_form_sends_nothing(qt, monkeypatch) -> None:
    """A cancelled form (#1050): nothing composes — the bridge
    hears no call and the list stands."""
    tab, bridge = make_tab(qt)
    fake_entry_dialog(monkeypatch, "")
    tab.add()
    assert bridge.calls == []
    assert entry_rows(tab) == [".debian.org"]
    tab.remove()  # nothing selected: the remove decides nothing
    assert bridge.calls == []


def test_a_read_blank_under_the_form_decides_nothing(qt, monkeypatch) -> None:
    """The change's own gate (#1050): a read that lands while
    the form stands can blank the posture (a malformed answer);
    the closed form's composition then decides nothing — no
    ground, no write — the same ground the add's own gate
    reads before the form opens."""
    tab, bridge = make_tab(qt)

    class BlankDialog:
        def __init__(self, parent=None) -> None:
            self.entry = "example.com"

        def exec(self) -> int:
            tab.take("not a frame")  # the read blanks under the form
            return 1

    monkeypatch.setattr(egress_mod, "EntryDialog", BlankDialog)
    tab.add()
    assert bridge.calls == []
    assert entry_rows(tab) == []


def test_remove_posts_the_whole_list_and_lands(qt) -> None:
    """The Remove action (#1050): the selected row's entry leaves
    the list and the remaining list posts whole through the
    policy route; the reply repaints and the note names the
    effect — a stopped workspace's write waits for the next
    start."""
    tab, bridge = make_tab(qt)
    tab.entries.setCurrentRow(0)
    tab.remove()
    assert bridge.calls == [
        (tab.write_key, "set_egress_mode", ("ws-a", "interactive", False, []))
    ]
    assert entry_rows(tab) == []  # the operator's edit at once
    assert tab.empty.isVisibleTo(tab)
    bridge.succeeded.emit(
        tab.write_key, {**rules_frame(allow_list=()), "applied": False}
    )
    assert entry_rows(tab) == []
    assert tab.note.text() == "allowlist saved — takes effect at next start"


def test_an_invalid_entry_surfaces_the_routes_reason(qt, monkeypatch) -> None:
    """A rejected entry (#1050): the route's own reason — the 400
    ``parse_allowlist`` answers — lands whole on the note line and
    the daemon restores the list from its standing frame: the
    daemon is the authority, and the refused entry rides no later
    write."""
    tab, bridge = make_tab(qt)
    fake_entry_dialog(monkeypatch, "not a host!")
    tab.add()
    assert entry_rows(tab) == [".debian.org", "not a host!"]
    bridge.failed.emit(
        tab.write_key, "invalid egress allowlist entries: 'not a host!'"
    )
    assert tab.note.text() == (
        "refused: invalid egress allowlist entries: 'not a host!'"
    )
    assert entry_rows(tab) == [".debian.org"]  # the restore
    fake_entry_dialog(monkeypatch, "example.com")  # the editor stands ready
    tab.add()
    assert len(bridge.calls) == 2


def test_removing_the_last_entry_under_static_asks_before_it_sends(
    qt, monkeypatch
) -> None:
    """The offline gate before the editor's send (#1050): a
    remove that empties a static posture with no in-effect
    allowed verdict asks the shared question first — a No sends
    nothing, a Yes sends the confirmed write with the empty
    list."""
    frame = rules_frame(mode="static", allow_list=(".d.org",), allowed=())
    tab, bridge = make_tab(qt)
    bridge.succeeded.emit(tab.list_key, frame)
    asks = fake_confirm(monkeypatch, False)
    tab.entries.setCurrentRow(0)
    tab.remove()
    assert asks == [(tab, "Switch to static", EMPTY_STATIC_QUESTION)]
    assert bridge.calls == []  # the No decided nothing
    asks = fake_confirm(monkeypatch, True)
    tab.remove()
    assert bridge.calls == [
        (tab.write_key, "set_egress_mode", ("ws-a", "static", True, []))
    ]


def test_the_routes_confirm_refusal_on_a_write_asks_and_resends(
    qt, monkeypatch
) -> None:
    """The route's own refusal on a write (#1050): a remove that
    passed the local gate — the read held an in-effect allowed
    verdict — can still meet the daemon's nothing-effectively-
    allowed refusal when the verdict left between the read and
    the send. The refusal surfaces as the shared question: a Yes
    resends the composition confirmed, a No rolls the list back
    and decides nothing."""
    frame = rules_frame(mode="static", allow_list=(".d.org",))
    tab, bridge = make_tab(qt)
    bridge.succeeded.emit(tab.list_key, frame)
    tab.entries.setCurrentRow(0)
    tab.remove()
    assert bridge.calls[-1] == (
        tab.write_key,
        "set_egress_mode",
        ("ws-a", "static", False, []),
    )
    asks = fake_confirm(monkeypatch, True)
    bridge.failed.emit(tab.write_key, EMPTY_REFUSAL)
    assert asks == [(tab, "Switch to static", EMPTY_STATIC_QUESTION)]
    assert bridge.calls[-1] == (
        tab.write_key,
        "set_egress_mode",
        ("ws-a", "static", True, []),
    )
    asks = fake_confirm(monkeypatch, False)
    bridge.failed.emit(tab.write_key, EMPTY_REFUSAL)
    assert len(asks) == 1
    assert tab.note.text() == ""  # the No decided nothing
    assert entry_rows(tab) == [".d.org"]  # the restore


def test_one_policy_flight_gates_the_other_shape(qt, monkeypatch) -> None:
    """The route is one (#1050): a write already out gates the
    switch's send, and a switch already out gates the editor's —
    the two shapes never race each other to the policy route."""
    tab, bridge = make_tab(qt)
    fake_entry_dialog(monkeypatch, "example.com")
    tab.add()
    choose(tab, "allow")  # a write is out: the pick sends nothing
    assert len(bridge.calls) == 1
    bridge.succeeded.emit(
        tab.write_key,
        {
            **rules_frame(allow_list=(".debian.org", "example.com")),
            "applied": True,
        },
    )
    choose(tab, "allow")  # the switch flies
    assert len(bridge.calls) == 2
    fake_entry_dialog(monkeypatch, "example.org")
    tab.add()  # a switch is out: the form never opens
    assert len(bridge.calls) == 2
    bridge.succeeded.emit(
        tab.switch_key, {**rules_frame(mode="allow"), "applied": True}
    )
    tab.add()  # the flight cleared: the editor writes
    assert len(bridge.calls) == 3


def test_a_reply_that_is_not_a_frame_names_the_effect(qt, monkeypatch) -> None:
    """The landing note's tolerance (#1050): a reply that carries
    no frame — no route shape, only the tab's own — blanks the
    posture like the read's own arm and still names the write's
    effect from the reply's facts."""
    tab, bridge = make_tab(qt)
    fake_entry_dialog(monkeypatch, "example.com")
    tab.add()
    bridge.succeeded.emit(tab.write_key, "not a frame")
    assert tab.mode_line.text() == "current mode: —"
    assert entry_rows(tab) == []
    assert tab.note.text() == "allowlist saved — takes effect at next start"


def test_a_read_under_a_pending_write_keeps_the_edit(qt, monkeypatch) -> None:
    """A read the page raced under the write (#1050): the
    operator's edit stands — the answer repaints, and an older
    frame cannot roll the posted list back out from under it."""
    tab, bridge = make_tab(qt)
    fake_entry_dialog(monkeypatch, "example.com")
    tab.add()
    tab.poke()
    bridge.succeeded.emit(tab.list_key, rules_frame())  # the older list
    assert entry_rows(tab) == [".debian.org", "example.com"]
    bridge.succeeded.emit(
        tab.write_key,
        {
            **rules_frame(allow_list=(".debian.org", "example.com")),
            "applied": True,
        },
    )
    assert entry_rows(tab) == [".debian.org", "example.com"]


def test_a_workspace_switch_cancels_the_write_flight(qt, monkeypatch) -> None:
    """The open's reset over the editor (#1050): a workspace
    switch cancels a write the old workspace still had out — its
    answer is not the new workspace's to take — and blanks the
    list for the new workspace's own read."""
    tab, bridge = make_tab(qt)
    fake_entry_dialog(monkeypatch, "example.com")
    tab.add()
    stale = tab.write_key
    tab.host("ws-b")
    assert entry_rows(tab) == []
    bridge.succeeded.emit(stale, rules_frame(mode="allow"))
    assert tab.mode_line.text() == "current mode: —"
    tab.poke()
    bridge.succeeded.emit(
        tab.list_key,
        {
            **rules_frame(allow_list=(".debian.org",)),
            "workspace_id": "ws-b",
        },
    )
    assert entry_rows(tab) == [".debian.org"]


def test_the_entry_dialog_builds_and_submits(qt) -> None:
    """The Add form itself (#1050): the accepted forms named on
    its note line, a blank submit keeping the form standing with
    its reason, and a whole entry accepting with the typed text
    stripped."""
    dialog = egress_mod.EntryDialog()
    qt.keep.append(dialog)
    assert dialog.windowTitle() == "Add an allowlist entry"
    assert dialog.entry == ""
    assert dialog.note.text() == egress_mod.ENTRY_HINT
    dialog.submit()  # blank: the required field says itself
    assert dialog.entry == ""
    assert dialog.note.text() == "the entry is the form's one required field"
    dialog.field.setText("  example.com  ")
    dialog.submit()
    assert dialog.entry == "example.com"
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted


def asking_with_frame(tab, monkeypatch, frame: dict) -> list:
    """The question's stub over a landing frame (#1050 review):
    the ask pushes the frame — a switch another surface made
    while the question stood — then answers Yes."""
    asks: list[tuple] = []

    def ask(parent, title: str, text: str) -> bool:
        asks.append((parent, title, text))
        tab.push({"event": "egress.rules", "data": frame})
        return True

    monkeypatch.setattr(egress_mod, "confirm", ask)
    return asks


def test_the_confirmed_write_keeps_the_composed_posture(
    qt, monkeypatch
) -> None:
    """The confirmation's unit (#1050 review): the posture the
    composition captured is the posture the confirmed write
    carries — a frame the modal ask lets land cannot repoint the
    write the operator answers for, on the gate's own ask and on
    the route's refusal ask alike."""
    tab, bridge = make_tab(qt)
    bridge.succeeded.emit(
        tab.list_key,
        rules_frame(mode="static", allow_list=(".d.org",), allowed=()),
    )
    tab.entries.setCurrentRow(0)
    asking_with_frame(tab, monkeypatch, rules_frame(mode="interactive"))
    tab.remove()  # the gate's ask: the frame lands under the question
    assert bridge.calls[-1] == (
        tab.write_key,
        "set_egress_mode",
        ("ws-a", "static", True, []),
    )
    bridge.succeeded.emit(
        tab.write_key,
        {
            **rules_frame(mode="static", allow_list=(), allowed=()),
            "applied": True,
        },
    )
    # The route's refusal arm: a verdict stood at the send and
    # left before the answer — the local gate passes, the daemon
    # refuses, and the ask lands the same racing frame.
    bridge.succeeded.emit(
        tab.list_key, rules_frame(mode="static", allow_list=(".d.org",))
    )
    tab.entries.setCurrentRow(0)
    tab.remove()
    assert bridge.calls[-1] == (
        tab.write_key,
        "set_egress_mode",
        ("ws-a", "static", False, []),
    )
    asking_with_frame(
        tab,
        monkeypatch,
        rules_frame(mode="interactive", allow_list=(".d.org",)),
    )
    bridge.failed.emit(tab.write_key, EMPTY_REFUSAL)
    assert bridge.calls[-1] == (
        tab.write_key,
        "set_egress_mode",
        ("ws-a", "static", True, []),
    )


def test_a_tick_that_changes_nothing_keeps_the_selection(qt) -> None:
    """The tick's repaint (#1050 review): a frame whose list the
    rows already show rebuilds nothing — the selection the
    Remove action reads survives the page's ticks — and a frame
    that changes the list rebuilds the rows, the selection going
    with it."""
    tab, _bridge = make_tab(qt)
    tab.entries.setCurrentRow(0)
    assert tab.remove_button.isEnabled()
    tab.take(rules_frame())  # the same list: the rows stand
    assert tab.entries.currentRow() == 0
    assert tab.remove_button.isEnabled()
    tab.take(rules_frame(allow_list=(".debian.org", "example.com")))
    assert tab.entries.currentRow() == -1  # a changed list rebuilds
    assert not tab.remove_button.isEnabled()


def test_each_accepted_form_persists_through_the_route(
    qt, monkeypatch
) -> None:
    """The daemon's accepted forms (#1050): one add of each — a
    host, a scope, an address, a CIDR, and host:port — each
    posts the whole list through the policy route and stays on
    the list the reply repaints."""
    tab, bridge = make_tab(qt)
    forms = (
        "example.com",
        "*.example.com",
        "10.1.2.3",
        "10.0.0.0/8",
        "example.com:443",
    )
    standing = [".debian.org"]
    for form in forms:
        fake_entry_dialog(monkeypatch, form)
        tab.add()
        standing = [*standing, form]
        assert bridge.calls[-1] == (
            tab.write_key,
            "set_egress_mode",
            ("ws-a", "interactive", False, standing),
        )
        bridge.succeeded.emit(
            tab.write_key,
            {**rules_frame(allow_list=tuple(standing)), "applied": True},
        )
        assert entry_rows(tab) == standing


def verdict_texts(tab) -> list[str]:
    """The verdicts list as a plain list of strings — the rows
    the operator reads."""
    return [
        tab.verdicts.item(row).text() for row in range(tab.verdicts.count())
    ]


def land_audit(tab, bridge, rows) -> None:
    """The trail's standing read landed — the cadence-forwarding
    tests' own arm, clearing the pending flag the first poke
    left."""
    bridge.succeeded.emit(tab.audit.key, rows)


def test_the_read_paints_the_verdicts(qt) -> None:
    """The verdicts' read (#1051): the frame's in-effect allowed
    verdicts paint one row each — the destination label with the
    duration beside it, port 0 reading as all ports and a
    missing duration as the dash — the empty state's stand-in
    under none, and a blanked read blanking the list."""
    tab, _bridge = make_tab(qt)
    assert verdict_texts(tab) == ["example.com:443 — forever"]
    assert not tab.verdicts_empty.isVisibleTo(tab)
    tab.push(
        {
            "event": "egress.rules",
            "data": rules_frame(
                allowed=(
                    {
                        "id": "req-0",
                        "dest_host": "plain.example",
                        "dest_port": 0,
                        "decision": "allowed",
                        "duration": None,
                        "decided_at": 1.0,
                        "decided_by": "token",
                    },
                )
            ),
        }
    )
    assert verdict_texts(tab) == ["plain.example (all ports) — -"]
    tab.push(
        {
            "event": "egress.rules",
            "data": rules_frame(allowed=()),
        }
    )
    assert verdict_texts(tab) == []
    assert tab.verdicts_empty.isVisibleTo(tab)
    tab.take("not a frame")  # the blanked read
    assert verdict_texts(tab) == []


def test_a_verdicts_tick_that_changes_nothing_keeps_the_selection(
    qt,
) -> None:
    """The verdicts' repaint guard (#1051): a frame whose rows
    the list already shows rebuilds nothing — the selection the
    Revoke action reads survives the page's ticks — and a frame
    that changes the rows rebuilds the list, the selection going
    with it."""
    tab, _bridge = make_tab(qt)
    tab.verdicts.setCurrentRow(0)
    assert tab.revoke_button.isEnabled()
    tab.take(rules_frame())  # the same rows: the list stands
    assert tab.verdicts.currentRow() == 0
    assert tab.revoke_button.isEnabled()
    tab.take(rules_frame(allowed=()))
    assert tab.verdicts.currentRow() == -1
    assert not tab.revoke_button.isEnabled()


def test_revoke_asks_then_flies_and_lands(qt, monkeypatch) -> None:
    """The Revoke action (#1051): the selected verdict's question
    first — No the default, a no deciding nothing — a yes sending
    the revoke through its own route, and the landing naming the
    consequence with both reads flying again — the fresh frame
    repaints the verdicts and the trail's row flips."""
    tab, bridge = make_tab(qt)
    land_audit(tab, bridge, [])
    bridge.calls.clear()
    tab.verdicts.setCurrentRow(0)
    asks = fake_confirm(monkeypatch, False)
    tab.revoke()
    question = egress_mod.revoke_question("example.com:443")
    assert asks == [(tab, "Revoke the verdict", question)]
    assert bridge.calls == []  # the no decided nothing
    asks = fake_confirm(monkeypatch, True)
    tab.revoke()
    assert bridge.calls == [(tab.revoke_key, "revoke", ("ws-a", "req-1"))]
    assert tab.note.text() == "revoking …"
    bridge.succeeded.emit(tab.revoke_key, {"id": "req-1", "revoked": True})
    assert tab.note.text() == egress_mod.REVOKE_LANDING
    assert [c[1] for c in bridge.calls if c[1] != "revoke"] == [
        "egress_rules",
        "egress_requests",
    ]
    tab.push(
        {
            "event": "egress.rules",
            "data": rules_frame(allowed=()),
        }
    )
    assert verdict_texts(tab) == []


def test_a_revoke_refusal_names_itself(qt, monkeypatch) -> None:
    """A revoke the daemon refuses (#1051): the route's own words
    land on the note line and the row stands — a verdict the
    daemon no longer holds leaves with the next read."""
    tab, bridge = make_tab(qt)
    fake_confirm(monkeypatch, True)
    tab.verdicts.setCurrentRow(0)
    tab.revoke()
    bridge.failed.emit(tab.revoke_key, "no in-effect verdict with that id")
    assert tab.note.text() == ("refused: no in-effect verdict with that id")
    assert verdict_texts(tab) == ["example.com:443 — forever"]
    tab.revoke()  # the flight cleared: the ask flies again
    assert len(bridge.calls) == 2


def test_one_revoke_at_a_time(qt, monkeypatch) -> None:
    """One revoke flight at a time (#1051): a revoke already out
    holds the next ask — the question never opens over a flight
    the operator cannot answer for."""
    tab, bridge = make_tab(qt)
    asks = fake_confirm(monkeypatch, True)
    tab.verdicts.setCurrentRow(0)
    tab.revoke()
    tab.revoke()  # the flight gates before the question
    assert len(asks) == 1
    assert len(bridge.calls) == 1


def test_a_revoke_without_a_selection_decides_nothing(qt) -> None:
    """The Revoke action's own guard (#1051): no row selected, no
    question, no flight."""
    tab, bridge = make_tab(qt)
    tab.revoke()
    assert bridge.calls == []
    assert tab.note.text() == ""


def test_a_workspace_switch_cancels_the_revoke_flight(qt, monkeypatch) -> None:
    """The open's reset over the revoke (#1051): a workspace
    switch cancels a revoke the old workspace still had out —
    its answer is not the new workspace's to take — and the
    stale answer changes nothing."""
    tab, bridge = make_tab(qt)
    fake_confirm(monkeypatch, True)
    tab.verdicts.setCurrentRow(0)
    tab.revoke()
    stale = tab.revoke_key
    tab.host("ws-b")
    assert tab.note.text() == ""
    bridge.succeeded.emit(stale, {"id": "req-1", "revoked": True})
    assert tab.note.text() == ""
    assert not tab.pending_revoke


def test_deny_to_allowlist_composes_the_write(qt) -> None:
    """The deny-to-allowlist ask (#1051): the denied name joins
    the working list and the whole new list posts through the
    policy route with the standing mode — the editor's own write
    path — the reply naming the write's effect."""
    tab, bridge = make_tab(qt)
    tab.audit.allow_wanted.emit("denied.example")
    assert bridge.calls == [
        (
            tab.write_key,
            "set_egress_mode",
            ("ws-a", "interactive", False, [".debian.org", "denied.example"]),
        )
    ]
    assert entry_rows(tab) == [".debian.org", "denied.example"]
    bridge.succeeded.emit(
        tab.write_key,
        {
            **rules_frame(allow_list=(".debian.org", "denied.example")),
            "applied": True,
        },
    )
    assert tab.note.text() == "allowlist saved — in effect now"


def test_deny_to_allowlist_answers_a_duplicate(qt) -> None:
    """A denied name already listed (#1051): the ask answers
    itself on the note line — no write flies for a name the
    list already carries."""
    tab, bridge = make_tab(qt)
    tab.audit.allow_wanted.emit(".debian.org")
    assert tab.note.text() == ".debian.org already on the allowlist"
    assert bridge.calls == []


def test_deny_to_allowlist_under_a_standing_write(qt, monkeypatch) -> None:
    """The ask under a standing policy flight (#1051): the route
    is one — a switch or a write already out holds the ask, its
    answer on the note line, and no second write races the
    first."""
    tab, bridge = make_tab(qt)
    fake_entry_dialog(monkeypatch, "example.com")
    tab.add()
    tab.audit.allow_wanted.emit("denied.example")
    assert tab.note.text() == egress_mod.WRITE_HELD
    assert len(bridge.calls) == 1  # no second write races the first
    bridge.succeeded.emit(
        tab.write_key,
        {
            **rules_frame(allow_list=(".debian.org", "example.com")),
            "applied": True,
        },
    )
    tab.audit.allow_wanted.emit("denied.example")  # a read stands again
    assert len(bridge.calls) == 2


def test_the_tab_forwards_the_cadence_and_the_frames(qt) -> None:
    """The trail's cadence (#1051): the page's poke flies the
    trail's read beside the posture's, and a pushed resolved
    frame — the trail's newest row — flies it again."""
    tab, bridge = make_tab(qt)
    land_audit(tab, bridge, [])
    tab.poke()
    assert [c[1] for c in bridge.calls if c[1] != "egress_rules"] == [
        "egress_requests"
    ]
    land_audit(tab, bridge, [])
    tab.push(
        {
            "event": "egress.resolved",
            "data": {"workspace_id": "ws-a", "request_id": "req-2"},
        }
    )
    assert [c[1] for c in bridge.calls if c[1] == "egress_requests"]


def test_a_first_read_with_nothing_shows_the_stand_ins(qt) -> None:
    """The empty stand-ins on the first read (#1051 review): a
    tab whose first landed frame carries no verdicts and no
    allowlist entries builds nothing — and both stand-ins still
    say themselves."""
    bridge = StubBridge()
    tab = egress_mod.EgressTab(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tab)
    window.show()
    qt.keep.append(window)
    tab.host("ws-a")
    tab.poke()
    bridge.succeeded.emit(tab.list_key, rules_frame(allowed=(), allow_list=()))
    assert verdict_texts(tab) == []
    assert tab.verdicts_empty.isVisibleTo(tab)
    assert entry_rows(tab) == []
    assert tab.empty.isVisibleTo(tab)


def test_the_tabs_minimum_fits_the_windows_smallest_save(qt) -> None:
    """The geometry contract (#1051 review): the window restores
    a 620-tall saved rect, and the tab's minimum must stay under
    it — the two columns and the lists' zero minimums are the
    shape that keeps the restore whole."""
    tab, _bridge = make_tab(qt)
    assert tab.minimumSizeHint().height() <= 620


def test_the_trails_table_shares_the_lists_edges(qt) -> None:
    """The two columns' edges (#1154): the audit table starts
    where the allowlist list starts and ends where the verdicts
    list ends — the grid rows both columns share — with rows in
    every list, with every list empty and a refusal standing on
    the trail's note, and after the window grows. The trail's
    footer is one row: the Allowlist button keeps the Revoke
    row's own edges beside it."""
    tab, bridge = make_tab(qt)

    def edges(widget) -> tuple[int, int]:
        top = widget.mapTo(tab, QtCore.QPoint()).y()
        return top, top + widget.height()

    def aligned(tag: str) -> None:
        top, bottom = edges(tab.audit.table)
        assert top == edges(tab.entries)[0], tag
        assert bottom == edges(tab.verdicts)[1], tag
        assert edges(tab.audit.allow_button) == edges(tab.revoke_button), tag

    rows = [{**rules_frame()["allowed"][0], "requested_at": 1.0}]
    land_audit(tab, bridge, rows)  # a row in every list
    assert tab.audit.table.rowCount() == 1
    aligned("rows")

    bridge.succeeded.emit(  # every list empty, the trail refused
        tab.list_key,
        rules_frame(allowed=(), allow_list=()),
    )
    tab.poke()
    land_audit(tab, bridge, [])
    tab.poke()
    bridge.failed.emit(tab.audit.key, "the daemon is gone")
    assert tab.empty.isVisibleTo(tab)
    assert tab.verdicts_empty.isVisibleTo(tab)
    assert tab.audit.empty.isVisibleTo(tab.audit.footer)
    assert tab.audit.note.text() == "refused: the daemon is gone"
    aligned("empty")

    window = tab.window()
    window.resize(1280, 980)
    qt.app.processEvents()
    assert (window.width(), window.height()) == (1280, 980)
    aligned("grown")


def test_a_long_refusal_wraps_inside_the_footer_row(qt) -> None:
    """The footer's width contract (#1154 review): the trail's
    refusal note wraps in its row. A message far wider than the
    pane leaves the tab's minimum width where an empty note
    stands it, under the window's 1280 default, instead of
    holding the window open to the message's width."""
    tab, bridge = make_tab(qt)
    bare = tab.minimumSizeHint().width()
    assert bare < 1280
    tab.poke()
    failure = ("the daemon is gone " * 20).strip()
    bridge.failed.emit(tab.audit.key, failure)
    assert tab.audit.note.text() == f"refused: {failure}"
    assert tab.audit.note.wordWrap()
    assert tab.minimumSizeHint().width() == bare


def test_the_revoke_button_follows_the_flight(qt, monkeypatch) -> None:
    """The Revoke button's affordance (#1051 review): the button
    grays while the revoke flies — the editor buttons' own rule
    over the policy route — and both landing arms give it back."""
    tab, bridge = make_tab(qt)
    fake_confirm(monkeypatch, True)
    tab.verdicts.setCurrentRow(0)
    assert tab.revoke_button.isEnabled()
    tab.revoke()
    assert not tab.revoke_button.isEnabled()  # the flight holds it
    tab.verdicts.setCurrentRow(-1)  # no selection: the row guard alone
    bridge.failed.emit(tab.revoke_key, "no in-effect verdict with that id")
    assert not tab.revoke_button.isEnabled()
    tab.verdicts.setCurrentRow(0)  # the refusal gave the button back
    assert tab.revoke_button.isEnabled()
    tab.revoke()
    assert not tab.revoke_button.isEnabled()
    bridge.succeeded.emit(tab.revoke_key, {"id": "req-1", "revoked": True})
    tab.verdicts.setCurrentRow(0)  # the landing gave it back
    assert tab.revoke_button.isEnabled()


def test_deny_to_allowlist_under_an_unread_posture_answers_itself(
    qt,
) -> None:
    """The ask under no landed read (#1051 review): the write
    composes against nothing — no mode to write beside — and the
    note names that ground instead of claiming a flight."""
    bridge = StubBridge()
    tab = egress_mod.EgressTab(bridge)
    qt.keep.append(tab)
    tab.host("ws-a")
    tab.poke()  # the read flies, nothing lands
    tab.audit.allow_wanted.emit("denied.example")
    assert tab.note.text() == egress_mod.NO_READ
    assert [c for c in bridge.calls if c[1] == "set_egress_mode"] == []
