"""The consent decider controller (pure protocol state, #195)."""

import json

import pytest
from msks.client.tui import consent
from msks.client.tui.consent import REJECTED, ConsentController


def frame(event: str, data: dict) -> str:
    """One events-envelope frame as the daemon sends it."""
    return json.dumps({"event": event, "data": data})


def request_frame(
    rid: str, host: str = "api.example", port: int = 443, workspace: str = "ws"
) -> str:
    return frame(
        "egress.request",
        {
            "request": {
                "id": rid,
                "workspace_id": workspace,
                "dest_host": host,
                "dest_port": port,
                "requested_at": 100.0,
            }
        },
    )


def rules_frame(workspace: str = "ws") -> str:
    return frame(
        "egress.rules",
        {
            "workspace_id": workspace,
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


def test_secret_events_append_to_the_log() -> None:
    """Each of the five interceptor audit frames lands in the log
    with its kind and fields (#201); the seq orders arrival."""
    controller = ConsentController()
    outcome, first = controller.apply_frame(
        frame(
            "secret.mint",
            {
                "placeholder_id": 4,
                "workspace_id": "ws",
                "name": "api",
                "dests": ["api.example"],
                "ts": 100.0,
            },
        )
    )
    assert outcome == consent.SECRET_EVENT
    assert (first.kind, first.seq) == ("mint", 1)
    assert first.dests == ("api.example",)
    outcome, swap = controller.apply_frame(
        frame(
            "secret.swap",
            {
                "placeholder_id": 4,
                "workspace_id": "ws",
                "name": "api",
                "host": "api.example",
                "ts": 101.0,
            },
        )
    )
    assert outcome == consent.SECRET_EVENT
    assert (swap.kind, swap.seq, swap.host) == ("swap", 2, "api.example")
    for name, kind in (
        ("secret.revoke", "revoke"),
        ("secret.expiry", "expiry"),
        ("secret.sighting", "sighting"),
    ):
        outcome, payload = controller.apply_frame(
            frame(name, {"workspace_id": "ws", "name": "api"})
        )
        assert outcome == consent.SECRET_EVENT
        assert payload.kind == kind
    assert [e.kind for e in controller.events] == [
        "mint",
        "swap",
        "revoke",
        "expiry",
        "sighting",
    ]
    assert [e.seq for e in controller.events] == [1, 2, 3, 4, 5]


def test_secret_events_ignore_malformed_without_a_slot() -> None:
    """An unusable secret frame is ignored and consumes no seq —
    the next good event's seq stays contiguous."""
    controller = ConsentController()
    for raw in (
        json.dumps({"event": "secret.mint", "data": {}}),
        json.dumps({"event": "secret.mint", "data": "junk"}),
        json.dumps({"event": "secret.mint"}),
        json.dumps({"event": "secret.unknown"}),
    ):
        assert controller.apply_frame(raw) == (consent.IGNORED, None)
    assert controller.events == []
    outcome, payload = controller.apply_frame(
        frame("secret.revoke", {"workspace_id": "ws", "name": "api"})
    )
    assert payload.seq == 1


def test_secret_events_degrade_on_older_daemon_fields() -> None:
    """A frame without its id, host, or timestamp still lands: the
    fields degrade to their empty forms."""
    controller = ConsentController()
    outcome, payload = controller.apply_frame(
        frame("secret.swap", {"workspace_id": 3, "name": "api"})
    )
    assert outcome == consent.IGNORED  # a non-string workspace is unusable
    outcome, payload = controller.apply_frame(
        frame("secret.swap", {"workspace_id": "ws", "name": "api"})
    )
    assert payload.placeholder_id is None
    assert payload.host is None
    assert payload.dests == ()
    assert payload.ts == 0.0
    # Junk shapes read as their empty forms, never raise.
    outcome, payload = controller.apply_frame(
        frame(
            "secret.mint",
            {
                "workspace_id": "ws",
                "name": "api",
                "placeholder_id": True,
                "dests": "api.example",
                "ts": "soon",
            },
        )
    )
    assert outcome == consent.SECRET_EVENT
    assert payload.placeholder_id is None
    assert payload.dests == ()
    assert payload.ts == 0.0


def test_foreign_workspace_secret_frames_are_ignored() -> None:
    """A foreign workspace's audit frame plants nothing — the #280
    rule carried to secret events: a foreign sighting flashing this
    decider's exfil alarm would be a false one. An empty own-id (the
    protocol default) accepts every frame."""
    controller = ConsentController(workspace_id="ws-a")
    sighting = frame(
        "secret.sighting",
        {"workspace_id": "ws-b", "name": "api", "host": "evil.example"},
    )
    assert controller.apply_frame(sighting) == (consent.IGNORED, None)
    assert controller.events == []
    own = frame(
        "secret.sighting",
        {"workspace_id": "ws-a", "name": "api", "host": "evil.example"},
    )
    outcome, payload = controller.apply_frame(own)
    assert outcome == consent.SECRET_EVENT
    assert payload.seq == 1  # the ignored frame used no slot
    unscoped = ConsentController()
    assert unscoped.apply_frame(sighting)[0] == consent.SECRET_EVENT


def test_an_unhashable_event_value_is_ignored() -> None:
    """A frame whose event field is a list (unhashable) is ignored,
    not raised into: the dict lookup would take the connection down
    a reconnect cycle for one malformed frame."""
    controller = ConsentController()
    raw = json.dumps({"event": ["secret.swap"], "data": {}})
    assert controller.apply_frame(raw) == (consent.IGNORED, None)


def test_secret_log_is_bounded_and_survives_reset() -> None:
    """The log keeps only the newest EVENT_LOG_MAX rows, and a
    reconnect's reset keeps it: the daemon does not re-send history,
    so a re-registration must not blank the tail the operator is
    reading."""
    controller = ConsentController()
    for i in range(consent.EVENT_LOG_MAX + 10):
        controller.apply_frame(
            frame("secret.swap", {"workspace_id": "ws", "name": f"n{i}"})
        )
    assert len(controller.events) == consent.EVENT_LOG_MAX
    assert controller.events[0].name == "n10"
    controller.apply_frame(request_frame("r1"))
    controller.reset()
    assert controller.pending == {}
    assert len(controller.events) == consent.EVENT_LOG_MAX


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


def test_countdowns_follow_the_frames_deadline() -> None:
    """The frame's expires_at (the daemon's settings-driven
    deadline) wins over the local hold_timeout fallback."""
    clock = [150.0]
    controller = ConsentController(hold_timeout=120.0, clock=lambda: clock[0])
    shorter = json.loads(request_frame("deadline"))
    shorter["data"]["request"]["expires_at"] = 180.0
    controller.apply_frame(json.dumps(shorter))
    assert controller.remaining(controller.ordered()[0]) == pytest.approx(
        30.0, abs=0.01
    )  # 180 - 150, not requested_at + 120


def test_decider_rejection_frame() -> None:
    """An egress.decider_rejected frame reports the reason (the app
    exits on it instead of waiting forever)."""
    controller = ConsentController()
    outcome, payload = controller.apply_frame(
        json.dumps(
            {
                "event": "egress.decider_rejected",
                "data": {"reason": "unknown workspace"},
            }
        )
    )
    assert outcome == REJECTED
    assert payload == "unknown workspace"
    # A junk arm stays ignored.
    assert controller.apply_frame(
        json.dumps({"event": "egress.decider_rejected", "data": "junk"})
    ) == (REJECTED, None)


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


def test_foreign_workspace_frames_are_ignored() -> None:
    """A controller that owns a workspace drops another workspace's
    request and rules frames (#280 review): the hub broadcasts to
    every subscriber, and a foreign snapshot must neither plant a
    ghost hold nor repaint this decider's view."""
    controller = consent.ConsentController(workspace_id="ws-mine")
    assert (
        controller.apply_frame(request_frame("r1", workspace="ws-mine"))[0]
        == consent.ADDED
    )
    assert (
        controller.apply_frame(request_frame("r2", workspace="ws-other"))[0]
        == consent.IGNORED
    )
    assert "r2" not in controller.pending
    assert controller.apply_frame(rules_frame(workspace="ws-mine"))[0] == (
        consent.RULES
    )
    assert controller.apply_frame(rules_frame(workspace="ws-other"))[0] == (
        consent.IGNORED
    )
    assert controller.rules.workspace_id == "ws-mine"
    # The empty own-id (the protocol default) accepts every frame.
    bare = consent.ConsentController()
    assert bare.apply_frame(request_frame("r3", workspace="anywhere"))[0] == (
        consent.ADDED
    )


def test_first_rules_frame_adopts_resolved_workspace_id() -> None:
    """The TUI starts with the CLI name (``bar``); the server
    resolves it to the row's immutable id (``95c55d47d1``) and
    sends all frames keyed on that id. The first rules frame
    adopts the resolved id so later request frames pass the
    ``owns()`` check instead of being silently dropped (#297)."""
    controller = consent.ConsentController(workspace_id="bar")
    # The server sends the rules frame with the resolved id.
    outcome, rules = controller.apply_frame(
        rules_frame(workspace="95c55d47d1")
    )
    assert outcome == consent.RULES
    assert controller.workspace_id == "95c55d47d1"
    # Subsequent request frames with the resolved id are accepted.
    outcome, _ = controller.apply_frame(
        request_frame("r1", workspace="95c55d47d1")
    )
    assert outcome == consent.ADDED
    assert "r1" in controller.pending
    # A truly foreign workspace is still rejected after adoption.
    outcome, _ = controller.apply_frame(rules_frame(workspace="ws-foreign"))
    assert outcome == consent.IGNORED
