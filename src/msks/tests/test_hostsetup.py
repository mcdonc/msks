"""Tests over ``msks-setup-debian-host`` (#546).

The privileged actions shell through the ``sh`` seam and the file
touches through ``write_file``/``read_file`` seams (doctor's run
shape), so the suite pins the plan deterministically: the refusal
order, the command sequence, the file contents, and the summary.
The live root path runs on a dev host the way the nfqbind probe
did; CI has no consumer of the retired script.
"""

import os
import subprocess
from pathlib import Path

import msks.server.hostsetup as hostsetup
import pytest
from msks.server.hostsetup import (
    MODULES,
    SYSCTL_FILE,
    SYSCTL_TEXT,
    WRAPPER,
    main,
)

CAPS = "cap_net_admin,cap_net_bind_service"


class Host:
    """The fake host: every tool present, every command green."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.writes: dict[str, str] = {}
        self.ip_forward = "1\n"
        # trixie's getcap sorts the caps its own way; the fake
        # mirrors that order to pin the order-insensitive readback.
        self.getcap_answer = (
            f"{WRAPPER} cap_net_bind_service,cap_net_admin=ep\n"
        )
        self.modprobe_fails: set[str] = set()
        self.ambient = "0000000000001400"

    def sh(self, cmd, timeout=10.0, env=None):
        self.commands.append(cmd)
        head = cmd[0]
        if head == "getcap":
            return 0, self.getcap_answer, ""
        if head == WRAPPER:
            return 0, f"{self.ambient}\n", ""
        if head == "sysctl":
            return 0, "", ""
        if head == "modprobe":
            return (
                (1, "", "") if cmd[1] in self.modprobe_fails else (0, "", "")
            )
        if head == "ip":
            return 0, "default via 192.0.2.1 dev eno4 proto dhcp\n", ""
        return 0, "", ""

    def write(self, path: str, text: str) -> None:
        self.writes[path] = text

    def read(self, path: str) -> str:
        if path.endswith("ip_forward"):
            return self.ip_forward
        return self.writes.get(path, "")

    def ran(self, head: str) -> list[list[str]]:
        return [c for c in self.commands if c[0] == head]


@pytest.fixture()
def debian_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    host = Host()
    release = tmp_path / "os-release"
    release.write_text('ID="debian"\n')
    monkeypatch.setattr(hostsetup, "sh", host.sh)
    monkeypatch.setattr(hostsetup, "write_file", host.write)
    monkeypatch.setattr(hostsetup, "read_file", host.read)
    monkeypatch.setattr(hostsetup, "OS_RELEASE", str(release))
    monkeypatch.setattr(hostsetup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(hostsetup, "which", lambda tool: f"/usr/bin/{tool}")
    yield host


def test_non_root_refuses_before_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(hostsetup.os, "geteuid", lambda: 1000)
    assert main() == 1
    assert "run me as root (sudo msks-setup-debian-host)" in (
        capsys.readouterr().err
    )


def test_non_debian_host_refuses_by_name(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    release = Path(hostsetup.OS_RELEASE)
    release.write_text("ID=fedora\n")
    monkeypatch.setattr(hostsetup, "OS_RELEASE", str(release))
    assert main() == 1
    assert "targets Debian hosts (os-release says 'fedora')" in (
        capsys.readouterr().err
    )
    assert debian_host.commands == []


def test_happy_path_lands_every_piece(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    assert main() == 0
    # No tool was missing: apt never ran.
    assert debian_host.ran("apt-get") == []
    # The wrapper: fresh copy, caps set, read back, ambient proof.
    install = debian_host.ran("install")
    assert install == [
        [
            "install",
            "-o",
            "root",
            "-g",
            "root",
            "-m",
            "0755",
            "/usr/bin/setpriv",
            WRAPPER,
        ]
    ]
    assert debian_host.ran("setcap") == [["setcap", f"{CAPS}+ep", WRAPPER]]
    proof = debian_host.ran(WRAPPER)
    assert proof[0][1:3] == [
        "--inh-caps=+net_admin,+net_bind_service",
        "--ambient-caps=+net_admin,+net_bind_service",
    ]
    # The routing settings: file text, load, every module.
    assert debian_host.writes[SYSCTL_FILE] == SYSCTL_TEXT
    assert debian_host.ran("sysctl") == [["sysctl", "--load", SYSCTL_FILE]]
    modules_file = debian_host.writes[hostsetup.MODULES_FILE]
    assert modules_file.endswith("\n".join(MODULES) + "\n")
    assert len(debian_host.ran("modprobe")) == len(MODULES)
    out = capsys.readouterr().out
    assert "wrapper:    /usr/local/bin/msks-caps" in out
    assert "dev-daemon use: DEV_CAPS=/usr/local/bin/msks-caps msks-dev" in out
    assert (
        "uplink:" in out
        and "MSKSD_EGRESS_UPLINK, or DEV_UPLINK for msks-dev" in out
        and "eno4" in out
    )


def test_missing_tools_install_via_apt(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_which = hostsetup.which

    def lacking(tool):
        return None if tool == "conntrack" else real_which(tool)

    monkeypatch.setattr(hostsetup, "which", lacking)
    assert main() == 0
    apt = debian_host.ran("apt-get")
    assert apt[0] == ["apt-get", "-qq", "update"]
    assert apt[1] == ["apt-get", "install", "-y", "conntrack"]


def test_getcap_mismatch_refuses(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    debian_host.getcap_answer = f"{WRAPPER} nothing=ep\n"
    assert main() == 1
    assert f"getcap does not read {CAPS}=ep" in capsys.readouterr().err
    # The refusal stopped the run before the kernel settings.
    assert SYSCTL_FILE not in debian_host.writes


def test_zero_ambient_caps_refuse(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    debian_host.ambient = "0000000000000000"
    assert main() == 1
    assert "no ambient capabilities" in capsys.readouterr().err


def test_ip_forward_still_zero_refuses(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    debian_host.ip_forward = "0\n"
    assert main() == 1
    assert "still reads 0" in capsys.readouterr().err


def test_modprobe_failure_warns_without_refusing(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    debian_host.modprobe_fails = {"nfnetlink_queue"}
    assert main() == 0
    assert "WARNING modprobe nfnetlink_queue failed" in (
        capsys.readouterr().err
    )


def test_sh_runs_a_real_command() -> None:
    """The one seam the fakes replace: a real subprocess answers."""
    rc, out, err = hostsetup.sh(["echo", "hello"])
    assert (rc, out, err) == (0, "hello\n", "")


def test_os_release_missing_file_reads_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hostsetup, "OS_RELEASE", "/nonexistent")
    assert hostsetup.os_release_id() == ""


def test_os_release_without_id_reads_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = tmp_path / "os-release"
    release.write_text("PRETTY_NAME=Not Debian\n")
    monkeypatch.setattr(hostsetup, "OS_RELEASE", str(release))
    assert hostsetup.os_release_id() == ""


def test_os_release_id_strips_quotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = tmp_path / "os-release"
    release.write_text('ID="debian"\n')
    monkeypatch.setattr(hostsetup, "OS_RELEASE", str(release))
    assert hostsetup.os_release_id() == "debian"


def test_apt_failure_refuses(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    real_which = hostsetup.which

    def lacking(tool):
        return None if tool == "nft" else real_which(tool)

    monkeypatch.setattr(hostsetup, "which", lacking)

    def failing(cmd, timeout=10.0, env=None):
        debian_host.commands.append(cmd)
        if cmd[0] == "apt-get" and "update" in cmd:
            return 100, "", "apt update is broken"
        return debian_host.sh(cmd, timeout)

    monkeypatch.setattr(hostsetup, "sh", failing)
    assert main() == 1
    assert "apt-get update failed: apt update is broken" in (
        capsys.readouterr().err
    )
    # The script's set -e contract: no install after a failed update.
    assert debian_host.ran("apt-get") == [["apt-get", "-qq", "update"]]


def test_missing_setpriv_refuses(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    real_which = hostsetup.which

    def lacking(tool):
        return None if tool == "setpriv" else real_which(tool)

    monkeypatch.setattr(hostsetup, "which", lacking)
    debian_host.sh = lambda cmd, timeout=10.0: (  # apt "succeeds"
        (0, "", "")
        if cmd[0] == "apt-get"
        else Host.sh(debian_host, cmd, timeout)
    )
    assert main() == 1
    assert "setpriv not found after package install" in (
        capsys.readouterr().err
    )


def test_install_failure_refuses(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def failing(cmd, timeout=10.0):
        debian_host.commands.append(cmd)
        if cmd[0] == "install":
            return 1, "", "no space left"
        return debian_host.sh(cmd, timeout)

    monkeypatch.setattr(hostsetup, "sh", failing)
    assert main() == 1
    assert "installing /usr/local/bin/msks-caps failed: no space left" in (
        capsys.readouterr().err
    )


def test_setcap_failure_refuses(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def failing(cmd, timeout=10.0):
        debian_host.commands.append(cmd)
        if cmd[0] == "setcap":
            return 1, "", "operation not supported"
        return debian_host.sh(cmd, timeout)

    monkeypatch.setattr(hostsetup, "sh", failing)
    assert main() == 1
    assert "filesystem must support capability xattrs" in (
        capsys.readouterr().err
    )


def test_wrapper_proof_command_failure_refuses(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def failing(cmd, timeout=10.0):
        debian_host.commands.append(cmd)
        if cmd[0] == WRAPPER:
            return 1, "", "setpriv: capset failed"
        return debian_host.sh(cmd, timeout)

    monkeypatch.setattr(hostsetup, "sh", failing)
    assert main() == 1
    assert "no ambient capabilities" in capsys.readouterr().err


def test_kvm_absent_hint(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Both absence shapes read the same: no such group (KeyError)
    and a failing NSS backend (OSError) — the script's getent
    degraded the same way."""

    def no_group(name):
        raise KeyError(name)

    def broken_backend(name):
        raise OSError("nss is down")

    for failure in (no_group, broken_backend):
        monkeypatch.setattr(hostsetup.grp, "getgrnam", failure)
        assert main() == 0
        assert "kvm group is absent" in capsys.readouterr().out


def test_uplink_without_a_default_route(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ip answers success with no route, and a failing ip answers
    nothing: both read as no uplink."""
    monkeypatch.setattr(hostsetup, "sh", lambda cmd, timeout=10.0: (0, "", ""))
    assert hostsetup.uplink() == ""
    monkeypatch.setattr(
        hostsetup, "sh", lambda cmd, timeout=10.0: (1, "", "oops")
    )
    assert hostsetup.uplink() == ""


def test_the_file_seams_touch_the_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real seams the fakes replace: write_file lands root-owned
    bytes (chown behind the root seam here) and read_file reads
    them back; which answers PATH."""
    monkeypatch.setattr(hostsetup.os, "chown", lambda *a: None)
    target = tmp_path / "sysctl.d" / "msks.conf"
    hostsetup.write_file(str(target), "net.ipv4.ip_forward = 1\n")
    assert hostsetup.read_file(str(target)) == "net.ipv4.ip_forward = 1\n"
    assert target.stat().st_mode & 0o644 == 0o644
    assert hostsetup.which("no-such-tool-anywhere") is None
    assert hostsetup.which("sh") is not None


def test_granted_caps_reads_the_set_before_ep() -> None:
    """The parser behind the order-insensitive readback: one =ep
    token's names as a set, empty when the output carries none."""
    assert hostsetup.granted_caps(
        f"{WRAPPER} cap_net_bind_service,cap_net_admin=ep"
    ) == {"cap_net_admin", "cap_net_bind_service"}
    assert hostsetup.granted_caps("") == set()
    assert hostsetup.granted_caps(f"{WRAPPER} nothing") == set()


def test_sh_converts_exceptions_to_the_failure_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing binary and a timeout answer (-1, "", reason) —
    doctor's one-shape-of-failure rule — so no traceback escapes
    the entrypoint."""
    rc, out, err = hostsetup.sh(["no-such-binary-anywhere"])
    assert (rc, out) == (-1, "")
    assert "no-such-binary-anywhere" in err

    def expired(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(hostsetup.subprocess, "run", expired)
    rc, out, err = hostsetup.sh(["sleep", "100"], timeout=1)
    assert (rc, out) == (-1, "")
    assert "sleep" in err


def test_apt_install_runs_noninteractive(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The script's guarantee, kept: the install env carries
    DEBIAN_FRONTEND=noninteractive so debconf never reaches for
    the terminal."""
    seen: list[dict | None] = []
    real_sh = debian_host.sh

    def recording(cmd, timeout=10.0, env=None):
        seen.append(env)
        return real_sh(cmd, timeout)

    monkeypatch.setattr(hostsetup, "sh", recording)
    release = Path(hostsetup.OS_RELEASE)
    release.write_text("ID=debian\n")
    monkeypatch.setattr(hostsetup, "OS_RELEASE", str(release))
    monkeypatch.setattr(
        hostsetup,
        "which",
        lambda tool: None if tool == "nft" else "/usr/bin/x",
    )
    assert main() == 0
    installs = [e for e in seen if e is not None]
    assert installs and all(
        e.get("DEBIAN_FRONTEND") == "noninteractive" for e in installs
    )


def test_path_restored_after_the_run(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Debian PATH prepend holds for the run and leaves."""
    monkeypatch.setenv("PATH", "/original")
    assert main() == 0
    assert os.environ["PATH"] == "/original"


def test_apt_install_failure_refuses(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """update succeeds, install fails: the refusal names install
    (the split from update's refusal, above)."""
    real_which = hostsetup.which

    def lacking(tool):
        return None if tool == "nft" else real_which(tool)

    monkeypatch.setattr(hostsetup, "which", lacking)

    def failing(cmd, timeout=10.0, env=None):
        debian_host.commands.append(cmd)
        if cmd[0] == "apt-get" and "install" in cmd:
            return 100, "", "dpkg is locked"
        return debian_host.sh(cmd, timeout, env)

    monkeypatch.setattr(hostsetup, "sh", failing)
    assert main() == 1
    assert "apt install failed: dpkg is locked" in capsys.readouterr().err
