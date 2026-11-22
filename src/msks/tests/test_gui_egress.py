"""The workspace Egress tab (#1042's first sub-issue, #1049): the
posture surface over a stub bridge — the rules read painting the
mode, the switch through the policy route with its effect note,
the empty-static gate's question before the send, the route's own
refusal asking the same question again, and the pushed rules
frame repainting a switch another surface made. The widgets run
under the offscreen platform on every host.
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
from PySide6 import QtCore, QtGui  # noqa: E402

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
