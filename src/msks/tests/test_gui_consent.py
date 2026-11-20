"""The consent presenter (#1000, phase 5.5 of #924): the
decider's state and verdict seam — frames landing through the
TUI's pure controller, the row models the panel renders, and
the decide flight the panel sends. These tests run against
frame fakes and a stubbed bridge, the same shape the TUI's
consent suite runs under: no Qt, no daemon, no loop — the
controller's own clock pinned so the countdowns read exact.
"""

import importlib.util
import json

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui import consent as consent_mod  # noqa: E402
from msks.client.tui.consent import ADDED, IGNORED, RESOLVED, RULES  # noqa: E402

#: The pinned clock's now: every countdown reads against it.
NOW = 1000.0


class StubBridge:
    """The bridge's surface for the presenter's tests: the one
    verb the seam calls, every flight recorded with its key."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, key: str, method: str, *args: object):
        self.calls.append((key, method, args))
        return None


def presenter(ws: str = "ws-a") -> consent_mod.ConsentPresenter:
    """One presenter over the stub bridge, the clock pinned at
    :data:`NOW` — the countdowns' exact reads."""
    return consent_mod.ConsentPresenter(
        ws, StubBridge(), hold_timeout=120.0, clock=lambda: NOW
    )


def rules_raw(ws: str = "ws-a", mode: str = "interactive") -> str:
    """One ``egress.rules`` frame: the workspace's posture."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": ws,
                "mode": mode,
                "allow_list": ["api.github.com"],
            },
        }
    )


def request_raw(
    rid: str = "req-1",
    ws: str = "ws-a",
    host: str = "api.github.com",
    port: int = 443,
    at: float = 900.0,
    expires: float | None = 1120.0,
) -> str:
    """One ``egress.request`` frame: a held flow, with the
    daemon's deadline when one is given."""
    request = {
        "id": rid,
        "workspace_id": ws,
        "dest_host": host,
        "dest_port": port,
        "requested_at": at,
    }
    if expires is not None:
        request["expires_at"] = expires
    return json.dumps(
        {"event": "egress.request", "data": {"request": request}}
    )


def resolved_raw(rid: str = "req-1", ws: str = "ws-a") -> str:
    """One ``egress.resolved`` frame: a hold's exit."""
    return json.dumps(
        {
            "event": "egress.resolved",
            "data": {
                "workspace_id": ws,
                "request_id": rid,
                "decision": "allow",
            },
        }
    )


def test_frames_add_and_drop_holds() -> None:
    """The frames through the presenter's controller (#1000):
    ``egress.request`` adds a hold, ``egress.resolved`` drops
    one, and ``egress.rules`` replaces the posture snapshot —
    while a foreign workspace's frames change nothing, the
    controller's own ``owns()`` check, and junk lands ignored
    with the state untouched."""
    held = presenter()
    assert held.frame(rules_raw()) == (RULES, held.controller.rules)
    outcome, payload = held.frame(request_raw("req-1"))
    assert outcome == ADDED
    assert payload.dest_host == "api.github.com"
    assert [row.id for row in held.rows()] == ["req-1"]
    # A foreign workspace's hold plants nothing.
    assert held.frame(request_raw("req-9", ws="ws-b"))[0] == IGNORED
    assert [row.id for row in held.rows()] == ["req-1"]
    # The posture snapshot replaces whole.
    assert held.frame(rules_raw(mode="static"))[0] == RULES
    assert held.controller.rules.mode == "static"
    # The resolved frame drops the hold; another workspace's
    # resolution drops nothing.
    assert held.frame(resolved_raw("req-8", ws="ws-b"))[0] == RESOLVED
    assert [row.id for row in held.rows()] == ["req-1"]
    assert held.frame(resolved_raw("req-1"))[0] == RESOLVED
    assert held.rows() == []
    # Junk is ignored, never fatal.
    assert held.frame("not json at all")[0] == IGNORED


def test_rows_carry_the_destination_and_the_countdown() -> None:
    """The row models (#1000): the destination label —
    ``host:port``, port 0 reading as all ports — and the
    countdown off the frame's ``expires_at`` (the daemon's
    settings-driven deadline), oldest first through the
    controller's ``ordered()``."""
    held = presenter()
    held.frame(request_raw("req-2", at=901.0, expires=1010.0))
    held.frame(request_raw("req-1", at=900.0, expires=1120.0))
    held.frame(
        request_raw("req-3", host="pypi.org", port=0, at=902.0, expires=1075.0)
    )
    rows = held.rows()
    assert [(row.id, row.dest) for row in rows] == [
        ("req-1", "api.github.com:443"),
        ("req-2", "api.github.com:443"),
        ("req-3", "pypi.org (all ports)"),
    ]
    # The countdowns read the pinned clock: 120s, 10s, and 75s
    # left, the labels the controller's own math builds.
    assert [row.remaining for row in rows] == [120.0, 10.0, 75.0]
    assert [row.countdown for row in rows] == ["2m", "10s", "1m"]


def test_the_hold_timeout_is_the_countdowns_fallback() -> None:
    """A frame without ``expires_at`` counts from the
    controller's ``hold_timeout`` (#1000) — the fallback the
    daemon's settings-driven deadline otherwise wins — and a
    deadline the clock passed clamps at zero."""
    held = presenter()
    held.frame(request_raw("req-1", at=900.0, expires=None))
    assert held.rows()[0].remaining == 20.0  # 900 + 120 - 1000
    # The frame's own deadline wins over the fallback: a hold
    # requested late with an earlier deadline reads its own.
    held.frame(request_raw("req-2", at=990.0, expires=1005.0))
    held.frame(request_raw("req-3", at=900.0, expires=999.0))
    by_id = {row.id: row for row in held.rows()}
    assert by_id["req-2"].remaining == 5.0
    assert by_id["req-3"].remaining == 0.0
    assert by_id["req-3"].countdown == "0s"


def test_the_decide_call_sends_verdict_and_duration_together() -> None:
    """The verdict seam (#1000): one bridge flight per verdict,
    the decision and the duration chosen in the same action —
    ``tilrestart`` the default, the controller's own table —
    each flight under its own key."""
    held = presenter()
    held.frame(request_raw("req-1"))
    held.decide("req-1", "allow")
    assert held.bridge.calls == [
        (
            "consent-decide-1",
            "decide",
            ("ws-a", "req-1", "allow", "tilrestart"),
        )
    ]
    assert held.decide_key == "consent-decide-1"
    held.decide("req-1", "deny", "5m")
    assert held.bridge.calls[1] == (
        "consent-decide-2",
        "decide",
        ("ws-a", "req-1", "deny", "5m"),
    )
    assert held.decide_key == "consent-decide-2"


def test_a_row_clears_on_the_resolved_frame_not_the_click() -> None:
    """The row's exit (#1000): the decide flight drops nothing
    locally — the row stands past the click until the daemon's
    resolved frame drops it, the daemon the authority, a
    refused verdict flashing in the panel while the row stays."""
    held = presenter()
    held.frame(request_raw("req-1"))
    held.decide("req-1", "allow", "5m")
    assert [row.id for row in held.rows()] == ["req-1"]
    held.frame(resolved_raw("req-1"))
    assert held.rows() == []
