"""The consent decider controller (pure protocol state, #195)."""

import json

import pytest
from msks.client.tui import consent
from msks.client.tui.consent import ConsentController


def frame(event: str, data: dict) -> str:
    """One events-envelope frame as the daemon sends it."""
    return json.dumps({"event": event, "data": data})


def request_frame(rid: str, host: str = "api.example", port: int = 443) -> str:
    return frame(
        "egress.request",
        {
            "request": {
                "id": rid,
                "workspace_id": "ws",
                "dest_host": host,
                "dest_port": port,
                "requested_at": 100.0,
            }
        },
    )


def rules_frame() -> str:
    return frame(
        "egress.rules",
        {
            "workspace_id": "ws",
            "mode": "interactive",
            "allow_list": [".debian.org"],
            "allowed": [
                {
                    "id": "a1",
                    "dest_host": "api.example",
                    "dest_port": 443,
                    "decision": "allowed",
                    "duration": "5m",
                    "decided_at": 200.0,
                    "decided_by": "token",
                }
            ],
            "denied": [
                {
                    "id": "d1",
                    "dest_host": "203.0.113.7",
                    "dest_port": 0,
                    "decision": "denied",
                    "duration": "forever",
                    "decided_at": 201.0,
                    "decided_by": "token",
                }
            ],
        },
    )


def test_apply_frame_lifecycle() -> None:
    controller = ConsentController()
    action, payload = controller.apply_frame(request_frame("r1"))
    assert action == consent.ADDED
    assert payload.id == "r1"
    assert payload.dest_host == "api.example"
    assert list(controller.pending) == ["r1"]
    # Resolved drops it, carrying the decision.
    action, payload = controller.apply_frame(
        frame("egress.resolved", {"request_id": "r1", "decision": "allowed"})
    )
    assert action == consent.RESOLVED
    assert payload == ("r1", "allowed")
    assert controller.pending == {}
    # Rules replace the snapshot.
    action, payload = controller.apply_frame(rules_frame())
    assert action == consent.RULES
    assert payload.mode == "interactive"
    assert [rule.id for rule in payload.allowed] == ["a1"]
    assert [rule.id for rule in payload.denied] == ["d1"]


def test_apply_frame_ignores_malformed() -> None:
    controller = ConsentController()
    for raw in (
        "not json",
        "[1, 2]",
        json.dumps({"event": "unknown"}),
        json.dumps({"event": "egress.request", "data": {}}),
        json.dumps({"event": "egress.request", "data": {"request": 7}}),
        json.dumps({"event": "egress.rules", "data": {"workspace_id": 3}}),
        "",
    ):
        assert controller.apply_frame(raw) == (consent.IGNORED, None)
    assert controller.pending == {} and controller.rules is None
    # A resolved frame with no usable id reports the outcome but
    # drops nothing.
    assert controller.apply_frame('{"event": "egress.resolved"}') == (
        consent.RESOLVED,
        (None, None),
    )


def test_parse_request_degrades() -> None:
    row = consent.parse_request(
        {
            "id": "r",
            "workspace_id": "ws",
            "dest_host": "",
            "dest_port": True,
            "requested_at": "soon",
        }
    )
    assert row is not None
    assert row.dest_host == "" and row.dest_port == 0
    assert row.requested_at == 0.0


def test_parse_rule_degrades() -> None:
    rule = consent.parse_rule(
        {
            "id": "",
            "dest_host": "h",
            "dest_port": None,
            "decision": "",
            "duration": 5,
            "decided_at": False,
            "decided_by": 7,
        }
    )
    assert rule is not None
    assert rule.duration is None and rule.decided_at is None
    assert rule.decided_by is None and rule.dest_port == 0
    assert consent.parse_rule("junk") is None
    assert consent.parse_rule_rows("junk") == ()
    assert consent.parse_rule_rows([{"id": "x"}]) != ()


def test_ordered_and_countdowns() -> None:
    clock = [150.0]
    controller = ConsentController(hold_timeout=120.0, clock=lambda: clock[0])
    controller.apply_frame(request_frame("older"))
    late = json.loads(request_frame("newer"))
    late["data"]["request"]["requested_at"] = 200.0
    controller.apply_frame(json.dumps(late))
    assert [r.id for r in controller.ordered()] == ["older", "newer"]
    assert controller.remaining(controller.ordered()[0]) == pytest.approx(
        70.0, abs=0.01
    )
    clock[0] += 1000.0
    assert controller.remaining(controller.ordered()[0]) == 0.0  # clamped


def test_rule_countdowns() -> None:
    clock = [400.0]
    controller = ConsentController(clock=lambda: clock[0])
    controller.apply_frame(rules_frame())
    allowed = controller.rules.allowed[0]
    denied = controller.rules.denied[0]
    assert controller.rule_remaining(allowed) == pytest.approx(
        200.0 + 300.0 - 400.0, abs=0.01
    )
    assert controller.rule_remaining(denied) is None  # forever
    clock[0] += 200.0
    assert controller.rule_remaining(allowed) == 0.0
    unknown = consent.ConsentRule(
        id="u",
        dest_host="h",
        dest_port=0,
        decision="allowed",
        duration="1w",
        decided_at=1.0,
        decided_by=None,
    )
    assert controller.rule_remaining(unknown) is None  # unknown duration
    undated = consent.ConsentRule(
        id="n",
        dest_host="h",
        dest_port=0,
        decision="allowed",
        duration="5m",
        decided_at=None,
        decided_by=None,
    )
    assert controller.rule_remaining(undated) is None


def test_reset_clears_state() -> None:
    controller = ConsentController()
    controller.apply_frame(request_frame("r1"))
    controller.apply_frame(rules_frame())
    controller.reset()
    assert controller.pending == {} and controller.rules is None


def test_fmt_duration_labels() -> None:
    assert consent.fmt_duration(45) == "45s"
    assert consent.fmt_duration(300) == "5m"
    assert consent.fmt_duration(7200) == "2h"
    assert consent.fmt_duration(3 * 86400) == "3d"


def test_decode_frame_shapes() -> None:
    assert consent.decode_frame('{"a": 1}') == {"a": 1}
    assert consent.decode_frame("[]") is None
    assert consent.decode_frame("nope") is None
    assert consent.decode_frame("") is None


def test_parse_request_refuses_nonstring_ids() -> None:
    assert consent.parse_request({"id": 5, "workspace_id": "ws"}) is None
    assert consent.parse_request({"id": "r", "workspace_id": None}) is None
