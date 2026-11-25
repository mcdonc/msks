"""Consent controls through Qt widgets and the existing presenter seam."""

from types import SimpleNamespace

import pytest
from test_gui_consent import request_raw, resolved_raw
from test_gui_workspace import qt as qt


class FakePresenter:
    """Replaceable rows and recorded decisions, without a controller."""

    def __init__(self, rows=()):
        self.models = list(rows)
        self.decisions = []
        self.decide_keys = set()

    def rows(self):
        return self.models

    def decide(self, *args):
        self.decisions.append(args)

    def retire(self, key):
        self.decide_keys.discard(key)


@pytest.fixture()
def panel(qt):
    from msks.client.gui import consentpanel
    from msks.client.gui.consent import ConsentRow

    class Bridge(qt.QtCore.QObject):
        succeeded = qt.QtCore.Signal(str, object)
        failed = qt.QtCore.Signal(str, object)
        decider_landed = qt.QtCore.Signal(str, object)

        def __init__(self):
            super().__init__()
            self.calls = []
            self.fail_now = False

        def call(self, key, method, *args):
            self.calls.append((key, method, args))
            if self.fail_now:
                self.failed.emit(key, RuntimeError("bridge stopped"))

    bridge = Bridge()
    widget = consentpanel.ConsentPanel(bridge)
    widget.resize(800, 240)
    held = FakePresenter(
        [ConsentRow("req-1", "api.github.com:443", 58, "58s")]
    )
    widget.host(held)
    widget.show()
    qt.app.processEvents()
    return SimpleNamespace(
        widget=widget, presenter=held, bridge=bridge, Row=ConsentRow
    )


def button(qt, panel, decision="allow", rid="req-1"):
    return panel.widget.findChild(
        qt.QtWidgets.QToolButton, f"{decision}-{rid}"
    )


def label(qt, panel, kind="destination", rid="req-1"):
    return panel.widget.findChild(qt.QtWidgets.QLabel, f"{kind}-{rid}")


def real_presenter(panel):
    from msks.client.gui.consent import ConsentPresenter

    held = ConsentPresenter("ws-a", panel.bridge, clock=lambda: 1000.0)
    held.controller.apply_frame(request_raw())
    panel.widget.host(held)
    return held


def test_empty_queue_collapses_panel(qt, panel):
    panel.presenter.models.clear()
    panel.widget.refresh()
    assert panel.widget.isHidden()
    panel.widget.host(None)
    assert panel.widget.isHidden()


def test_rows_follow_presenter_order(qt, panel):
    panel.presenter.models = [
        panel.Row("second", "<b>host</b>:443", 0, "0s"),
        panel.Row("first", "other (all ports)", 20, "20s"),
    ]
    panel.widget.refresh()
    qt.app.processEvents()
    top = label(qt, panel, rid="second")
    bottom = label(qt, panel, rid="first")
    assert top.text() == "<b>host</b>:443"
    assert top.textFormat() == qt.QtCore.Qt.TextFormat.PlainText
    assert bottom.text() == "other (all ports)"
    assert (
        top.mapTo(panel.widget, top.rect().topLeft()).y()
        < bottom.mapTo(panel.widget, bottom.rect().topLeft()).y()
    )
    assert not panel.widget.isHidden()  # zero countdown is not resolution


def test_refresh_updates_countdown_without_replacing_controls(qt, panel):
    action = button(qt, panel)
    action.setFocus()
    panel.presenter.models[0] = panel.Row(
        "req-1", "api.github.com:443", 57, "57s"
    )
    panel.widget.refresh()
    assert button(qt, panel) is action
    assert action.hasFocus()
    assert label(qt, panel, "countdown").text() == "57s"


def test_focused_removal_follows_visual_order_after_older_hold_arrives(
    qt, panel
):
    panel.presenter.models += [panel.Row("next", "next:443", 20, "20s")]
    panel.widget.refresh()
    panel.presenter.models.insert(
        0, panel.Row("older", "older:443", 20, "20s")
    )
    panel.widget.refresh()
    qt.app.processEvents()
    button(qt, panel).setFocus()
    panel.presenter.models.pop(1)
    panel.widget.refresh()
    assert button(qt, panel, rid="next").hasFocus()
    # The last row's removal falls back to the preceding surviving row.
    panel.presenter.models.pop()
    panel.widget.refresh()
    assert button(qt, panel, rid="older").hasFocus()


def test_removed_row_sends_nothing(qt, panel):
    choice = button(qt, panel).menu().actions()[0]
    panel.presenter.models.clear()
    choice.trigger()
    assert panel.presenter.decisions == []


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_main_action_uses_tilrestart(qt, panel, decision):
    button(qt, panel, decision).click()
    assert panel.presenter.decisions == [("req-1", decision, "tilrestart")]


@pytest.mark.parametrize("decision", ["allow", "deny"])
@pytest.mark.parametrize(
    "duration", ["once", "5m", "15m", "tilrestart", "forever"]
)
def test_duration_action_sends_immediately(qt, panel, decision, duration):
    actions = button(qt, panel, decision).menu().actions()
    assert [a.text() for a in actions] == [
        "once",
        "5m",
        "15m",
        "tilrestart",
        "forever",
    ]
    next(a for a in actions if a.text() == duration).trigger()
    assert panel.presenter.decisions == [("req-1", decision, duration)]


def test_menu_cancel_sends_nothing(qt, panel):
    from PySide6 import QtTest

    action = button(qt, panel)
    menu = action.menu()
    menu.popup(action.mapToGlobal(action.rect().bottomLeft()))
    qt.app.processEvents()
    QtTest.QTest.keyClick(menu, qt.QtCore.Qt.Key.Key_Escape)
    assert panel.presenter.decisions == []
    assert not menu.isVisible()


def test_menu_choice_does_not_arm_next_click(qt, panel):
    action = button(qt, panel)
    action.menu().actions()[1].trigger()
    action.click()
    assert panel.presenter.decisions == [
        ("req-1", "allow", "5m"),
        ("req-1", "allow", "tilrestart"),
    ]


def test_success_keeps_hold_until_resolved_frame(qt, panel):
    held = real_presenter(panel)
    button(qt, panel).click()
    key, method, args = panel.bridge.calls[-1]
    assert method == "decide"
    assert args == ("ws-a", "req-1", "allow", "tilrestart")
    panel.bridge.succeeded.emit(key, {"verdict": {"decision": "allowed"}})
    assert held.decide_keys == set()
    assert not panel.widget.isHidden()
    held.controller.apply_frame(resolved_raw())
    panel.widget.refresh()
    assert panel.widget.isHidden()


def test_failed_verdict_flashes_and_retires_key(qt, panel):
    held = real_presenter(panel)
    button(qt, panel).click()
    panel.bridge.failed.emit(
        panel.bridge.calls[-1][0], RuntimeError("<refused>")
    )
    assert held.decide_keys == set()
    assert panel.widget.note.text() == "decide failed: <refused>"
    assert panel.widget.note.kind == "error"  # the error tint (#1160)
    assert panel.widget.note.textFormat() == qt.QtCore.Qt.TextFormat.PlainText
    assert not panel.widget.isHidden()


def test_stopped_bridge_failure_is_matched_synchronously(qt, panel):
    held = real_presenter(panel)
    panel.bridge.fail_now = True
    button(qt, panel).click()
    assert panel.widget.note.text() == "decide failed: bridge stopped"
    assert held.decide_keys == set()


def test_unrelated_and_old_keys_are_ignored(qt, panel):
    first = real_presenter(panel)
    button(qt, panel).click()
    old_key = panel.bridge.calls[-1][0]
    second = real_presenter(panel)
    button(qt, panel).click()
    live_key = panel.bridge.calls[-1][0]
    for key in ["unrelated", old_key]:
        panel.bridge.failed.emit(key, RuntimeError("old"))
        panel.bridge.succeeded.emit(key, {})
    assert panel.widget.note.text() == ""
    assert live_key in second.decide_keys
    assert old_key in first.decide_keys


@pytest.fixture()
def timeouts(qt, monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        qt.QtCore.QTimer,
        "singleShot",
        lambda ms, receiver, callback: callbacks.append((ms, callback)),
    )
    return callbacks


def fail(qt, panel, message):
    button(qt, panel).click()
    panel.bridge.failed.emit(panel.bridge.calls[-1][0], RuntimeError(message))


def test_new_flash_survives_old_timeout(qt, panel, timeouts):
    real_presenter(panel)
    fail(qt, panel, "first")
    fail(qt, panel, "second")
    assert [ms for ms, callback in timeouts] == [4000, 4000]
    timeouts[0][1]()
    assert panel.widget.note.text() == "decide failed: second"
    timeouts[1][1]()
    assert panel.widget.note.text() == ""


def test_refresh_preserves_flash(qt, panel):
    real_presenter(panel)
    fail(qt, panel, "refused")
    panel.widget.refresh()
    assert panel.widget.note.text() == "decide failed: refused"


def test_empty_queue_hides_flash(qt, panel):
    held = real_presenter(panel)
    fail(qt, panel, "refused")
    held.controller.reset()
    panel.widget.refresh()
    assert panel.widget.isHidden()


def test_host_clears_flash_and_invalidates_old_actions(qt, panel, timeouts):
    real_presenter(panel)
    old_action = button(qt, panel).menu().actions()[0]
    fail(qt, panel, "first")
    second = real_presenter(panel)
    old_action.trigger()
    assert len(panel.bridge.calls) == 1
    assert panel.widget.note.text() == ""
    fail(qt, panel, "second")
    timeouts[0][1]()
    assert panel.widget.note.text() == "decide failed: second"
    assert len(second.rows()) == 1


def test_timer_refresh_preserves_open_menu_and_focus(qt, panel):
    action = button(qt, panel)
    action.setFocus()
    menu = action.menu()
    menu.popup(action.mapToGlobal(action.rect().bottomLeft()))
    qt.app.processEvents()
    panel.widget.refresh()
    assert menu.isVisible()
    assert button(qt, panel) is action
    menu.close()
    assert panel.presenter.decisions == []


def test_keyboard_activation_uses_explicit_action(qt, panel):
    from PySide6 import QtTest

    action = button(qt, panel)
    action.setFocus()
    assert panel.presenter.decisions == []
    QtTest.QTest.keyClick(action, qt.QtCore.Qt.Key.Key_Space)
    assert panel.presenter.decisions == [("req-1", "allow", "tilrestart")]


def test_narrow_panel_wraps_actions(qt, panel):
    panel.widget.resize(330, 260)
    qt.app.processEvents()
    dest = label(qt, panel)
    action = button(qt, panel)
    assert (
        action.mapTo(panel.widget, action.rect().topLeft()).y()
        > dest.mapTo(panel.widget, dest.rect().bottomLeft()).y()
    )
    assert (
        action.mapTo(panel.widget, action.rect().bottomRight()).x()
        < panel.widget.width()
    )
    assert dest.toolTip() == "api.github.com:443"


def test_large_queue_scrolls_without_consuming_terminal_area(qt, panel):
    panel.presenter.models = [
        panel.Row(str(i), f"host{i}:443", 20, "20s") for i in range(5)
    ]
    panel.widget.refresh()
    qt.app.processEvents()
    scroll = panel.widget.findChild(qt.QtWidgets.QScrollArea)
    assert scroll.verticalScrollBar().maximum() > 0
    scroll.ensureWidgetVisible(button(qt, panel, rid="4"))
    qt.app.processEvents()
    assert scroll.verticalScrollBar().value() > 0


def test_the_countdown_turns_error_under_the_last_seconds(qt, panel):
    """The countdown's tint (#1160): the chrome's own text above
    the last seconds, the error tint under them, following each
    refresh without replacing the label."""
    from msks.client.gui import consentpanel

    countdown = label(qt, panel, "countdown")
    assert countdown.kind is None  # 58 seconds stand
    last = consentpanel.LAST_SECONDS
    panel.presenter.models[0] = panel.Row(
        "req-1", "api.github.com:443", last, f"{last}s"
    )
    panel.widget.refresh()
    assert label(qt, panel, "countdown") is countdown
    assert countdown.kind == "error"
    panel.presenter.models[0] = panel.Row(
        "req-1", "api.github.com:443", last + 1, f"{last + 1}s"
    )
    panel.widget.refresh()
    assert countdown.kind is None
    assert consentpanel.countdown_kind(last) == "error"
    assert consentpanel.countdown_kind(last + 1) is None
