"""The decider registry (#69)."""

from msks.consent.deciders import DeciderRegistry


def test_register_and_deregister() -> None:
    registry = DeciderRegistry()
    registry.register(1, "ws-a")
    assert registry.has_decider("ws-a")
    assert registry.watchers("ws-a") == 1
    assert not registry.has_decider("ws-b")
    registry.register(2, "ws-a")
    registry.register(2, "ws-b")
    assert registry.watchers("ws-a") == 2
    registry.deregister(2)  # its socket closed: all its watches end
    assert registry.has_decider("ws-a")
    assert not registry.has_decider("ws-b")
    registry.deregister(2)  # idempotent
    registry.deregister(1)
    assert not registry.has_decider("ws-a")


def test_watchers_counts_per_workspace() -> None:
    registry = DeciderRegistry()
    registry.register(7, "ws-x")
    registry.register(8, "ws-x")
    registry.register(9, "ws-y")
    assert registry.watchers("ws-x") == 2
    assert registry.watchers("ws-y") == 1
    assert registry.watchers("ws-z") == 0
