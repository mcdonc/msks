"""The client half of the console challenge (#123): the exchange on
the wire, the signer resolution by where the key lives, and the
``msks console`` flow answering through the daemon's relay.
"""

import asyncio
import os
from pathlib import Path

import pytest
from msks.client import console, consoleauth, sshsig
from msks.identity import mint
from test_client_console import FakeWs

PEM, PUBLIC = mint("ed25519")
KEY = {"public_key": f"{PUBLIC} msksd:alpha", "private_key": PEM}


def challenge_line() -> bytes:
    return b"AUTH CHALLENGE " + b"0a" * 32 + b"\n"


async def test_exchange_passes_pre_challenge_guests_through() -> None:
    """A guest whose first bytes are the shell's own (pre-#123): the
    bytes pass through untouched, nothing is sent."""
    ws = FakeWs([b"root@ws:~# "])
    lead = await consoleauth.auth_exchange(ws, "alpha", "u", "t")
    assert lead == b"root@ws:~# "
    assert ws.sent == []


async def test_exchange_waits_then_passes_a_quiet_guest_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bytes in the challenge window is a pre-#123 guest too: the
    pump will speak whenever the shell does."""
    monkeypatch.setattr(consoleauth, "CHALLENGE_WINDOW_S", 0.05)
    ws = FakeWs([])
    assert await consoleauth.auth_exchange(ws, "alpha", "u", "t") == b""


async def test_exchange_answers_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Challenge → signature → AUTH OK → the shell's first bytes
    ride back to the caller."""
    ws = FakeWs([challenge_line(), b"AUTH OK\nroot@ws:~# "])
    monkeypatch.setattr(
        consoleauth,
        "workspace_signer",
        lambda *a, **k: _signer(),
    )
    lead = await consoleauth.auth_exchange(ws, "alpha", "u", "t")
    assert lead == b"root@ws:~# "
    assert len(ws.sent) == 1 and ws.sent[0].startswith(b"AUTH SIG ")
    assert ws.sent[0].endswith(b"\n")
    # The signature is a real SSHSIG body: it would satisfy the
    # guest's ssh-keygen.
    import base64

    body = base64.b64decode(ws.sent[0][len(b"AUTH SIG ") : -1].decode())
    assert body.startswith(b"SSHSIG\x00\x00\x00\x01")


async def _signer():
    return consoleauth.console_signer(PEM), KEY["public_key"]


async def test_exchange_refusal_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWs([challenge_line(), b"MSKS ERR auth\n"])
    monkeypatch.setattr(
        consoleauth, "workspace_signer", lambda *a, **k: _signer()
    )
    with pytest.raises(SystemExit, match="refused the console signature"):
        await consoleauth.auth_exchange(ws, "alpha", "u", "t")


async def test_exchange_rejects_an_unexpected_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything but AUTH OK or the refusal is a protocol break: one
    line, not a hang."""
    ws = FakeWs([challenge_line(), b"what?\n"])
    monkeypatch.setattr(
        consoleauth, "workspace_signer", lambda *a, **k: _signer()
    )
    with pytest.raises(SystemExit, match="unexpected auth reply"):
        await consoleauth.auth_exchange(ws, "alpha", "u", "t")


async def test_exchange_assembles_a_prefix_split_mid_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frames may split the challenge line INSIDE its prefix: the
    undecidable window keeps reading instead of misreading the
    fragment as the shell's first bytes."""
    ws = FakeWs(
        [
            b"AUTH CHAL",
            b"LENGE " + b"0a" * 32,
            b"\n",
            b"AUTH OK\nprompt",
        ]
    )
    monkeypatch.setattr(
        consoleauth, "workspace_signer", lambda *a, **k: _signer()
    )
    lead = await consoleauth.auth_exchange(ws, "alpha", "u", "t")
    assert lead == b"prompt"
    assert ws.sent and ws.sent[0].startswith(b"AUTH SIG ")


async def test_exchange_assembles_split_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frames may split the challenge and the reply mid-line: the
    exchange assembles whole lines before deciding."""
    nonce_hex = b"0a" * 32
    ws = FakeWs(
        [
            b"AUTH CHALLENGE " + nonce_hex[:20],
            nonce_hex[20:] + b"\n",
            b"AUTH O",
            # The OK's tail, the newline, and the shell's first bytes
            # in one frame: what follows the protocol's line rides
            # back as the lead for the caller's pump.
            b"K\nprompt",
        ]
    )
    monkeypatch.setattr(
        consoleauth, "workspace_signer", lambda *a, **k: _signer()
    )
    lead = await consoleauth.auth_exchange(ws, "alpha", "u", "t")
    assert lead == b"prompt"


async def test_signer_prefers_the_daemon_escrow() -> None:
    signer, public = consoleauth.signer_for_key(KEY, "alpha")
    assert public == KEY["public_key"]
    assert signer(b"nonce") == sshsig.sign_payload(
        PEM, b"nonce", "msks-console"
    )


async def test_signer_falls_back_to_the_client_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = tmp_path / "msks" / "alpha" / "identity"
    path.parent.mkdir(parents=True)
    path.write_text(PEM)
    no_escrow = {"public_key": KEY["public_key"], "private_key": None}
    signer, _public = consoleauth.signer_for_key(no_escrow, "alpha")
    assert signer(b"nonce").startswith("U1NIU0lH")  # b64 "SSHSIG"


async def test_signer_uses_the_operator_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No escrow, no data-root file, an agent with the key: the
    signer goes through the agent (msks never reads the half)."""
    from msks.client import agent

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sock = tmp_path / "agent.sock"
    private = agent.load_private(PEM)
    with agent.serve(private, "held") as served:
        os.symlink(served.server_address, sock)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
        no_escrow = {
            "public_key": f"{PUBLIC} msks-client:alpha",
            "private_key": None,
        }
        signer, _public = consoleauth.signer_for_key(no_escrow, "alpha")
        assert signer(b"nonce") == sshsig.sign_via_agent(
            str(sock), f"{PUBLIC} msks-client:alpha", b"nonce", "msks-console"
        )


async def test_signer_without_any_half_is_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    no_escrow = {"public_key": KEY["public_key"], "private_key": None}
    with pytest.raises(SystemExit):
        consoleauth.signer_for_key(no_escrow, "alpha")


async def test_console_flow_answers_the_challenge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run_shell`` (``msks console``) answers a challenge through
    the daemon's relay before its raw pump starts: the shell's first
    bytes land on stdout, the signature on the wire."""
    import sys

    from test_client_console import ConnectStub, FakeStdout, PipeStdin

    ws = FakeWs([challenge_line(), b"AUTH OK\nroot@ws:~# "])
    pipe = PipeStdin()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(console.websockets, "connect", ConnectStub(ws))
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(
        consoleauth, "workspace_signer", lambda *a, **k: _signer()
    )
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, lambda: pipe.feed(b"\x1d"))  # detach
    result = await asyncio.wait_for(
        console.run_shell("alpha", "u", "t", None), 5
    )
    assert result == 0
    assert ws.sent and ws.sent[0].startswith(b"AUTH SIG ")
    assert stdout.buffer.getvalue() == b"root@ws:~# "


async def test_workspace_signer_fetches_over_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetching wrapper serves the API-held record through the
    same resolution as the key dict."""
    seen = {}

    async def fake_fetch(
        url, token, workspace_id, transport=None, ssl_ctx=None
    ):
        seen["path"] = (url, token, workspace_id)
        return KEY

    monkeypatch.setattr(consoleauth, "fetch_ssh_key", fake_fetch)
    signer, public = await consoleauth.workspace_signer(
        "https://d", "tok", "alpha"
    )
    assert seen["path"] == ("https://d", "tok", "alpha")
    assert public == KEY["public_key"]
    assert signer(b"nonce") == sshsig.sign_payload(
        PEM, b"nonce", "msks-console"
    )


async def test_console_flow_with_empty_lead_and_pump_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A challenge answered with nothing after the OK writes no lead,
    and a close during the pump reports a clean end."""
    import sys

    from test_client_console import ConnectStub, FakeStdout, FakeWs, PipeStdin

    ws = FakeWs([challenge_line(), b"AUTH OK\n"])
    ws.recv = _closing_recv(ws)
    pipe = PipeStdin()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(console.websockets, "connect", ConnectStub(ws))
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(
        consoleauth, "workspace_signer", lambda *a, **k: _signer()
    )
    result = await asyncio.wait_for(
        console.run_shell("alpha", "u", "t", None), 5
    )
    assert result == 0
    assert stdout.buffer.getvalue() == b""


def _closing_recv(ws):
    """recv that plays the queued messages, then closes the session
    (the pump's close arm)."""

    async def recv():
        if ws._incoming:
            return ws._incoming.pop(0)
        close = console.websockets.Close(1000, "bye")
        raise console.websockets.ConnectionClosed(None, close)

    return recv


async def test_exchange_refuses_a_malformed_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A challenge whose body is not hex is a protocol break: one
    line, fail closed, no guessing at the nonce."""
    ws = FakeWs([b"AUTH CHALLENGE not-hex-at-all\n"])
    with pytest.raises(SystemExit, match="malformed challenge"):
        await consoleauth.auth_exchange(ws, "alpha", "u", "t")


def test_agent_signer_reports_a_dead_socket(tmp_path: Path) -> None:
    """A socket path that names no live agent is one line naming
    the recovery, not ConnectionRefusedError."""
    import socket

    dead = tmp_path / "dead.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(dead))
    listener.close()  # the path exists, the listener does not
    _, public = mint("ecdsa")
    signer = consoleauth.agent_signer(str(dead), public)
    with pytest.raises(SystemExit, match="not reachable"):
        signer(b"nonce")


async def test_open_session_normalizes_a_text_lead(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A text frame from the relay still lands as bytes on the tty."""
    import sys

    async def text_lead(*args, **kwargs):
        return "prompt"

    monkeypatch.setattr(console.consoleauth, "auth_exchange", text_lead)
    assert await console.open_session(None, "alpha", "u", "t", None)
    out = capsys.readouterr()
    assert sys.stdout.encoding and "prompt" in out.out


async def test_open_session_names_a_pre_challenge_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guest that refuses before any challenge (a broken trust
    store) reports the recovery, not a raw refusal line and a clean
    exit."""

    async def refused_lead(*args, **kwargs):
        return b"MSKS ERR auth\n"

    monkeypatch.setattr(console.consoleauth, "auth_exchange", refused_lead)
    with pytest.raises(SystemExit, match="trust store is broken"):
        await console.open_session(None, "alpha", "u", "t", None)


async def test_exchange_passes_a_text_lead_through() -> None:
    """A text frame from the relay rides back verbatim for the
    caller to normalize."""
    ws = FakeWs(["prompt"])
    assert await consoleauth.auth_exchange(ws, "alpha", "u", "t") == "prompt"
