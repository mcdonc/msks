"""The unlock-time plaintext cache (#514, #504): deadlines, the
zero-window session rule, the served window the unlock requires,
and the named locked error the swap path answers with."""

from datetime import UTC, datetime, timedelta

import pytest
from msks.secretcache import (
    NotEncryptableError,
    PlaintextCache,
    SecretLockedError,
    now_utc,
)


class FakeStoreSettings:
    """The live timeout the cache reads, mutable per test."""

    def __init__(self, timeout_s: float) -> None:
        self.unlock_timeout_s = timeout_s


class FakeSecretStore:
    def __init__(self, timeout_s: float) -> None:
        self.unlock_timeout_s = timeout_s


class FakeSettings:
    def __init__(self, timeout_s: float) -> None:
        self.secret_store = FakeSecretStore(timeout_s)


class FakeState:
    def __init__(self, timeout_s: float) -> None:
        self.settings = FakeSettings(timeout_s)


class FakeApp:
    def __init__(self, timeout_s: float) -> None:
        self.state = FakeState(timeout_s)


class Clock:
    """The test seam: a mutable now."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def cache(timeout_s: float = 900.0, clock: Clock | None = None) -> tuple:
    app = FakeApp(timeout_s)
    plain = PlaintextCache(app)
    plain._clock = clock if clock is not None else Clock()
    return plain, plain._clock


def test_store_and_read_answer_under_the_window() -> None:
    plain, clock = cache()
    plain.store({"MSKSWS_WS_X_TOKEN": "value"})
    assert plain.read("MSKSWS_WS_X_TOKEN") == "value"
    assert plain.knows("MSKSWS_WS_X_TOKEN")
    clock.advance(899)
    assert plain.read("MSKSWS_WS_X_TOKEN") == "value"


def test_a_cold_read_raises_the_named_locked_error() -> None:
    plain, _clock = cache()
    with pytest.raises(SecretLockedError):
        plain.read("MSKSWS_WS_X_TOKEN")


def test_expiry_drops_the_plaintext_on_read() -> None:
    plain, clock = cache()
    plain.store({"MSKSWS_WS_X_TOKEN": "value"})
    clock.advance(901)
    with pytest.raises(SecretLockedError):
        plain.read("MSKSWS_WS_X_TOKEN")
    assert not plain.knows("MSKSWS_WS_X_TOKEN")
    # The entry left the memory, not just the read: the values
    # dict no longer holds it.
    assert "MSKSWS_WS_X_TOKEN" not in plain._values


def test_flush_expired_retires_on_time_without_a_read() -> None:
    plain, clock = cache()
    plain.store({"A": "1", "B": "2"})
    plain.served([7, 8])
    clock.advance(901)
    assert plain.flush_expired() == 4
    assert plain._values == {}
    assert plain._served == {}
    # Nothing left to flush the second time around.
    assert plain.flush_expired() == 0


def test_the_unlock_resets_the_timer() -> None:
    """A second store() takes a fresh deadline: the unlock POST is
    the one event that resets the window."""
    plain, clock = cache()
    plain.store({"A": "1"})
    clock.advance(800)
    plain.store({"A": "1"})
    clock.advance(800)  # 1600 past the first, 0 past the second
    assert plain.read("A") == "1"


def test_zero_window_holds_only_while_an_operator_stands() -> None:
    plain, clock = cache(timeout_s=0.0)
    plain.operator_connected()
    plain.store({"A": "1"})
    plain.served([3])
    clock.advance(10_000)  # the clock never expires a session-held entry
    assert plain.read("A") == "1"
    assert plain.served_alive(3)
    plain.operator_disconnected()
    with pytest.raises(SecretLockedError):
        plain.read("A")
    assert not plain.served_alive(3)
    assert plain.flush_expired() == 0  # the drop already happened


def test_an_http_only_unlock_holds_nothing_past_the_reply() -> None:
    """A zero window's session is the websocket, not the POST
    (#514 review): with no operator socket connected, a
    session-held entry reads cold at once."""
    plain, _clock = cache(timeout_s=0.0)
    plain.store({"A": "1"})
    with pytest.raises(SecretLockedError):
        plain.read("A")


def test_the_last_disconnect_drops_the_session() -> None:
    """Two operators stand: the FIRST disconnect keeps the
    session (the second operator is still connected); the LAST
    drops it."""
    plain, _clock = cache(timeout_s=0.0)
    plain.operator_connected()
    plain.operator_connected()
    plain.store({"A": "1"})
    plain.operator_disconnected()
    assert plain.read("A") == "1"
    plain.operator_disconnected()
    with pytest.raises(SecretLockedError):
        plain.read("A")


def test_a_disconnect_at_zero_operators_is_inert() -> None:
    plain, _clock = cache(timeout_s=900.0)
    plain.store({"A": "1"})
    plain.operator_disconnected()  # a socket that never registered
    assert plain.read("A") == "1"


def test_a_disconnect_keeps_dated_entries() -> None:
    """A nonzero window belongs to the clock, not the socket: the
    operator's websocket closing does not shorten it."""
    plain, _clock = cache(timeout_s=900.0)
    plain.store({"A": "1"})
    plain.operator_disconnected()
    assert plain.read("A") == "1"


def test_the_timeout_is_read_live() -> None:
    plain, clock = cache(timeout_s=900.0)
    plain.store({"A": "1"})
    plain.app.state.settings.secret_store.unlock_timeout_s = 60.0
    plain.store({"B": "2"})
    clock.advance(61)
    assert not plain.knows("B")
    assert plain.knows("A")


def test_the_served_window_gates_and_drops() -> None:
    plain, clock = cache()
    assert not plain.served_alive(5)  # never served
    plain.served([5])
    assert plain.served_alive(5)
    clock.advance(901)
    assert not plain.served_alive(5)
    assert 5 not in plain._served  # the dead marker left with the check


def test_clear_and_forget() -> None:
    plain, _clock = cache()
    plain.store({"A": "1", "B": "2"})
    plain.served([1, 2])
    plain.forget(["A"], [1])
    with pytest.raises(SecretLockedError):
        plain.read("A")
    assert plain.read("B") == "2"
    assert not plain.served_alive(1)  # the marker left with the value
    assert plain.served_alive(2)
    plain.clear()
    with pytest.raises(SecretLockedError):
        plain.read("B")
    assert not plain.served_alive(2)


def test_not_encryptable_names_workspace_and_constraint() -> None:
    err = NotEncryptableError("ws-x", "its key type is not capable")
    assert "ws-x" in str(err)
    assert "its key type is not capable" in str(err)


def test_now_utc_answers_utc() -> None:
    assert now_utc().tzinfo == UTC
