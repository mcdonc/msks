"""The macOS host setup (#676): the tool that takes a Mac to a
running msksd-VM.

Covers the host check's refusals (the platform, the Silicon, the
Lima floor with its remedies), the instance config (the shipped
template plus the port forward this tool owns), the bootstrap
script's contract with the config and unit it writes, the token's
survival across reruns, the verify pass's any-answer and
chain-proof shapes, the preset's three exports, the check rows'
refusals, and the two subprocess seams' failure tails — against
one fake at ``subprocess.run`` (the whole tool is subprocesses;
the fakes answer at that boundary so every real body runs).
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from msks.server import hostsetup_macos as mac

# --- the one fake: subprocess.run -------------------------------------


class FakeProcesses:
    """Answers at the subprocess boundary: a table of command
    prefixes to outputs, a table of commands that fail, and a
    table of in-VM scripts to outputs (vm_shell sends the script
    on stdin)."""

    def __init__(self):
        self.listing = '[{"name":"msksd","status":"Running"}]'
        self.version = "limactl version 2.2.1"
        self.fail: list[str] = []
        self.fail_output: dict = {}
        self.vm: dict[str, str] = {}
        self.vm_fail: str | None = None
        self.calls: list[list[str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeProcesses:
        monkeypatch.setattr(mac.subprocess, "run", self._run)
        return self

    def _run(self, command, **_kwargs):
        self.calls.append(command)
        script = _kwargs.get("input")
        if script is not None:
            if self.vm_fail is not None and self.vm_fail in script:
                return completed(1, "", "boom: the VM said no")
            for needle, out in self.vm.items():
                if needle in script:
                    return completed(0, out, "")
            return completed(0, "", "")
        for bad, output in self.fail_output.items():
            if bad in " ".join(command):
                return completed(1, "", output)
        if command[:2] == ["limactl", "--version"]:
            return completed(0, self.version + "\n", "")
        if command[:2] == ["limactl", "list"]:
            return completed(0, self.listing, "")
        return completed(0, "", "")


def completed(rc: int, out: str, err: str) -> subprocess.CompletedProcess:
    """The shape run() and vm_shell() read."""
    return subprocess.CompletedProcess([], rc, out, err)


@pytest.fixture
def procs(monkeypatch: pytest.MonkeyPatch) -> FakeProcesses:
    fake = FakeProcesses()
    fake.install(monkeypatch)
    fake.vm = {
        "msks-ca.pem": "-----BEGIN CERTIFICATE-----\nxyz\n"
        "-----END CERTIFICATE-----\n",
        "bootstrap-token": "the-token\n",
        "nVHE|nv: [0-9]+ coarse": (
            "kvm [1]: nv: 554 coarse grained trap handlers\n"
        ),
        "dpkg -s virtiofsd": "present\n",
    }
    return fake


@pytest.fixture
def client_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The client's config tree and the agent's paths, relocated:
    the entry paths write these, and a test run must never reach
    the operator's real files."""
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mac, "MAC_CA", tmp_path / "config" / "ca.pem")
    monkeypatch.setattr(mac, "MAC_TOKEN", tmp_path / "config" / "vm.token")
    monkeypatch.setattr(
        mac, "AGENT_PLIST", tmp_path / "LaunchAgents" / "agent.plist"
    )
    monkeypatch.setattr(mac, "AGENT_LOG", tmp_path / "Logs" / "agent.log")
    return tmp_path


# --- the host check ---------------------------------------------------


def test_the_platform_refusals(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses
) -> None:
    monkeypatch.setattr(mac.sys, "platform", "linux")
    with pytest.raises(mac.MacSetupError, match="runs on macOS"):
        mac.check_host()
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine="x86_64")
    )
    with pytest.raises(mac.MacSetupError, match="Apple Silicon"):
        mac.check_host()


def test_a_missing_lima_names_the_brew_line(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses
) -> None:
    procs.fail_output = {"limactl --version": "not found"}
    with pytest.raises(mac.MacSetupError, match="brew install lima"):
        mac.require_lima()


@pytest.mark.parametrize(
    ("version", "refuses"),
    [
        ("2.2.1", False),
        ("2.3.0", False),
        ("2.1.0", True),
    ],
)
def test_the_lima_floor(
    procs: FakeProcesses, version: str, refuses: bool
) -> None:
    procs.version = f"limactl version {version}"
    if refuses:
        with pytest.raises(mac.MacSetupError, match="upgrade"):
            mac.require_lima()
    else:
        assert mac.require_lima() == version


def test_a_failed_run_without_a_tail_still_names_the_command(
    procs: FakeProcesses,
) -> None:
    """A failing command whose stderr is empty (or runs uncaptured)
    refuses naming the command alone — the no-tail arm."""
    procs.fail_output = {"limactl list": ""}
    with pytest.raises(mac.MacSetupError, match="`limactl list"):
        mac.run(["limactl", "list", "--json"], capture=True)


def test_a_failed_run_carries_the_tail(procs: FakeProcesses) -> None:
    procs.fail_output = {"limactl list": "the daemon is angry"}
    with pytest.raises(mac.MacSetupError, match="the daemon is angry"):
        mac.run(["limactl", "list", "--json"], capture=True)


def test_a_failed_vm_command_carries_its_tail(
    procs: FakeProcesses,
) -> None:
    procs.vm_fail = "apt-get"
    with pytest.raises(mac.MacSetupError, match="the VM said no"):
        mac.vm_shell("apt-get install nothing")


# --- the instance config ----------------------------------------------


def test_the_instance_config_appends_the_forward() -> None:
    """The shipped template rides whole — the share roots under
    the reserved prefix, vz, nested virtualization — and the one
    piece this tool owns appends: the port forward, both
    loopbacks, the course VM's verified shape."""
    document = mac.instance_yaml(mac.PORT)
    assert "mountPoint: /mnt/msks/Users/{{.User}}" in document
    assert "nestedVirtualization: true" in document
    assert f"guestPort: {mac.VM_PORT}" in document
    assert document.rstrip().endswith(
        f'hostPort: {mac.PORT}\n  hostIP: "127.0.0.1"'
    )


def test_the_vm_is_created_started_or_left_alone(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses
) -> None:
    for listing, expect_start in (
        ('[{"name":"other"}]', "creating"),
        ('[{"name":"msksd","status":"Stopped"}]', "starting"),
        ('[{"name":"msksd","status":"Running"}]', None),
    ):
        procs.calls.clear()
        procs.listing = listing
        monkeypatch.setattr(mac, "say", lambda _message: None)
        monkeypatch.setattr(mac, "forward_host_port", lambda name: None)
        mac.ensure_vm()
        starts = [c for c in procs.calls if c[1] == "start"]
        if expect_start is None:
            assert not starts
        else:
            assert starts


# --- the bootstrap ----------------------------------------------------


def test_the_bootstrap_writes_the_geometry_and_survives_reruns() -> None:
    """The script installs the wheel, writes the config with the
    prefix-mode geometry (the template's reserved prefix, the
    user's home the default allowed root), the unit with
    --config, and keeps the token file across runs — a rerun
    refreshes, never re-mints."""
    script = mac.bootstrap_script(None, "/Users/probe")
    assert "mount_host_mode: prefix" in script
    assert 'mount_host_roots: ["/Users/probe"]' in script
    assert f"port: {mac.VM_PORT}" in script
    assert 'socket: ""' in script
    assert "egress_enabled: true" in script
    # The daemon's tool set, its kernel routing setting, and the
    # pinned VMM — the pieces a workspace start execs or checks.
    assert "nftables iproute2 conntrack" in script
    assert "net.ipv4.ip_forward=1" in script
    assert mac.CH_SHA256 in script
    assert mac.CH_URL in script
    assert "bootstrap-token" in script
    assert "if [ ! -s" in script
    assert f"msks=={mac.vm_version()}" in script
    assert "systemctl restart msksd.service" in script
    assert f"--config {mac.VM_CONF}" in script


def test_a_named_wheel_installs_the_file() -> None:
    script = mac.bootstrap_script(
        "/builds/msks-0.5.0-py3-none-any.whl", "/Users/p"
    )
    assert "/tmp/msks-0.5.0-py3-none-any.whl" in script


def test_the_bootstrap_copies_a_named_wheel(procs: FakeProcesses) -> None:
    procs.vm = {}
    mac.bootstrap_vm("/builds/w.whl", "/Users/p")
    copied = [c for c in procs.calls if c[:2] == ["limactl", "copy"]]
    assert copied


# --- the verify pass and the preset -----------------------------------


def test_any_http_answer_proves_the_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 through the forward proves the forward, the
    listener, and the daemon's TLS at once — the urlopen raising
    an HTTPError carries a code and passes."""
    opened = []

    def fake_urlopen(url, timeout, context=None):
        opened.append(url)
        raise HTTPError(url, 401, "no token", None, None)

    monkeypatch.setattr(mac.urllib.request, "urlopen", fake_urlopen)
    mac.verify_daemon(mac.PORT, timeout_s=1.0)
    assert opened == [f"https://127.0.0.1:{mac.PORT}/"]


def test_listing_entries_skips_noise_lines() -> None:
    """The listing parser skips blank and non-JSON lines — the
    dmesg-grade noise a limactl burp can interleave."""
    entries = mac.listing_entries(
        ' {"name":"msksd","status":"Running"}\n'
        "\n"
        "not json at all\n"
        '[{"name":"other"}]'
    )
    assert entries == [
        {"name": "msksd", "status": "Running"},
        {"name": "other"},
    ]


def test_a_two_xx_answer_proves_the_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200-level answer returns on the urlopen return path."""
    monkeypatch.setattr(
        mac.urllib.request,
        "urlopen",
        lambda url, timeout, context=None: object(),
    )
    mac.verify_daemon(mac.PORT, timeout_s=1.0)


def test_a_retry_then_answer_proves_the_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first refused dial that answers on retry covers the
    loop's retry arm — the daemon coming up mid-verify."""
    attempts = []

    def flaky(url, timeout, context=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("refused")
        raise HTTPError(url, 401, "no token", None, None)

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(mac.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(mac.time, "sleep", fake_sleep)
    monkeypatch.setattr(mac.time, "monotonic", lambda: 0.0)
    mac.verify_daemon(mac.PORT, timeout_s=30.0)
    assert len(attempts) == 2 and sleeps


def test_the_chain_holds_on_a_verified_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrap-socket return path: a real context over a real
    local listener is CI-hostile, so the socket layers fake and
    the context is real over a self-owned CA pair."""
    import http.server
    import ssl as ssl_mod
    import tempfile
    import threading

    # A one-shot TLS server answering on a random loopback port,
    # self-signed through the same CA the check reads.
    ca_dir = tempfile.mkdtemp()
    from msks.server.tls import load_or_generate

    cert, key, _fp = load_or_generate(Path(ca_dir), "127.0.0.1", None, None)
    ca_pem = (Path(ca_dir) / "msks-ca.pem").read_text()
    context = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = http.server.HTTPServer(("127.0.0.1", 0), None)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mac.verify_chain(port, ca_pem)
    finally:
        server.shutdown()


def test_a_silent_listener_refuses_with_the_journal_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def silent(url, timeout, context=None):
        raise OSError("refused")

    monkeypatch.setattr(mac.urllib.request, "urlopen", silent)
    with pytest.raises(mac.MacSetupError, match="journalctl"):
        mac.verify_daemon(mac.PORT, timeout_s=0.0)


def test_a_chain_mismatch_names_the_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mac.ssl, "create_default_context", lambda **_kwargs: None
    )

    def fake_connect(address, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(mac.socket, "create_connection", fake_connect)
    with pytest.raises(mac.MacSetupError, match="--port"):
        mac.verify_chain(8660, "whatever")


def test_the_preset_carries_the_three_exports(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, client_tree: Path
) -> None:
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    preset = mac.client_preset(mac.PORT)
    assert "export MSKSC_URL=https://127.0.0.1:8660" in preset
    assert "export MSKSC_TOKEN=the-token" in preset
    assert f"export MSKSC_CAFILE={mac.MAC_CA}" in preset
    assert mac.MAC_CA.read_text().startswith("-----BEGIN")
    # The token file and the config beside it: the pair a bare
    # msks reads (the exports remain the copy-paste fallback).
    assert mac.MAC_TOKEN.read_text() == "the-token\n"
    assert mac.MAC_TOKEN.stat().st_mode & 0o777 == 0o600
    config = Path(mac.default_config_path())
    assert config.read_text() == mac.client_config_body(mac.PORT)
    from msks.client.config import parse_config_doc

    doc = parse_config_doc(config.read_text(), str(config))
    entry = doc["daemons"][mac.MANAGED_ALIAS]
    assert entry["url"] == "https://127.0.0.1:8660"
    assert entry["token_file"] == str(mac.MAC_TOKEN)
    assert entry["cafile"] == str(mac.MAC_CA)
    assert doc["active_daemon"] == mac.MANAGED_ALIAS


def test_a_missing_ca_retries_then_refuses(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses
) -> None:
    procs.vm = {"msks-ca.pem": ""}
    monkeypatch.setattr(mac, "MAC_CA", mac.MAC_CA)
    with pytest.raises(mac.MacSetupError, match="CA did not come back"):
        mac.client_preset(mac.PORT)


# --- the check rows ---------------------------------------------------


def test_the_check_rows_answer_the_doctor_questions(
    monkeypatch: pytest.MonkeyPatch,
    procs: FakeProcesses,
    client_tree: Path,
) -> None:
    monkeypatch.setattr(
        mac, "verify_daemon", lambda port, timeout_s=15.0: None
    )
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    mac.ensure_client_config(mac.PORT)
    rows = mac.check_rows(mac.PORT)
    assert rows[0] == "lima 2.2.1 at the template's floor"
    assert any("nested virtualization is on" in row for row in rows)
    assert any("nv: 554 coarse" in row for row in rows)
    assert any("virtiofsd present" in row for row in rows)
    assert any("chained to the VM's CA" in row for row in rows)


def test_m1_and_m2_refuse_at_the_gate(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses
) -> None:
    """#676's gate: an M1 or M2 boots the daemon VM but its
    workspaces can never start — the tool refuses before
    provisioning; newer, unrecognized chips pass."""
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine="arm64")
    )
    for brand, refuses in (
        ("Apple M2 Pro", True),
        ("Apple M1", True),
        ("Apple M3", False),
        ("Apple M4 Max", False),
        ("Some Future Chip", False),
    ):
        procs.calls.clear()
        procs.vm_fail = None
        procs.fail_output = {}
        procs.version = "limactl version 2.2.1"

        def fake_sysctl(command, **kwargs):
            if command[:1] == ["sysctl"]:
                return completed(0, brand + "\n", "")
            return procs._run(command, **kwargs)

        monkeypatch.setattr(mac.subprocess, "run", fake_sysctl)
        if refuses:
            with pytest.raises(mac.MacSetupError, match="nested"):
                mac.check_host()
        else:
            mac.check_host()


def test_the_not_found_nested_row_reports_truthfully(
    monkeypatch: pytest.MonkeyPatch,
    procs: FakeProcesses,
    client_tree: Path,
) -> None:
    """A VM whose markers have not landed yet (kvm loads on
    demand) reports the not-found row and the pass continues."""
    procs.vm = {
        "msks-ca.pem": "-----BEGIN CERTIFICATE-----\nxyz\n"
        "-----END CERTIFICATE-----\n",
        "bootstrap-token": "the-token\n",
        "dpkg -s virtiofsd": "present\n",
    }
    monkeypatch.setattr(
        mac, "verify_daemon", lambda port, timeout_s=15.0: None
    )
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    mac.ensure_client_config(mac.PORT)
    rows = mac.check_rows(mac.PORT)
    assert any("marker not found" in row for row in rows)


def test_a_missing_virtiofsd_refuses_the_check(
    procs: FakeProcesses,
) -> None:
    """virtiofsd's absence is a refusal, not a degraded pass —
    shares are this VM's headline feature."""
    procs.vm["dpkg -s virtiofsd"] = "missing\n"
    with pytest.raises(mac.MacSetupError, match="re-run the tool"):
        mac.check_rows(mac.PORT)


def test_a_mismatched_chain_refuses_with_the_port_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listener that answers with another daemon's certificate
    (the port-collision case) refuses naming --port — the
    verification-error arm, distinct from a torn CA's parse
    failure."""
    import ssl as ssl_mod

    class VerifiedSocket:
        def __enter__(self):
            raise ssl_mod.SSLCertVerificationError("certificate verify failed")

        def __exit__(self, *exc):
            return False

    class RawSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        mac.ssl,
        "create_default_context",
        lambda **kwargs: SimpleNamespace(
            wrap_socket=lambda *a, **kw: VerifiedSocket()
        ),
    )

    class Connect:
        def __enter__(self):
            return RawSocket()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        mac.socket, "create_connection", lambda a, timeout=None: Connect()
    )
    with pytest.raises(mac.MacSetupError, match="--port"):
        mac.verify_chain(8660, "whatever")


def test_the_nested_marker_skips_blank_lines(
    procs: FakeProcesses,
) -> None:
    """A probe whose output is blank lines yields the empty
    marker — the loop's skip arm."""
    procs.vm = {"VHE": "\n   \n"}
    assert mac.nested_marker() == ""


def test_a_torn_ca_read_refuses_with_the_rerun_remedy() -> None:
    """A CA that will not parse (a torn read inside the daemon's
    write window) refuses by name, never a raw traceback."""
    with pytest.raises(mac.MacSetupError, match="re-run the tool"):
        mac.verify_chain(8660, "-----BEGIN CERTIFICATE-----\nMIIabc\n")


def test_a_missing_vm_names_the_tool(procs: FakeProcesses) -> None:
    procs.listing = "[]"
    with pytest.raises(mac.MacSetupError, match=f"run {mac.TOOL}"):
        mac.check_rows(mac.PORT)


def test_a_stopped_vm_names_the_start(procs: FakeProcesses) -> None:
    procs.listing = '[{"name":"msksd","status":"Stopped"}]'
    with pytest.raises(mac.MacSetupError, match="limactl start msksd"):
        mac.check_rows(mac.PORT)


# --- the entry ---------------------------------------------------------


def test_creation_writes_the_rendered_instance(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, tmp_path
) -> None:
    """The create branch: limactl start reads the rendered
    instance config (template plus this tool's forward) from a
    temp path."""
    monkeypatch.setattr(mac, "say", lambda _message: None)
    monkeypatch.setattr(mac.Path, "home", lambda: tmp_path)
    procs.listing = ""
    mac.ensure_vm(host_port=8662)
    starts = [c for c in procs.calls if c[:2] == ["limactl", "start"]]
    assert starts and len(starts[0]) == 5


def test_a_port_change_on_an_existing_vm_names_the_recreation(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, tmp_path
) -> None:
    """The forward is fixed at creation: a mid-life --port is a
    named refusal with the delete-and-recreate remedy, never a
    silent wrong port."""
    monkeypatch.setattr(mac, "say", lambda _message: None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mac.Path, "home", lambda: home)
    (home / ".lima" / mac.VM_NAME).mkdir(parents=True)
    (home / ".lima" / mac.VM_NAME / "lima.yaml").write_text(
        f"portForwards:\n- guestPort: {mac.VM_PORT}\n"
        '  guestIP: "127.0.0.1"\n  hostPort: 8661\n'
    )
    with pytest.raises(mac.MacSetupError, match="limactl delete -f"):
        mac.ensure_vm(host_port=8662)


def test_the_check_only_entry_prints_rows(
    monkeypatch: pytest.MonkeyPatch,
    procs: FakeProcesses,
    capsys,
    client_tree: Path,
) -> None:
    """--check runs the platform gate and the rows, prints them
    on stdout, mutates nothing."""
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine="arm64")
    )
    monkeypatch.setattr(
        mac.subprocess,
        "run",
        lambda command, **kwargs: (
            completed(0, "Apple M3\n", "")
            if command[:1] == ["sysctl"]
            else procs._run(command, **kwargs)
        ),
    )
    monkeypatch.setattr(
        mac, "verify_daemon", lambda port, timeout_s=15.0: None
    )
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    mac.ensure_client_config(8661)
    assert mac.main(["--check", "--port", "8661"]) == 0
    out = capsys.readouterr().out
    assert "  ok  " in out and "chained to the VM's CA" in out
    assert "a bare msks reaches the daemon" in out
    assert "login agent brings the msksd VM up at login" in out


def test_the_entry_prints_the_preset(
    monkeypatch: pytest.MonkeyPatch,
    procs: FakeProcesses,
    capsys,
    client_tree: Path,
) -> None:
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    monkeypatch.setattr(mac, "verify_daemon", lambda port: None)
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine="arm64")
    )
    monkeypatch.setattr(
        mac.shutil, "which", lambda name: "/opt/homebrew/bin/limactl"
    )
    # Path.home pinned: run_setup's home and the instance-config
    # read stay in the test tree (a dev Mac's real ~/.lima/msksd
    # would answer the port read and break the run's hermeticity).
    monkeypatch.setattr(mac.Path, "home", lambda: client_tree)
    assert mac.main(["--port", "8661"]) == 0
    out = capsys.readouterr().out
    assert "export MSKSC_URL=https://127.0.0.1:8661" in out
    assert "export MSKSC_TOKEN=the-token" in out
    # The config and the login agent the run installed beside the
    # preset: the file a bare msks reads, and the plist launchd
    # loads (both under the relocated tree, never the real home).
    assert Path(mac.default_config_path()).is_file()
    assert mac.AGENT_PLIST.is_file()
    bootstraps = [
        c for c in procs.calls if c[:2] == ["launchctl", "bootstrap"]
    ]
    assert bootstraps and str(mac.AGENT_PLIST) in bootstraps[0]


def test_the_uninstall_mirror(
    monkeypatch: pytest.MonkeyPatch,
    procs: FakeProcesses,
    capsys,
    client_tree: Path,
) -> None:
    monkeypatch.setattr(mac, "say", lambda _message: None)
    mac.AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    mac.AGENT_PLIST.write_bytes(b"stale plist")
    assert mac.main(["--uninstall"]) == 0
    assert any(c[:2] == ["limactl", "delete"] for c in procs.calls)
    assert any(c[:2] == ["launchctl", "bootout"] for c in procs.calls)
    assert not mac.AGENT_PLIST.exists()
    out = capsys.readouterr().out
    assert "Lima itself" in out and "login agent" in out


def test_a_named_refusal_exits_one(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, capsys
) -> None:
    monkeypatch.setattr(mac.sys, "platform", "linux")
    assert mac.main([]) == 1
    assert "runs on macOS" in capsys.readouterr().err


# --- phase 5: the client config and the login agent ---------------------


def test_the_config_write_policies(
    monkeypatch: pytest.MonkeyPatch, client_tree: Path
) -> None:
    """One policy per prior state: a missing file is written, the
    current body is a no-op, the client's own empty template is
    replaced (nothing was set; the client regenerates it on
    demand), this tool's file is refreshed, and an operator's own
    file is kept byte-for-byte — the run names the choice."""
    from msks.client.config import render_template

    path = Path(mac.default_config_path())
    body = mac.client_config_body(mac.PORT)
    assert mac.ensure_client_config(mac.PORT).startswith("wrote")
    assert path.read_text() == body
    assert "is current" in mac.ensure_client_config(mac.PORT)
    path.write_text(render_template())
    assert mac.ensure_client_config(mac.PORT).startswith("wrote")
    assert path.read_text() == body
    path.write_text(mac.client_config_body(8661))  # this tool's, stale
    assert mac.ensure_client_config(mac.PORT).startswith("refreshed")
    assert path.read_text() == body
    path.write_text("# the operator's own config\nurl: https://elsewhere\n")
    assert mac.ensure_client_config(mac.PORT).startswith("kept")
    assert "elsewhere" in path.read_text()


def test_the_config_file_is_private(client_tree: Path) -> None:
    mac.ensure_client_config(mac.PORT)
    mode = Path(mac.default_config_path()).stat().st_mode & 0o777
    assert mode == 0o600


def test_the_config_row_refusals(
    monkeypatch: pytest.MonkeyPatch, client_tree: Path
) -> None:
    path = Path(mac.default_config_path())
    with pytest.raises(mac.MacSetupError, match="does not exist"):
        mac.client_config_row(mac.PORT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("daemons: [not, a, mapping]\n")
    with pytest.raises(mac.MacSetupError, match="does not load"):
        mac.client_config_row(mac.PORT)
    # A green file pointed at another port: the wrong-daemon arm.
    path.unlink()
    mac.ensure_client_config(8661)
    with pytest.raises(mac.MacSetupError, match="does not point"):
        mac.client_config_row(mac.PORT)
    # An operator file whose active daemon names this forward with
    # the VM's token and CA passes — the alias name is theirs.
    mac.ensure_client_config(mac.PORT)
    body = Path(mac.default_config_path()).read_text()
    monkeypatch.setattr(mac, "MANAGED_ALIAS", "theirs")
    path.write_text(body.replace(mac.MANAGED_ALIAS, "theirs"))
    assert mac.client_config_row(mac.PORT)


def test_the_agent_plist_names_the_absolute_limactl(
    client_tree: Path,
) -> None:
    import plistlib

    doc = plistlib.loads(mac.agent_plist("/opt/homebrew/bin/limactl"))
    assert doc["Label"] == mac.AGENT_LABEL
    assert doc["ProgramArguments"] == [
        "/opt/homebrew/bin/limactl",
        "start",
        mac.VM_NAME,
    ]
    assert doc["RunAtLoad"] is True
    assert doc["StandardOutPath"] == str(mac.AGENT_LOG)
    assert doc["StandardErrorPath"] == str(mac.AGENT_LOG)


def test_ensure_agent_rewrites_reloads_and_creates_the_log_dir(
    monkeypatch: pytest.MonkeyPatch,
    procs: FakeProcesses,
    client_tree: Path,
) -> None:
    monkeypatch.setattr(mac, "say", lambda _message: None)
    monkeypatch.setattr(
        mac.shutil, "which", lambda name: "/opt/homebrew/bin/limactl"
    )
    mac.ensure_agent()
    mac.ensure_agent()  # idempotent: rewritten, booted out, reloaded
    assert mac.AGENT_PLIST.is_file()
    assert mac.AGENT_LOG.parent.is_dir()
    commands = [" ".join(c) for c in procs.calls]
    bootout = [i for i, c in enumerate(commands) if "bootout" in c]
    bootstraps = [i for i, c in enumerate(commands) if "bootstrap" in c]
    assert len(bootstraps) == 2
    # Each bootstrap is preceded by its bootout — the reload pair,
    # never a duplicate load of one label.
    assert len(bootout) == 2 and bootout[0] < bootstraps[0]
    assert bootout[1] < bootstraps[1]


def test_a_missing_limactl_refuses_the_agent(
    monkeypatch: pytest.MonkeyPatch, client_tree: Path
) -> None:
    monkeypatch.setattr(mac.shutil, "which", lambda name: None)
    with pytest.raises(mac.MacSetupError, match="brew install lima"):
        mac.ensure_agent()


def test_agent_loaded_reads_the_print_exit(
    procs: FakeProcesses,
) -> None:
    assert mac.agent_loaded() is True
    procs.fail_output = {"launchctl print": "not loaded"}
    assert mac.agent_loaded() is False


def test_the_check_refuses_when_the_agent_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
    procs: FakeProcesses,
    client_tree: Path,
) -> None:
    monkeypatch.setattr(
        mac, "verify_daemon", lambda port, timeout_s=15.0: None
    )
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    mac.ensure_client_config(mac.PORT)
    procs.fail_output = {"launchctl print": "not loaded"}
    with pytest.raises(mac.MacSetupError, match="re-run"):
        mac.check_rows(mac.PORT)
