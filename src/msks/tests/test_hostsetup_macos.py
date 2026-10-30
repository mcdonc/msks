"""The macOS host setup (#676): the tool that takes a Mac to a
running msksd-VM.

Covers the host check's refusals (the platform, the Silicon, the
Lima floor with its remedies), the instance config (the shipped
template plus the port forward this tool owns), the bootstrap
script's contract with the config and unit it writes, the token's
survival across reruns, the verify pass's any-answer-is-proof
shape, the preset's three exports, and the check rows' refusals —
against fakes at the two subprocess seams (`run`, `vm_shell`).
"""

from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from msks.server import hostsetup_macos as mac

# --- the host check ---------------------------------------------------


def test_the_platform_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(command, **_):
        raise mac.MacSetupError("not found")

    monkeypatch.setattr(mac, "run", missing)
    with pytest.raises(mac.MacSetupError, match="brew install lima"):
        mac.require_lima()


@pytest.mark.parametrize(
    ("version", "refuses"),
    [
        ("limactl version 2.2.1", False),
        ("limactl version 2.3.0", False),
        ("limactl version 2.1.0", True),
    ],
)
def test_the_lima_floor(
    monkeypatch: pytest.MonkeyPatch, version: str, refuses: bool
) -> None:
    monkeypatch.setattr(
        mac,
        "run",
        lambda command, **_: (
            version if command[:2] == ["limactl", "--version"] else ""
        ),
    )
    if refuses:
        with pytest.raises(mac.MacSetupError, match="upgrade"):
            mac.require_lima()
    else:
        assert mac.require_lima() == version.split()[-1]


# --- the instance config ----------------------------------------------


def test_the_instance_config_appends_the_forward() -> None:
    """The shipped template rides whole — the share roots under
    the reserved prefix, vz, nested virtualization — and the one
    piece this tool owns appends: the port forward, both
    loopbacks, the course VM's verified shape."""
    document = mac.instance_yaml(mac.PORT)
    assert "mountPoint: /mnt/msks/Users/{{.User}}" in document
    assert "nestedVirtualization: true" in document
    assert document.rstrip().endswith(
        f'  hostPort: {mac.PORT}\n  hostIP: "127.0.0.1"'
    )
    assert f"guestPort: {mac.PORT}" in document


def test_the_vm_is_created_started_or_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_):
        calls.append(command)
        if command[:2] == ["limactl", "list"]:
            return state
        return ""

    for state, expect in (
        ('[{"name":"other"}]', ["start", "--name", mac.VM_NAME]),
        ('[{"name":"msksd","status":"Stopped"}]', ["start", mac.VM_NAME]),
        ('[{"name":"msksd","status":"Running"}]', None),
    ):
        calls.clear()
        monkeypatch.setattr(mac, "run", fake_run)
        monkeypatch.setattr(mac, "say", lambda _message: None)
        mac.ensure_vm()
        if expect is None:
            assert not any(c[1] == "start" for c in calls)
        else:
            assert any(expect == c[1 : 1 + len(expect)] for c in calls)


# --- the bootstrap ----------------------------------------------------


def test_the_bootstrap_writes_the_geometry_and_survives_reruns() -> None:
    """The script installs the wheel, writes the config with the
    prefix-mode geometry (the template's reserved prefix, the
    user's home the default allowed root), the unit, and keeps
    the token file across runs — a rerun refreshes, never
    re-mints."""
    script = mac.bootstrap_script(None, "/Users/probe")
    assert "mount_host_mode: prefix" in script
    assert 'mount_host_roots: ["/Users/probe"]' in script
    assert f"port: {mac.VM_PORT}" in script
    assert 'socket: ""' in script
    assert "bootstrap-token" in script
    assert "if [ ! -s" in script
    assert f"msks=={mac.vm_version()}" in script
    assert "systemctl restart msksd.service" in script


def test_a_named_wheel_installs_the_file() -> None:
    script = mac.bootstrap_script(
        "/builds/msks-0.5.0-py3-none-any.whl", "/Users/p"
    )
    assert "/tmp/msks-0.5.0-py3-none-any.whl" in script
    assert f"port: {mac.VM_PORT}" in script


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


def test_the_preset_carries_the_three_exports(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(mac, "MAC_CA", tmp_path / "ca.pem")

    def fake_vm(script):
        assert "msks-ca.pem" in script or "bootstrap-token" in script
        if "msks-ca" in script:
            return (
                "-----BEGIN CERTIFICATE-----\nxyz\n-----END CERTIFICATE-----\n"
            )
        return "the-token\n"

    monkeypatch.setattr(mac, "vm_shell", fake_vm)
    monkeypatch.setattr(mac, "verify_chain", lambda port, ca: None)
    preset = mac.client_preset(mac.PORT)
    assert "export MSKSC_URL=https://127.0.0.1:8660" in preset
    assert "export MSKSC_TOKEN=the-token" in preset
    assert f"export MSKSC_CAFILE={tmp_path / 'ca.pem'}" in preset
    assert (tmp_path / "ca.pem").read_text().startswith("-----BEGIN")


def test_a_missing_ca_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mac, "vm_shell", lambda _script: "not a cert")
    with pytest.raises(mac.MacSetupError, match="CA did not come back"):
        mac.client_preset(mac.PORT)


# --- the check rows ---------------------------------------------------


def test_the_check_rows_answer_the_doctor_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mac, "require_lima", lambda: "2.2.1")
    monkeypatch.setattr(
        mac,
        "run",
        lambda command, **_: (
            '[{"name":"msksd","status":"Running"}]'
            if command[:2] == ["limactl", "list"]
            else ""
        ),
    )
    monkeypatch.setattr(
        mac, "verify_daemon", lambda port, timeout_s=15.0: None
    )
    answers = iter(
        [
            "nv\n",
            "present\n",
        ]
    )
    monkeypatch.setattr(mac, "vm_shell", lambda script: next(answers))
    rows = mac.check_rows(mac.PORT)
    assert rows[0] == "lima 2.2.1 at the template's floor"
    assert any("nested virtualization" in row for row in rows)
    assert any("virtiofsd present" in row for row in rows)
    assert any("answers https" in row for row in rows)


def test_a_missing_vm_names_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mac, "require_lima", lambda: "2.2.1")
    monkeypatch.setattr(mac, "run", lambda command, **_: "[]")
    with pytest.raises(mac.MacSetupError, match=f"run {mac.TOOL}"):
        mac.check_rows(mac.PORT)


def test_a_stopped_vm_names_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mac, "require_lima", lambda: "2.2.1")
    monkeypatch.setattr(
        mac,
        "run",
        lambda command, **_: '[{"name":"msksd","status":"Stopped"}]',
    )
    with pytest.raises(mac.MacSetupError, match="limactl start msksd"):
        mac.check_rows(mac.PORT)


# --- the entry ---------------------------------------------------------


def test_the_entry_prints_the_preset(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(mac, "check_host", lambda: {"lima": "2.2.1"})
    monkeypatch.setattr(mac, "ensure_vm", lambda: None)
    monkeypatch.setattr(mac, "bootstrap_vm", lambda *args: None)
    monkeypatch.setattr(mac, "verify_daemon", lambda port: None)
    monkeypatch.setattr(
        mac, "client_preset", lambda port: "export MSKSC_URL=x"
    )
    monkeypatch.setattr(mac, "ensure_vm", lambda **kwargs: None)
    assert mac.main([]) == 0
    assert "export MSKSC_URL=x" in capsys.readouterr().out


def test_the_uninstall_mirror(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_):
        calls.append(command)
        return ""

    monkeypatch.setattr(mac, "run", fake_run)
    monkeypatch.setattr(mac, "say", lambda _message: None)
    assert mac.main(["--uninstall"]) == 0
    assert calls == [["limactl", "delete", "-f", "msksd"]]
    assert "Lima itself" in capsys.readouterr().out


def test_a_named_refusal_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        mac,
        "check_host",
        lambda: (_ for _ in ()).throw(mac.MacSetupError("nope")),
    )
    assert mac.main([]) == 1
    assert "nope" in capsys.readouterr().err


def test_a_chain_mismatch_names_the_collision(monkeypatch) -> None:
    """The verify pass proves the served certificate chains to
    the VM's CA — the collision case (another VM's forward or a
    local daemon answering the host port) names the remedy."""

    def fake_context(**_kwargs):
        return None

    monkeypatch.setattr(mac.ssl, "create_default_context", fake_context)

    def fake_connect(address, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(mac.socket, "create_connection", fake_connect)
    with pytest.raises(mac.MacSetupError, match="--port"):
        mac.verify_chain(8660, "whatever")
