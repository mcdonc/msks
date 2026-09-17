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


def live_session(prompt: bytes, reply: bytes | None) -> tuple:
    """A session that prompts now and — when ``reply`` is not None —
    answers the command once it is drained: the echoing-pty ordering
    run_in_console rides on. A None ``reply`` is the shell that took
    the command and never answered (the marker-phase stall)."""
    reader = asyncio.StreamReader()
    reader.feed_data(prompt)

    class EchoWriter(FakeWriter):
        async def drain(self) -> None:
            if reply is not None:
                reader.feed_data(self.written + b"\r\n" + reply + prompt)

    return reader, EchoWriter()


class FakeMicrovm:
    """``console()`` hands out the sessions in order."""

    def __init__(self, sessions: list) -> None:
        self.sessions = list(sessions)
        self.opened = 0

    async def console(
        self,
        workspace_id: str,
        user: str | None = None,
        rows: int = 0,
        cols: int = 0,
        term: str = "xterm",
    ):
        self.opened += 1
        return self.sessions.pop(0)


def pin_attempts_and_timeout(monkeypatch) -> None:
    """Run the retries against pinned values, not env-tunable ones —
    a CI setting MSKSD_TEST_CONSOLE_ATTEMPTS must not break the pins."""
    monkeypatch.setattr(test_smoke, "CONSOLE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(test_smoke, "CONSOLE_ATTEMPTS", 3)


async def test_wedged_session_gets_a_fresh_one(monkeypatch, capsys) -> None:
    # The prompt stall must burn the (shortened) timeout, not skip it.
    pin_attempts_and_timeout(monkeypatch)
    live = live_session(test_smoke.CONSOLE_PROMPT_NEEDLE, b"hi\r\n")
    microvm = FakeMicrovm([wedged_session(), live])
    await test_smoke.run_in_console(microvm, "wid", "echo hi", "hi")
    assert microvm.opened == 2
    assert live[1].written == b"echo hi\n"
    # The abandoned session leaves its story in the log.
    assert "1/3" in capsys.readouterr().out


async def test_marker_stall_also_gets_a_fresh_session(monkeypatch) -> None:
    # The command stalls after the prompt (the shell took it and
    # never answered): the retry re-runs it — idempotent by contract
    # — in a new session, which is what the docstring promises.
    pin_attempts_and_timeout(monkeypatch)
    stalled = live_session(test_smoke.CONSOLE_PROMPT_NEEDLE, reply=None)
    live = live_session(test_smoke.CONSOLE_PROMPT_NEEDLE, reply=b"hi\r\n")
    microvm = FakeMicrovm([stalled, live])
    await test_smoke.run_in_console(microvm, "wid", "echo hi", "hi")
    assert microvm.opened == 2
    # The command really ran twice: once into the stalled shell,
    # once into the fresh one.
    assert stalled[1].written == b"echo hi\n"
    assert live[1].written == b"echo hi\n"


async def test_all_sessions_wedged_names_the_count(monkeypatch) -> None:
    pin_attempts_and_timeout(monkeypatch)
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


class AnswerWriter(FakeWriter):
    """The writer surface answer_console_auth touches."""


def stream(chunks: list[bytes], eof: bool = False) -> asyncio.StreamReader:
    """A reader whose bytes arrive chunk by chunk, like the relay;
    ``eof`` ends the stream after them (a closed session)."""
    reader = asyncio.StreamReader()

    class Feed:
        def __init__(self) -> None:
            self.pending = list(chunks)

        async def run(self) -> None:
            for chunk in self.pending:
                await asyncio.sleep(0)
                reader.feed_data(chunk)
            if eof:
                reader.feed_eof()

    asyncio.get_running_loop().create_task(Feed().run())
    return reader


async def test_answer_passes_a_prompt_without_newline_through() -> None:
    """A no-challenge guest's first bytes are the bracketed-paste
    escape and a prompt — no newline anywhere. The detection is
    byte-wise, so these bytes go back for the prompt wait instead of
    stalling a line read (#123's smoke regression)."""
    prompt = b"\x1b[?2004hroot@msks-guest:~# "
    reader = stream([prompt])
    writer = AnswerWriter()
    await test_smoke.answer_console_auth(reader, writer, "ws", app=object())
    assert writer.written == b""
    assert await reader.read(4096) == prompt


async def test_answer_assembles_a_split_challenge_and_rest() -> None:
    """The challenge line may arrive split inside its prefix and
    carry the AUTH OK's tail behind it: the exchange assembles the
    line, answers, and puts the leftover back in order."""
    nonce = b"0a" * 32
    reader = stream(
        [
            b"AUTH CH",
            b"ALLENGE " + nonce[:20],
            nonce[20:] + b"\nAUTH O",
            b"K\nprompt",
        ]
    )
    writer = AnswerWriter()
    await test_smoke.answer_console_auth(
        reader, writer, "ws", app=object(), signer=lambda n: "c2ln"
    )
    assert writer.written == b"AUTH SIG c2ln\n"
    assert await reader.read(4096) == b"prompt"


async def test_answer_returns_quietly_at_eof() -> None:
    """A stream that ends before any decidable byte: nothing is
    sent, nothing is lost."""
    reader = stream([], eof=True)
    writer = AnswerWriter()
    await test_smoke.answer_console_auth(reader, writer, "ws", app=object())
    assert writer.written == b""


async def test_answer_feeds_diverging_shell_bytes_back() -> None:
    """Bytes that diverge from the prefix are the shell's own: they
    return to the stream whole, with whatever followed them."""
    reader = stream([b"ro", b"ot@msks-guest:~# "])
    writer = AnswerWriter()
    await test_smoke.answer_console_auth(reader, writer, "ws", app=object())
    assert writer.written == b""
    # The fed-back first chunk lands before the relay's second one.
    got = await reader.read(4096)
    got += await reader.read(4096)
    assert got == b"root@msks-guest:~# "
