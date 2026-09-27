"""The interceptor CA's client half (#392): the trust marker, the
install command, the scripted console session, and the hand-run
recipe — unit-tested against fakes (the console session against a
scripted websocket, the marker against a relocated data root)."""

import asyncio

import pytest
import websockets
from msks.client import interceptor_ca
from msks.client.interceptor_ca import (
    CA_DEST,
    MARKER,
    ca_line,
    ca_trusted,
    install_ca,
    install_command,
    mark_ca_trusted,
    recipe_commands,
    recipe_lines,
    trusted_path,
)

PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
OTHER_PEM = PEM.replace("MIIB", "MIIC")


@pytest.fixture
def data_root(monkeypatch, tmp_path):
    """The client data root, relocated: the trust marker writes
    nowhere near the operator's real one. The connection env is
    pinned too — the session reads it when the caller does not."""
    monkeypatch.setenv("MSKSC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MSKSC_URL", "https://api.test")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    return tmp_path


class ScriptedConsole:
    """The console surface install_ca rides, scripted: the dial's
    websocket and the challenge exchange, both recorded."""

    def __init__(self, incoming: list | None = None, chatter=False) -> None:
        self.sent: list[bytes] = []
        self._incoming = list(incoming or [])
        self._chatter = chatter
        self.closed = False
        self.auth_calls: list[str] = []

    async def dial(self, address, token, ssl_ctx, url):
        self.dial_args = (address, token, ssl_ctx, url)
        return self

    async def auth_exchange(self, ws, workspace_id, url, token, ssl_ctx):
        self.auth_calls.append(workspace_id)
        return b""

    async def recv(self):
        if self._incoming:
            return self._incoming.pop(0)
        if self._chatter:
            return b"still printing\n"  # output that never carries the marker
        await asyncio.sleep(3600)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


def script_console(monkeypatch, incoming=None, **kw) -> ScriptedConsole:
    console = ScriptedConsole(incoming, **kw)
    monkeypatch.setattr(interceptor_ca, "dial", console.dial)
    monkeypatch.setattr(interceptor_ca, "auth_exchange", console.auth_exchange)
    return console


def test_the_trust_marker_names_the_ca_it_trusts(data_root) -> None:
    """The marker is client state carrying the installed PEM's
    digest: absent until an install records it, present after —
    and a re-minted CA (a different PEM) reads untrusted again, so
    the line cannot lie through a daemon-side re-mint."""
    assert ca_trusted("ws-a") is False
    mark_ca_trusted("ws-a", PEM)
    assert ca_trusted("ws-a", PEM) is True
    assert trusted_path("ws-a") == (
        data_root / "ws-a" / "interceptor-ca.trusted"
    )
    # Without the PEM beside it, the record stands alone: this
    # client installed some CA of the workspace's.
    assert ca_trusted("ws-a") is True
    # The daemon's current PEM differs (a re-mint): untrusted.
    assert ca_trusted("ws-a", OTHER_PEM) is False
    # A marker is per-workspace: one workspace's install says
    # nothing about another's guest.
    assert ca_trusted("ws-b", PEM) is False


def test_the_trust_line_names_both_states(data_root) -> None:
    """The line names the symptom while untrusted — the failure
    reads as the trust it is — and the plain state once trusted."""
    assert ca_line("ws-a", PEM) == (
        "interceptor CA: untrusted — HTTPS toward allowlisted "
        "destinations fails validation"
    )
    mark_ca_trusted("ws-a", PEM)
    assert ca_line("ws-a", PEM) == "interceptor CA: trusted"
    assert ca_line("ws-a") == "interceptor CA: trusted"  # the record alone
    assert ca_line("ws-a", OTHER_PEM).startswith("interceptor CA: untrusted")


def test_the_install_command_decodes_into_the_store() -> None:
    """The one line the guest runs: the CA's base64, the decode
    into the store directory, the update command — and a marker
    whose echoed form cannot satisfy the wait."""
    line = install_command(PEM)
    assert line.startswith("echo ")
    blob = line.split()[1]
    assert interceptor_ca.base64.b64decode(blob).decode() == PEM
    assert f"> {CA_DEST}" in line
    assert "update-ca-certificates" in line
    # The echo spells the marker quoted, so the pty's echo of the
    # command line — which precedes any output — never matches the
    # marker the finished run prints.
    assert MARKER not in line.encode()


async def test_the_session_waits_for_the_marker(
    monkeypatch, data_root
) -> None:
    """The session sends the install line and reads until the
    marker lands, whatever the guest prints around it — the pty's
    own echo of the command line included (it precedes the run and
    must not satisfy the wait), a text frame included (the relay
    normalizes nothing; bytes are bytes on a tty, and the wait
    encodes the same way)."""
    echoed = install_command(PEM).encode() + b"\r\n"  # the tty's echo
    console = script_console(
        monkeypatch,
        incoming=[echoed, b"Updating certificates", "1 added, 0 removed.\n"],
    )
    # The marker arrives last, as its own frame after the run.
    console._incoming.append(MARKER + b"\r\n")
    await install_ca("ws-a", PEM, ssl_ctx=None)
    assert console.auth_calls == ["ws-a"]
    assert console.sent == [install_command(PEM).encode() + b"\n"]
    assert console.closed is True
    # The trust record is the install's caller's job — the session
    # itself only runs the line.
    assert ca_trusted("ws-a", PEM) is False


async def test_the_session_names_a_stalled_guest(
    monkeypatch, data_root
) -> None:
    """A guest that never prints the marker is one named line, not
    a hang: the window bounds the wait."""
    monkeypatch.setattr(interceptor_ca, "INSTALL_TIMEOUT_S", 0.05)
    console = script_console(monkeypatch, incoming=[])
    with pytest.raises(SystemExit, match="did not finish the CA install"):
        await install_ca("ws-a", PEM, ssl_ctx=None)
    assert console.sent == [install_command(PEM).encode() + b"\n"]


async def test_a_chatty_guest_cannot_stretch_the_window(
    monkeypatch, data_root
) -> None:
    """The window is one deadline for the whole install, not a
    fresh budget per read: a console that keeps printing output
    without the marker still ends at the window's edge."""
    monkeypatch.setattr(interceptor_ca, "INSTALL_TIMEOUT_S", 0.1)
    script_console(monkeypatch, incoming=[], chatter=True)
    with pytest.raises(SystemExit, match="did not finish the CA install"):
        await install_ca("ws-a", PEM, ssl_ctx=None)


async def test_the_session_names_the_daemons_close(
    monkeypatch, data_root
) -> None:
    """A daemon close rides the console's close-code table: a
    stopped workspace names itself, not a traceback — and a clean
    close before the marker is its own named end."""
    close = websockets.Close(4501, "")

    class Refused(ScriptedConsole):
        async def recv(self):
            raise websockets.ConnectionClosed(close, None)

    console = Refused()
    monkeypatch.setattr(interceptor_ca, "dial", console.dial)
    monkeypatch.setattr(interceptor_ca, "auth_exchange", console.auth_exchange)
    with pytest.raises(SystemExit, match="console unavailable"):
        await install_ca("ws-a", PEM, ssl_ctx=None)

    class Ended(ScriptedConsole):
        async def recv(self):
            raise websockets.ConnectionClosed(
                None, websockets.Close(1000, "bye")
            )

    console = Ended()
    monkeypatch.setattr(interceptor_ca, "dial", console.dial)
    monkeypatch.setattr(interceptor_ca, "auth_exchange", console.auth_exchange)
    with pytest.raises(SystemExit, match="ended before the install"):
        await install_ca("ws-a", PEM, ssl_ctx=None)


async def test_the_session_names_a_guest_that_refused(
    monkeypatch, data_root
) -> None:
    """A guest that answers the challenge with MSKS ERR — its
    console trust store broken — refuses before the install runs."""
    console = script_console(monkeypatch)

    async def refused(ws, workspace_id, url, token, ssl_ctx):
        return b"MSKS ERR auth refused\n"

    monkeypatch.setattr(interceptor_ca, "auth_exchange", refused)
    with pytest.raises(SystemExit, match="refused the console session"):
        await install_ca("ws-a", PEM, ssl_ctx=None)
    assert console.sent == []  # nothing reached the guest


def test_the_recipes_commands_paste_whole() -> None:
    """The hand-run install as short whole shell lines: each line
    a complete command that fits the recipe panel's width (a copy
    across soft wraps carries whole commands), and the chunk chain
    assembles the CA exactly."""
    commands = recipe_commands(PEM)
    assert commands[-3:] == [
        "base64 -d /tmp/msks-ca.b64 > /tmp/msks-ws.crt",
        f"cp /tmp/msks-ws.crt {CA_DEST}",
        "update-ca-certificates",
    ]
    blob = ""
    for line in commands[:-3]:
        assert len(line) <= 58  # fits the 64-column panel's rows
        assert line.startswith(("printf %s ",))
        blob += line.split()[2]
    assert interceptor_ca.base64.b64decode(blob).decode() == PEM


def test_the_recipe_carries_the_real_path_and_commands() -> None:
    """The hand-run recipe names the daemon-side file the reply
    carried and the guest-side commands — the same install, spelled
    for the operator."""
    info = {"path": "/state/vms/ws-a/interceptor-ca.crt", "ca_pem": PEM}
    lines = recipe_lines(info, "ws-a")
    joined = "\n".join(lines)
    assert "/state/vms/ws-a/interceptor-ca.crt" in joined
    assert recipe_commands(PEM)[0] in joined
    assert "update-ca-certificates" in joined
    assert "root" in joined
    # A reply that carried no path still names the file's place.
    fallback = recipe_lines({"ca_pem": PEM}, "ws-a")
    assert "<state_dir>/vms/ws-a/interceptor-ca.crt" in "\n".join(fallback)
