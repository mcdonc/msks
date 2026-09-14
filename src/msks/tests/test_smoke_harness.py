"""Unit pins for the smoke harness's console self-heal (#75).

The smoke suite needs a real KVM guest and is opt-in; these exercise
its session-retry logic against fakes, so a regression that turns one
wedged console session back into a failed run (the smoke-kvm flake)
fails in every environment, not just on a slow CI runner.
"""

import asyncio

import pytest
import test_smoke


class FakeWriter:
    """The writer surface run_in_console touches."""

    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def wedged_session() -> tuple:
    """A session that carries nothing — the pty behind it echoes, but
    the shell never reaches its prompt (#75)."""
    return asyncio.StreamReader(), FakeWriter()


def live_session(prompt: bytes, reply: bytes) -> tuple:
    """A session that prompts now and answers the command once it is
    drained — the echoing-pty ordering run_in_console rides on."""
    reader = asyncio.StreamReader()
    reader.feed_data(prompt)

    class EchoWriter(FakeWriter):
        async def drain(self) -> None:
            reader.feed_data(self.written + b"\r\n" + reply + prompt)

    return reader, EchoWriter()


class FakeMicrovm:
    """``console()`` hands out the sessions in order."""

    def __init__(self, sessions: list) -> None:
        self.sessions = list(sessions)
        self.opened = 0

    async def console(self, workspace_id: str):
        self.opened += 1
        return self.sessions.pop(0)


async def test_wedged_session_gets_a_fresh_one(monkeypatch, capsys) -> None:
    # The prompt stall must burn the (shortened) timeout, not skip it.
    monkeypatch.setattr(test_smoke, "CONSOLE_TIMEOUT_S", 0.1)
    live = live_session(test_smoke.PROMPT_NEEDLE, b"hi\r\n")
    microvm = FakeMicrovm([wedged_session(), live])
    await test_smoke.run_in_console(microvm, "wid", "echo hi", "hi")
    assert microvm.opened == 2
    assert live[1].written == b"echo hi\n"
    # The abandoned session leaves its story in the log.
    assert "1/3" in capsys.readouterr().out


async def test_all_sessions_wedged_names_the_count(monkeypatch) -> None:
    monkeypatch.setattr(test_smoke, "CONSOLE_TIMEOUT_S", 0.1)
    microvm = FakeMicrovm(
        [wedged_session() for _ in range(test_smoke.CONSOLE_ATTEMPTS)]
    )
    with pytest.raises(AssertionError, match="never arrived within 3 console"):
        await test_smoke.run_in_console(microvm, "wid", "echo hi", "hi")
    assert microvm.opened == test_smoke.CONSOLE_ATTEMPTS


async def test_stalled_read_until_names_needle_and_timeout(monkeypatch) -> None:
    monkeypatch.setattr(test_smoke, "CONSOLE_TIMEOUT_S", 0.1)
    reader, _ = wedged_session()
    with pytest.raises(AssertionError, match=r"never saw b'needle' within 0.1s"):
        await test_smoke.read_until(reader, b"needle")
