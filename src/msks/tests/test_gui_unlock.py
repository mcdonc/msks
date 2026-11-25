"""The GUI's unlock flow (#1147): the window's presenter over the
stub bridge's signal emits — the landing's offer on the listing's
status line, the passphrase prompt between the two flights (the
window's one per session, the button's by hand), the spent
prompt's line, the dismissed prompt's cancel line, the refused
flight's flash with the re-run the feed's next serve fires
(#920), and one ask at a time. The two flights' own exchanges are
the data suite's (test_gui_data), the prompt's shape the forms
suite's, and the shared halves the client's own unlock suite
holds."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client.data import RestFailure  # noqa: E402
from msks.client.gui import unlock as unlock_mod  # noqa: E402
from test_gui_secrets import FakeSignal, StubBridge  # noqa: E402

KEY = Path("/keys/enc-key")
PREPARE = ("unlock-prepare#1", "unlock_prepare", (False,))
BY_HAND = ("unlock-prepare#1", "unlock_prepare", (True,))
CANCEL_LINE = (
    "secrets stay locked — the unlock needs the key file's "
    "passphrase (enc-key)"
)
SPENT_LINE = (
    "secrets stay locked — this session's passphrase prompt was "
    "already asked; the Secrets tab's Unlock button asks again"
)


class Screen:
    """A status line's stand-in (#1147): the outcomes and the
    refusals the flow lands on the screen the ask stood on."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []
        self.refusals: list[tuple[str, bool]] = []

    def outcome(self, message: str) -> None:
        self.outcomes.append(message)

    def refuse(self, message: str, *, reach: bool) -> None:
        self.refusals.append((message, reach))


class SecretsTab(Screen):
    """The Secrets tab's stand-in: its status line beside the
    button's ask signal the flow connects to."""

    def __init__(self) -> None:
        super().__init__()
        self.unlock_asked = FakeSignal()


def offering(line: str | None = None, encrypted: bool = True) -> dict:
    """The first flight's answer as the data layer shapes it: the
    locked rows, the key file, and whether it asks for a
    passphrase — or the line that ends the flow instead, the
    rows None beside it."""
    return {
        "line": line,
        "rows": None if line is not None else [{"id": 1}],
        "path": KEY,
        "encrypted": encrypted,
    }


@pytest.fixture()
def flow(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The flow over a stub bridge and a stub tree: the prompt
    replaced by a recorder answering ``answer`` (``"pw"`` unless a
    test steers it), the listing and the Secrets tab recording
    their status lines. The landing's offer is out when the test
    starts — the state every window opens in."""
    ns = SimpleNamespace(answer="pw", prompts=[])
    window = object()

    def prompt(parent, title, label, accept="OK") -> str:
        ns.prompts.append((parent, title, label, accept))
        return ns.answer

    monkeypatch.setattr(unlock_mod, "prompt_password", prompt)
    ns.window = window
    ns.flow = unlock_mod.UnlockFlow(window)
    ns.bridge, ns.listing, ns.secrets = StubBridge(), Screen(), SecretsTab()
    ns.flow.retarget(ns.bridge, ns.listing, ns.secrets)
    return ns


def redial(ns: SimpleNamespace) -> None:
    """A connection pick's re-dial (#1009): a fresh tree under the
    same window and flow."""
    ns.bridge, ns.listing, ns.secrets = StubBridge(), Screen(), SecretsTab()
    ns.flow.retarget(ns.bridge, ns.listing, ns.secrets)


def test_the_landing_offers_the_unlock_on_the_listing(flow) -> None:
    """The landing's offer (#1147, #515): the fetch goes out at
    once on the listing's behalf, nothing asked before it
    answers; a quiet answer ends the flow with nothing said, and
    a named line lands on the listing's status line."""
    assert flow.bridge.calls == [PREPARE]
    assert flow.flow.busy
    assert flow.prompts == []
    flow.bridge.succeeded.emit(
        flow.flow.prepare_key,
        {"line": None, "rows": None, "path": None, "encrypted": False},
    )
    assert flow.listing.outcomes == []
    assert flow.listing.refusals == []
    assert not flow.flow.busy
    flow.flow.automatic()
    flow.bridge.succeeded.emit(
        flow.flow.prepare_key, offering("nothing to unlock")
    )
    assert flow.listing.outcomes == ["nothing to unlock"]
    assert flow.bridge.calls == [PREPARE, PREPARE]
    assert flow.flow.passphrase_asked is False


def test_an_encrypted_key_prompts_the_window_before_the_second_flight(
    flow,
) -> None:
    """The encrypted key file (#1147): the window is asked once,
    over the main window, with the TUI modal's own note and the
    Unlock word on the OK button; the typed passphrase flies with
    the second flight, and its outcome lands on the listing."""
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    assert flow.prompts == [
        (
            flow.window,
            "Unlock secrets",
            "enc-key passphrase for secrets unlock",
            "Unlock",
        )
    ]
    assert flow.bridge.calls[-1] == (
        "unlock-finish#1",
        "unlock_finish",
        ([{"id": 1}], KEY, "pw"),
    )
    assert flow.flow.passphrase_asked
    assert flow.flow.busy
    flow.bridge.succeeded.emit(
        flow.flow.finish_key, "unlocked 1 placeholder row"
    )
    assert flow.listing.outcomes == ["unlocked 1 placeholder row"]
    assert not flow.flow.busy


def test_a_plain_key_file_flies_without_a_prompt(flow) -> None:
    """A key file that needs no passphrase (#1147): the second
    flight flies at once with an empty answer, the window asked
    nothing, the session's prompt unspent."""
    flow.bridge.succeeded.emit(
        flow.flow.prepare_key, offering(encrypted=False)
    )
    assert flow.prompts == []
    assert flow.bridge.calls[-1] == (
        "unlock-finish#1",
        "unlock_finish",
        ([{"id": 1}], KEY, ""),
    )
    assert flow.flow.passphrase_asked is False


def test_a_dismissed_prompt_leaves_the_rows_named_locked(flow) -> None:
    """The dismissed prompt (#515): the cancel line names the
    passphrase the file needs, nothing flies, the ask still
    spends the session's prompt."""
    flow.answer = ""
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    assert flow.listing.outcomes == [CANCEL_LINE]
    assert [call[0] for call in flow.bridge.calls] == ["unlock-prepare#1"]
    assert flow.flow.passphrase_asked
    assert not flow.flow.busy


def test_the_automatic_flow_never_asks_twice(flow) -> None:
    """The session's one prompt (#515, #920): after the first ask,
    a re-dial's offer of an encrypted key answers the spent line
    on the new listing and prompts nothing — the by-hand action
    the line points at asks again."""
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    flow.bridge.succeeded.emit(
        flow.flow.finish_key, "unlocked 1 placeholder row"
    )
    assert len(flow.prompts) == 1
    redial(flow)
    # The fresh landing's flights carry its own generation.
    assert flow.bridge.calls == [
        ("unlock-prepare#2", "unlock_prepare", (False,))
    ]
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    assert len(flow.prompts) == 1
    assert flow.listing.outcomes == [SPENT_LINE]
    assert [call[0] for call in flow.bridge.calls] == ["unlock-prepare#2"]


def test_the_button_asks_by_hand(flow) -> None:
    """The Secrets tab's Unlock button (#515, the ``u`` action's
    counterpart): the ask on the tab's own status line, the fetch
    made as an explicit ask, and the passphrase asked whatever the
    session already asked."""
    flow.bridge.succeeded.emit(
        flow.flow.prepare_key,
        {"line": None, "rows": None, "path": None, "encrypted": False},
    )
    flow.secrets.unlock_asked.emit()
    assert flow.bridge.calls == [PREPARE, BY_HAND]
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    flow.bridge.succeeded.emit(
        flow.flow.finish_key, "unlocked 1 placeholder row"
    )
    assert flow.secrets.outcomes == ["unlocked 1 placeholder row"]
    assert flow.listing.outcomes == []
    assert len(flow.prompts) == 1
    # The session's prompt is spent — the button asks anyway.
    flow.secrets.unlock_asked.emit()
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    assert len(flow.prompts) == 2
    assert flow.bridge.calls[-1][0] == "unlock-finish#1"


def test_a_failed_fetch_flashes_and_arms_the_re_run(flow) -> None:
    """A start the daemon answered nothing (#920, #1147): the
    failure flashes as an outage on the listing and arms one
    re-run; the feed's next serve fires it, the arm clearing
    first. A refusal, a by-hand ask's failure, and a spent prompt
    arm nothing."""
    flow.bridge.failed.emit(
        flow.flow.prepare_key, RestFailure("msks: cannot reach", reach=True)
    )
    assert flow.listing.refusals == [
        ("unlock failed: msks: cannot reach", True)
    ]
    assert flow.flow.retry
    assert not flow.flow.busy
    flow.bridge.feed_served.emit()
    assert flow.bridge.calls == [PREPARE, PREPARE]
    assert not flow.flow.retry
    flow.bridge.feed_served.emit()  # one serve serves one re-run
    assert len(flow.bridge.calls) == 2

    flow.bridge.failed.emit(
        flow.flow.prepare_key, RestFailure("locked: no session", reach=False)
    )
    assert flow.listing.refusals[-1] == (
        "unlock failed: locked: no session",
        False,
    )
    assert not flow.flow.retry

    flow.secrets.unlock_asked.emit()
    flow.bridge.failed.emit(
        flow.flow.prepare_key, RestFailure("msks: cannot reach", reach=True)
    )
    assert not flow.flow.retry  # the by-hand ask arms nothing
    assert flow.secrets.refusals == [
        ("unlock failed: msks: cannot reach", True)
    ]

    # A spent prompt arms nothing: the automatic flow's own
    # finish fails against a daemon that went away after the
    # fetch, the session's prompt already spent by the by-hand
    # ask above.
    flow.secrets.unlock_asked.emit()
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    flow.bridge.succeeded.emit(
        flow.flow.finish_key, "unlocked 1 placeholder row"
    )
    assert flow.flow.passphrase_asked
    flow.flow.automatic()
    flow.bridge.succeeded.emit(
        flow.flow.prepare_key, offering(encrypted=False)
    )
    flow.bridge.failed.emit(
        flow.flow.finish_key, RestFailure("msks: cannot reach", reach=True)
    )
    assert not flow.flow.retry
    assert flow.listing.refusals[-1] == (
        "unlock failed: msks: cannot reach",
        True,
    )


def test_the_re_run_drops_when_the_prompt_was_spent(flow) -> None:
    """The arm's other rule (#920): a session that spent its
    prompt between the arm and the serve drops the re-run — the
    by-hand ask already answered the rows."""
    flow.bridge.failed.emit(
        flow.flow.prepare_key, RestFailure("msks: cannot reach", reach=True)
    )
    assert flow.flow.retry
    flow.secrets.unlock_asked.emit()
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    flow.bridge.succeeded.emit(
        flow.flow.finish_key, "unlocked 1 placeholder row"
    )
    calls = list(flow.bridge.calls)
    flow.bridge.feed_served.emit()
    assert flow.bridge.calls == calls
    assert not flow.flow.retry


def test_one_ask_at_a_time(flow) -> None:
    """The gate (#515's turnstile): a flow already standing takes
    no second ask — the button's and the automatic's alike — and
    a serve under it fires nothing; the standing ask answers on
    the screen it began on."""
    flow.secrets.unlock_asked.emit()
    flow.flow.automatic()
    assert flow.bridge.calls == [PREPARE]
    flow.bridge.failed.emit(
        flow.flow.prepare_key, RestFailure("msks: cannot reach", reach=True)
    )
    flow.flow.retry = True
    flow.flow.automatic()
    flow.bridge.feed_served.emit()
    assert len(flow.bridge.calls) == 2
    flow.bridge.succeeded.emit(
        flow.flow.prepare_key, offering("nothing to unlock")
    )
    assert flow.listing.outcomes == ["nothing to unlock"]


def test_an_answer_for_another_key_is_not_the_flows(flow) -> None:
    """The bridge carries every screen's answers (#1147): another
    key's success or failure changes nothing — the ask stands,
    the lines untouched. A second-flight answer that is no line
    says nothing."""
    flow.bridge.succeeded.emit("secrets-page", {"secrets": []})
    flow.bridge.failed.emit("secrets-page", RestFailure("nope", reach=False))
    assert flow.flow.busy
    assert flow.listing.outcomes == []
    assert flow.listing.refusals == []
    flow.bridge.succeeded.emit(flow.flow.finish_key, {"odd": True})
    assert flow.listing.outcomes == []
    assert not flow.flow.busy


def test_a_re_dial_lets_the_old_tree_go(flow) -> None:
    """The re-dial (#1009, #1147): the stopped tree's hooks let go
    — its bridge's answers and its tab's button reach no one —
    and the fresh tree's offer stands with the session's prompt
    count carried."""
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    flow.bridge.succeeded.emit(
        flow.flow.finish_key, "unlocked 1 placeholder row"
    )
    old_bridge, old_secrets = flow.bridge, flow.secrets
    redial(flow)
    calls = list(flow.bridge.calls)
    old_bridge.succeeded.emit(flow.flow.prepare_key, offering())
    old_bridge.failed.emit(
        flow.flow.prepare_key, RestFailure("msks: cannot reach", reach=True)
    )
    old_secrets.unlock_asked.emit()
    assert len(flow.prompts) == 1
    assert flow.bridge.calls == calls
    assert flow.listing.outcomes == []
    assert flow.listing.refusals == []
    assert flow.flow.passphrase_asked  # the session's count stands


def test_a_retired_landings_answer_lands_on_no_one(flow) -> None:
    """The generation keys (#1147): the stopped bridge of a
    re-dial runs out the calls it had out, and Qt may deliver what
    it queued before the unhook — such an answer carries the
    retired landing's key, and the flow takes it for no one. The
    fresh landing's ask stands, its gate held, nothing prompts,
    nothing reaches the new daemon, and no stale outage arms a
    re-run."""
    flow.bridge.succeeded.emit(flow.flow.prepare_key, offering())
    stale_prepare, stale_finish = flow.flow.prepare_key, flow.flow.finish_key
    redial(flow)
    calls = list(flow.bridge.calls)
    flow.flow.on_succeeded(stale_prepare, offering())
    flow.flow.on_succeeded(stale_finish, "unlocked 3 placeholder rows")
    flow.flow.on_failed(
        stale_prepare, RestFailure("msks: cannot reach", reach=True)
    )
    flow.flow.on_failed(
        stale_finish, RestFailure("msks: cannot reach", reach=True)
    )
    assert flow.bridge.calls == calls
    assert flow.flow.busy  # the fresh landing's ask stands
    assert not flow.flow.retry
    assert len(flow.prompts) == 1
    assert flow.listing.outcomes == []
    assert flow.listing.refusals == []


def test_a_tab_qt_already_destroyed_takes_its_hook_with_it(flow) -> None:
    """The retired tab Qt already destroyed (#1147): its signal's
    disconnect raises the binding's deleted-object error, the
    landing goes on, and the fresh tree's offer stands as before
    — Qt severs a destroyed sender's connections itself."""

    class DestroyedSignal(FakeSignal):
        def disconnect(self, slot) -> None:
            raise RuntimeError("Internal C++ object already deleted")

    flow.secrets.unlock_asked = DestroyedSignal()
    redial(flow)
    assert flow.bridge.calls == [
        ("unlock-prepare#2", "unlock_prepare", (False,))
    ]
    assert flow.flow.busy


def test_the_outcome_line_reads_only_a_line() -> None:
    """The second flight's answer (#1147): the line as sent, and
    anything else as nothing to say."""
    assert unlock_mod.outcome_line("unlocked 1 placeholder row") == (
        "unlocked 1 placeholder row"
    )
    assert unlock_mod.outcome_line(None) is None
    assert unlock_mod.outcome_line({"line": "x"}) is None


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` real widgets need, the GUI
    suites' fixture shape (#939)."""
    import os

    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    yield SimpleNamespace(app=app, QtCore=QtCore, QtWidgets=QtWidgets)
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def test_a_re_dial_unhooks_the_real_tabs(qt, monkeypatch) -> None:
    """The re-dial over real widgets (#1009, #1147): open_window's
    landing swaps the main window's central widget and retargets
    the flow in one turn of the loop, before Qt's deferred delete
    of the retired strip runs — the flow unhooks the retired
    tree's Secrets tab while it still stands, its button reaching
    no one afterwards, while the fresh tab's button reaches the
    flow. (After a turn the retired tab is destroyed, and Qt
    severs a destroyed sender's connections itself.)"""
    from msks.client.gui.secrets import tab as secrets_mod

    prompts: list = []
    monkeypatch.setattr(
        unlock_mod,
        "prompt_password",
        lambda *args, **kwargs: prompts.append(args) or "pw",
    )
    window = qt.QtWidgets.QMainWindow()
    flow = unlock_mod.UnlockFlow(window)
    first_bridge, second_bridge = StubBridge(), StubBridge()
    first, second = (
        secrets_mod.SecretsTab(first_bridge),
        secrets_mod.SecretsTab(second_bridge),
    )
    tabs = qt.QtWidgets.QTabWidget()
    tabs.addTab(first, "Secrets")
    window.setCentralWidget(tabs)
    flow.retarget(first_bridge, Screen(), first)
    first_bridge.succeeded.emit(flow.prepare_key, offering())
    first_bridge.succeeded.emit(flow.finish_key, "unlocked 1 placeholder row")

    # The re-dial: a fresh strip replaces the central one, as
    # open_window's land() does, then the flow follows it.
    tabs = qt.QtWidgets.QTabWidget()
    tabs.addTab(second, "Secrets")
    window.setCentralWidget(tabs)
    second_listing = Screen()
    flow.retarget(second_bridge, second_listing, second)
    # The fresh tree's own offer answers quietly first — the
    # gate holds one ask at a time.
    second_bridge.succeeded.emit(
        flow.prepare_key,
        {"line": None, "rows": None, "path": None, "encrypted": False},
    )
    calls = list(second_bridge.calls)
    first.unlock_asked.emit()
    assert second_bridge.calls == calls
    second.unlock_asked.emit()
    assert second_bridge.calls == calls + [
        ("unlock-prepare#2", "unlock_prepare", (True,))
    ]
    second_bridge.succeeded.emit(flow.prepare_key, offering())
    # The button asks by hand whatever the session asked: the
    # first tree's flow spent the prompt, the second tree's
    # button asks anyway, and its second flight flies.
    assert len(prompts) == 2
    assert second_bridge.calls[-1][0] == "unlock-finish#2"
    assert flow.passphrase_asked
    window.close()
    window.deleteLater()
    qt.app.processEvents()
