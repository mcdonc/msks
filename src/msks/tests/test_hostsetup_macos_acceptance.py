"""The macOS acceptance harness (#740): the setup tool's phases
run for real on a disposable macOS host — a GitHub Actions runner
— against a local fake msksd, real launchd, the real brew prefix,
and the real client config tree.

The harness fakes exactly one seam: ``vm_shell`` (``limactl
shell``), the tool's only VM coupling. Everything else executes
the shipped code against the host's real facilities, so a green
run is the release confidence the Linux CI cannot give the
macOS surface: nobody hand-verifies it between releases.

Gated twice: darwin only, and ``MSKS_MACOS_ACCEPTANCE=1`` — the
harness loads a real LaunchAgent and writes into the real brew
prefix, so it must never run in a plain developer checkout. The
module is self-contained (no imports from the sibling unit-test
tree): the release workflow copies it out of the checkout and
runs it against the installed wheel, where the checkout's tree
must not shadow the code under test.

The LaunchAgent test runs under its own label, never the real
tool's: a disposable host cannot have a real agent under
``com.msks.msksd-vm``, and a developer machine that does stays
untouched. The brew-prefix link test stands down the same way
when any file already holds the path.
"""

import http.server
import json
import os
import plistlib
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from msks.client.config import parse_config_doc
from msks.server import hostsetup_macos as mac
from msks.server.tls import load_or_generate

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "darwin",
        reason="the acceptance harness runs on macOS",
    ),
    pytest.mark.skipif(
        os.environ.get("MSKS_MACOS_ACCEPTANCE") != "1",
        reason="set MSKS_MACOS_ACCEPTANCE=1 on a disposable macOS "
        "host (loads a LaunchAgent, writes the brew prefix)",
    ),
]

#: The harness's own launchd label: the real tool's label stays
#: untouched on any host that carries a real agent.
AGENT_TEST_LABEL = "com.msks.acceptance"


class HealthHandler(http.server.BaseHTTPRequestHandler):
    """The daemon's public surface: /api/v1/health answers the
    version word; anything else answers 401 (any HTTP answer
    proves the listener, the verify pass's own rule)."""

    def do_GET(self) -> None:
        if self.path == "/api/v1/health":
            body = json.dumps(
                {"status": "ok", "version": mac.vm_version()}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(401)
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass  # keep the runner's log readable


class FakeDaemon:
    """A local msksd stand-in: the daemon's real TLS shape — a
    self-signed CA and leaf minted by the daemon's own code — on
    a random loopback port, plus the fake VM state the vm_shell
    seam serves (the CA file and the bootstrap token beside it)."""

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True)
        cert, key, _fp = load_or_generate(state_dir, "127.0.0.1", None, None)
        self.state_dir = state_dir
        self.ca_pem = (state_dir / "msks-ca.pem").read_text()
        (state_dir / "bootstrap-token").write_text("acceptance-token\n")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), HealthHandler
        )
        self.server.socket = context.wrap_socket(
            self.server.socket, server_side=True
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )


@pytest.fixture
def daemon(tmp_path: Path) -> FakeDaemon:
    """One fake msksd per test, started and stopped around it."""
    fake = FakeDaemon(tmp_path / "daemon-state")
    fake.thread.start()
    try:
        yield fake
    finally:
        fake.server.shutdown()
        fake.server.server_close()


@pytest.fixture
def client_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The client's config tree and the agent's paths, relocated:
    the harness writes real files, never the host's own."""
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mac, "MAC_CA", tmp_path / "config" / "ca.pem")
    monkeypatch.setattr(mac, "MAC_TOKEN", tmp_path / "config" / "vm.token")
    monkeypatch.setattr(
        mac, "AGENT_PLIST", tmp_path / "LaunchAgents" / "agent.plist"
    )
    monkeypatch.setattr(mac, "AGENT_LOG", tmp_path / "Logs" / "agent.log")
    return tmp_path


@pytest.fixture
def vm_seam(monkeypatch: pytest.MonkeyPatch, daemon: FakeDaemon) -> FakeDaemon:
    """The one faked seam: vm_shell answers the tool's cat probes
    from the fake daemon's state, the way the VM's own files
    would."""

    def fake_vm_shell(script: str) -> str:
        if "msks-ca.pem" in script:
            return (daemon.state_dir / "msks-ca.pem").read_text()
        if "bootstrap-token" in script:
            return "acceptance-token\n"
        return ""

    monkeypatch.setattr(mac, "vm_shell", fake_vm_shell)
    return daemon


# --- the runner itself ------------------------------------------------


def test_the_chip_gate_answers_on_real_hardware() -> None:
    """#676's gate against the host's real chip word: an M1/M2
    class chip refuses (the runner's own shape, and the reason a
    full setup run can never happen there), anything else passes
    — both arms are the pinned behavior, read from the real
    sysctl, not a mock."""
    brand = subprocess.run(
        ["sysctl", "-n", "machdep.cpu.brand_string"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if brand.startswith(("Apple M1", "Apple M2")):
        with pytest.raises(mac.MacSetupError, match="nested"):
            mac.check_chip_class()
    else:
        mac.check_chip_class()


@pytest.mark.skipif(
    shutil.which("limactl") is not None,
    reason="limactl is installed — the stock-runner arm needs its absence",
)
def test_a_stock_host_names_the_install_remedy() -> None:
    """The check pass on a host without Lima: the real PATH, the
    real refusal — the remedy names the tool, never an install
    (the check pass mutates nothing)."""
    with pytest.raises(mac.MacSetupError, match="run the tool"):
        mac.require_lima(install=False)


@pytest.mark.skipif(
    shutil.which("limactl") is None,
    reason="brew install lima first (the weekly drift job)",
)
def test_lima_parsing_against_the_real_binary() -> None:
    """The drift probe (#740): the version word, the floor, and
    the listing schema read from the real limactl the drift job
    just installed — an upstream rename of the fields this tool
    reads (name, status) surfaces here, weekly, instead of in a
    user's terminal. A host with a real msksd VM (a dev machine)
    reads its entry back; a stock runner reads absence."""
    word = mac.lima_version_output()
    assert word
    assert mac.lima_version_tuple(word) >= mac.LIMA_FLOOR
    listing = mac.run(["limactl", "list", "--json"], capture=True)
    entries = mac.listing_entries(listing)
    assert all("name" in entry and "status" in entry for entry in entries)
    entry = mac.vm_entry(mac.VM_NAME)
    if entry is not None:
        assert entry["status"]


# --- the verify pass --------------------------------------------------


def test_the_verify_pass_holds_against_a_real_listener(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    """Real TLS end to end: the any-answer probe, the chain
    proof, and the version word — and the negative arm: a CA
    from another daemon fails the chain and maps to the --port
    remedy (a real handshake refusal, not a mock)."""
    mac.verify_daemon(daemon.port)
    mac.verify_chain(daemon.port, daemon.ca_pem)
    assert mac.daemon_version(daemon.port) == mac.vm_version()
    other = FakeDaemon(tmp_path / "other-state")
    with pytest.raises(mac.MacSetupError, match="--port"):
        mac.verify_chain(daemon.port, other.ca_pem)


def test_client_preset_lands_the_real_client_files(
    daemon: FakeDaemon, vm_seam: FakeDaemon, client_tree: Path
) -> None:
    """The whole preset pass against the fake daemon: the CA,
    the token, and the config land with the house modes, the
    exports name them, the config loads under the client's own
    parser, and a second run is the idempotent no-op."""
    preset = mac.client_preset(daemon.port)
    assert f"export MSKSC_URL=https://127.0.0.1:{daemon.port}" in preset
    assert f"export MSKSC_CAFILE={mac.MAC_CA}" in preset
    assert "export MSKSC_TOKEN=acceptance-token" in preset
    assert mac.MAC_CA.read_text() == daemon.ca_pem.strip() + "\n"
    assert mac.MAC_CA.stat().st_mode & 0o777 == 0o600
    assert mac.MAC_TOKEN.read_text() == "acceptance-token\n"
    assert mac.MAC_TOKEN.stat().st_mode & 0o777 == 0o600
    config = Path(mac.default_config_path())
    assert config.read_text() == mac.client_config_body(daemon.port)
    doc = parse_config_doc(config.read_text(), str(config))
    entry = doc["daemons"][mac.MANAGED_ALIAS]
    assert entry["url"] == f"https://127.0.0.1:{daemon.port}"
    assert entry["token_file"] == str(mac.MAC_TOKEN)
    assert entry["cafile"] == str(mac.MAC_CA)
    assert doc["active_daemon"] == mac.MANAGED_ALIAS
    mac.client_preset(daemon.port)  # the rerun keeps every file


# --- the login agent and the client link -------------------------------


def test_the_login_agent_loads_and_unloads_through_launchd(
    monkeypatch: pytest.MonkeyPatch, client_tree: Path
) -> None:
    """The agent pass through the real launchd: the plist is
    written, the label bootstraps into the runner's gui domain,
    kickstart runs the job, and the uninstall mirror boots it
    out again. The program is /usr/bin/true — limactl's slot in
    the plist, held by a command the runner carries; the label is
    the harness's own, never the real tool's."""
    monkeypatch.setattr(mac, "limactl_path", lambda: "/usr/bin/true")
    monkeypatch.setattr(mac, "AGENT_LABEL", AGENT_TEST_LABEL)
    mac.ensure_agent()
    try:
        assert mac.agent_loaded()
        doc = plistlib.loads(mac.AGENT_PLIST.read_bytes())
        assert doc["Label"] == AGENT_TEST_LABEL
        assert doc["ProgramArguments"] == [
            "/usr/bin/true",
            "start",
            mac.VM_NAME,
        ]
    finally:
        mac.remove_agent()
    assert not mac.agent_loaded()
    assert not mac.AGENT_PLIST.exists()


@pytest.mark.skipif(
    shutil.which("brew") is None,
    reason="the harness needs the brew prefix",
)
def test_the_client_links_claim_the_brew_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The link pass against the real brew prefix (#719, #734):
    both client commands claim their path, the check row reads
    them back green, and the uninstall sweep removes them — and
    the harness stands down when any file already holds a path
    (a real deployment's links are never touched)."""
    for command in mac.CLIENT_COMMANDS:
        (tmp_path / command).write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        mac,
        "client_sibling",
        lambda command: str(tmp_path / command),
    )
    paths = {mac.client_link_path(c) for c in mac.CLIENT_COMMANDS}
    held = [p for p in paths if Path(p).exists()]
    if held:
        pytest.skip(
            f"{held} already exist — the harness never touches "
            "files that hold the link paths"
        )
    try:
        note = mac.link_client_mac()
        for path in paths:
            assert path in note
        row = mac.client_link_row()
        for command in mac.CLIENT_COMMANDS:
            assert f"{command} answers at " in row
        for path in paths:
            link = Path(path)
            assert link.is_symlink() and link.exists()
    finally:
        assert "are gone" in mac.remove_client_link()
    for path in paths:
        assert not Path(path).exists()
