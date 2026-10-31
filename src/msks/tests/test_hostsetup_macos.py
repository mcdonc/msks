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
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, tmp_path
) -> None:
    monkeypatch.setattr(mac, "MAC_CA", tmp_path / "ca.pem")
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    preset = mac.client_preset(mac.PORT)
    assert "export MSKSC_URL=https://127.0.0.1:8660" in preset
    assert "export MSKSC_TOKEN=the-token" in preset
    assert f"export MSKSC_CAFILE={tmp_path / 'ca.pem'}" in preset
    assert (tmp_path / "ca.pem").read_text().startswith("-----BEGIN")


def test_a_missing_ca_retries_then_refuses(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses
) -> None:
    procs.vm = {"msks-ca.pem": ""}
    monkeypatch.setattr(mac, "MAC_CA", mac.MAC_CA)
    with pytest.raises(mac.MacSetupError, match="CA did not come back"):
        mac.client_preset(mac.PORT)


# --- the check rows ---------------------------------------------------


def test_the_check_rows_answer_the_doctor_questions(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses
) -> None:
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
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


def test_a_missing_virtiofsd_refuses_the_check(
    procs: FakeProcesses,
) -> None:
    """virtiofsd's absence is a refusal, not a degraded pass —
    shares are this VM's headline feature."""
    procs.vm["dpkg -s virtiofsd"] = "missing\n"
    with pytest.raises(mac.MacSetupError, match="re-run the tool"):
        mac.check_rows(mac.PORT)


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
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, capsys
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
    assert mac.main(["--check", "--port", "8661"]) == 0
    out = capsys.readouterr().out
    assert "  ok  " in out and "chained to the VM's CA" in out


def test_the_entry_prints_the_preset(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, capsys
) -> None:
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine="arm64")
    )
    assert mac.main(["--port", "8661"]) == 0
    out = capsys.readouterr().out
    assert "export MSKSC_URL=https://127.0.0.1:8661" in out
    assert "export MSKSC_TOKEN=the-token" in out


def test_the_uninstall_mirror(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, capsys
) -> None:
    monkeypatch.setattr(mac, "say", lambda _message: None)
    assert mac.main(["--uninstall"]) == 0
    assert any(c[:2] == ["limactl", "delete"] for c in procs.calls)
    assert "Lima itself" in capsys.readouterr().out


def test_a_named_refusal_exits_one(
    monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses, capsys
) -> None:
    monkeypatch.setattr(mac.sys, "platform", "linux")
    assert mac.main([]) == 1
    assert "runs on macOS" in capsys.readouterr().err
