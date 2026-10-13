"""The refused-bind registry's own arithmetic (#533): what a
record, a clear, and a forget leave behind."""

from msks.listenerfaults import ListenerFaults


def test_clear_keeps_the_services_that_still_fault() -> None:
    """One service's rebind clears only its own entry: a sibling
    whose bind still refuses keeps naming the workspace."""
    faults = ListenerFaults()
    faults.record("ws-a", "probe", 8644, "in use")
    faults.record("ws-a", "llm", 8770, "no route")
    assert faults.payload("ws-a") == {
        "llm": {"port": 8770, "error": "no route"},
        "probe": {"port": 8644, "error": "in use"},
    }
    faults.clear("ws-a", "probe")
    assert faults.payload("ws-a") == {
        "llm": {"port": 8770, "error": "no route"}
    }
    faults.clear("ws-a", "llm")
    assert faults.payload("ws-a") == {}


def test_record_keeps_the_latest_attempt() -> None:
    """A retried refusal replaces its own earlier error: the
    record names what an operator can still act on."""
    faults = ListenerFaults()
    faults.record("ws-a", "probe", 8644, "first refusal")
    faults.record("ws-a", "probe", 8645, "second refusal")
    assert faults.payload("ws-a") == {
        "probe": {"port": 8645, "error": "second refusal"}
    }


def test_forget_ends_the_whole_record() -> None:
    """Detach drops every service's entry at once — the endpoints
    are down because the workspace is — and forgetting a workspace
    with no record stays a no-op."""
    faults = ListenerFaults()
    faults.record("ws-a", "probe", 8644, "in use")
    faults.forget("ws-a")
    assert faults.payload("ws-a") == {}
    faults.forget("ws-a")
    assert faults.payload("ws-a") == {}
