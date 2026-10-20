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
from types import SimpleNamespace

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
        self.modprobe_absent: set[str] = set()
        self.ambient = "0000000000001400"
        # ldconfig answers with both NFQUEUE sonames present by
        # default (#598); starving it dials the library install.
        self.ldconfig_answer = (
            "        libnetfilter_queue.so.1 "
            "(libc6,x86-64) => /lib/x86_64-linux-gnu/"
            "libnetfilter_queue.so.1\n"
            "        libnfnetlink.so.0 "
            "(libc6,x86-64) => /lib/x86_64-linux-gnu/libnfnetlink.so.0\n"
        )
        # The service phase's dials (#560): is-active's answer, the
        # systemctl subcommands a run must fail on, and adduser's.
        self.is_active = 0
        self.fail_systemctl: set[str] = set()
        self.loginctl_rc = 0
        self.adduser_rc = 0
        self.bus_fails = 0

    def sh(self, cmd, timeout=10.0, env=None):
        self.commands.append(cmd)
        head = cmd[0]
        if head == "getcap":
            return 0, self.getcap_answer, ""
        if head == "ldconfig":
            return 0, self.ldconfig_answer, ""
        if head == WRAPPER:
            return 0, f"{self.ambient}\n", ""
        if head == "sysctl":
            return 0, "", ""
        if head == "modprobe":
            fail_set = (
                self.modprobe_absent
                if "--dry-run" in cmd
                else self.modprobe_fails
            )
            return (1, "", "") if cmd[-1] in fail_set else (0, "", "")
        if head == "ip":
            return 0, "default via 192.0.2.1 dev eno4 proto dhcp\n", ""
        if head == "adduser":
            if self.adduser_rc != 0:
                return self.adduser_rc, "", "adduser: refused\n"
        if head == "loginctl":
            if self.loginctl_rc != 0:
                return self.loginctl_rc, "", "loginctl: refused\n"
        if head == "runuser":
            sub = cmd[cmd.index("systemctl") + 2]
            if sub == "daemon-reload" and self.bus_fails > 0:
                self.bus_fails -= 1
                return 1, "", "Failed to connect to bus: ...\n"
            if sub in self.fail_systemctl:
                return 1, "", f"systemctl --user {sub}: refused\n"
            if sub == "is-active":
                return self.is_active, "", ""
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
    # The deployment's venv: the tool and its msksd sibling share
    # a bin dir, the systemd manager probe finds a live manager,
    # and the service runs as a fixed executing user (#560).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "msksd").write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        hostsetup.sys, "argv", [str(bin_dir / "msks-setup-debian-host")]
    )
    manager = tmp_path / "systemd"
    manager.mkdir()
    monkeypatch.setattr(hostsetup, "SYSTEMD_MANAGER", str(manager))
    monkeypatch.setattr(hostsetup, "group_present", lambda name: name == "kvm")
    monkeypatch.setattr(hostsetup, "in_group", lambda user, name: False)
    home = tmp_path / "home" / "chrism"
    home.mkdir(parents=True)
    monkeypatch.setattr(
        hostsetup, "executing_user", lambda: ("chrism", 1000, 1000, str(home))
    )
    # The user manager is up by default (the login-session shape);
    # the stale-manager tests point this at a missing path.
    runtime = tmp_path / "run-user-1000"
    runtime.mkdir()
    monkeypatch.setattr(
        hostsetup, "RUNTIME_DIR_FMT", str(runtime.parent / "run-user-{}")
    )
    # chown is root-only; the seam records the handovers instead.
    chowns: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        hostsetup,
        "chown_path",
        lambda path, uid, gid: chowns.append((str(path), uid, gid)),
    )
    host.chowns = chowns
    monkeypatch.setattr(hostsetup, "RETRY_DELAY", 0.0)
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
    """The hint names the invoked path: an install root's PATH
    does not carry a venv's bin, so the bare name cannot work
    under sudo."""
    monkeypatch.setattr(hostsetup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        hostsetup.sys,
        "argv",
        ["/home/chrism/msks/.venv/bin/msks-setup-debian-host"],
    )
    assert main() == 1
    assert (
        "run me as root (sudo "
        "/home/chrism/msks/.venv/bin/msks-setup-debian-host)"
        in (capsys.readouterr().err)
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
    real_loads = [
        c for c in debian_host.ran("modprobe") if "--dry-run" not in c
    ]
    assert len(real_loads) == len(MODULES)
    out = capsys.readouterr().out
    assert "consent:    interactive egress consent available" in out
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


def test_every_doctor_tool_gains_its_package(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's volume/image tools and the client-side pair
    install through apt like the plumbing set (#598): each tool's
    absence lands the package doctor's fix names."""
    real_which = hostsetup.which

    def lacking(tool):
        return (
            None
            if tool in {"mkfs.ext4", "qemu-img", "tmux"}
            else (real_which(tool))
        )

    monkeypatch.setattr(hostsetup, "which", lacking)
    assert main() == 0
    apt = debian_host.ran("apt-get")
    assert apt[1] == [
        "apt-get",
        "install",
        "-y",
        "e2fsprogs",
        "qemu-utils",
        "tmux",
    ]


def test_fetch_upstream_streams_to_disk(tmp_path: Path) -> None:
    """The network seam itself: one source, one destination,
    byte-identical (a file:// URL keeps the test offline)."""
    source = tmp_path / "release"
    source.write_bytes(b"BINARY\n")
    dest = tmp_path / "dest"
    hostsetup.fetch_upstream(source.as_uri(), str(dest))
    assert dest.read_bytes() == b"BINARY\n"


def test_absent_sonames_install_their_packages(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linker cache without the NFQUEUE sonames lands the
    soname packages; a complete host installs nothing (#598)."""
    debian_host.ldconfig_answer = (
        "        libcap.so.2 (libc6) => /lib/libcap.so.2\n"
    )
    assert main() == 0
    apt = debian_host.ran("apt-get")
    assert apt[1][-2:] == ["libnetfilter-queue1", "libnfnetlink0"]


def test_an_unreadable_linker_cache_installs_the_sonames(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that cannot run counts as absent: the packages
    land rather than pass silently (#598)."""

    def broken_sh(cmd, timeout=10.0, env=None):
        if cmd[0] == "ldconfig":
            return 1, "", "ldconfig: cannot read"
        return debian_host.sh(cmd, timeout=timeout, env=env)

    monkeypatch.setattr(hostsetup, "sh", broken_sh)
    assert main() == 0
    apt = debian_host.ran("apt-get")
    assert "libnetfilter-queue1" in apt[1]


def test_cloud_hypervisor_lands_from_upstream_when_absent(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """No VMM on the host: the tool streams the upstream static
    release to /usr/local/bin, executable, atomically (#598)."""
    real_which = hostsetup.which
    fetched: list[str] = []
    target = tmp_path / "cloud-hypervisor"

    def lacking(tool):
        return None if tool == "cloud-hypervisor" else real_which(tool)

    def fake_fetch(url: str, dest: str) -> None:
        fetched.append(url)
        Path(dest).write_bytes(b"CH-BINARY\n")

    monkeypatch.setattr(hostsetup, "which", lacking)
    monkeypatch.setattr(hostsetup, "fetch_upstream", fake_fetch)
    monkeypatch.setattr(hostsetup, "CLOUD_HYPERVISOR_BIN", str(target))
    assert main() == 0
    assert fetched == [hostsetup.cloud_hypervisor_url()]
    assert debian_host.ran(str(target)) == [[str(target), "--version"]]
    assert target.read_bytes() == b"CH-BINARY\n"
    assert target.stat().st_mode & 0o111  # executable
    assert not (tmp_path / "cloud-hypervisor.part").exists()
    assert "cloud-hypervisor: landed" in capsys.readouterr().out


def test_a_present_cloud_hypervisor_stays_untouched(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The host's own VMM install wins: nothing downloads, no
    fetch seam even runs (#598)."""
    fetched: list[str] = []
    monkeypatch.setattr(
        hostsetup,
        "fetch_upstream",
        lambda url, dest: fetched.append(url),
    )
    assert main() == 0
    assert fetched == []


def test_the_asset_matches_the_machine(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One static build per architecture: aarch64 hosts fetch
    the -aarch64 asset, everything else the x86-64 default
    (#598)."""
    import collections

    Uname = collections.namedtuple("Uname", "machine")
    monkeypatch.setattr(hostsetup.os, "uname", lambda: Uname("aarch64"))
    assert hostsetup.cloud_hypervisor_url().endswith(
        "cloud-hypervisor-static-aarch64"
    )
    monkeypatch.setattr(hostsetup.os, "uname", lambda: Uname("x86_64"))
    assert hostsetup.cloud_hypervisor_url().endswith("cloud-hypervisor-static")
    # an unknown machine falls back to the x86-64 asset, the
    # release's unsuffixed build
    monkeypatch.setattr(hostsetup.os, "uname", lambda: Uname("riscv64"))
    assert hostsetup.cloud_hypervisor_url().endswith("cloud-hypervisor-static")


def test_a_binary_that_does_not_run_is_removed_and_named(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The post-landing --version probe: a truncated or
    wrong-architecture download is removed, and the refusal
    names the machine and the URL — the next run downloads
    again instead of defending a broken binary (#598)."""
    real_which = hostsetup.which
    target = tmp_path / "cloud-hypervisor"

    def lacking(tool):
        return None if tool == "cloud-hypervisor" else real_which(tool)

    def broken_sh(cmd, timeout=10.0, env=None):
        if cmd[0] == str(target):
            return 126, "", "cannot execute binary file\n"
        return debian_host.sh(cmd, timeout=timeout, env=env)

    monkeypatch.setattr(hostsetup, "which", lacking)
    monkeypatch.setattr(
        hostsetup,
        "fetch_upstream",
        lambda url, dest: Path(dest).write_bytes(b"X"),
    )
    monkeypatch.setattr(hostsetup, "CLOUD_HYPERVISOR_BIN", str(target))
    monkeypatch.setattr(hostsetup, "sh", broken_sh)
    assert main() == 1
    err = capsys.readouterr().err
    assert "does not run" in err
    assert hostsetup.os.uname().machine in err
    assert hostsetup.cloud_hypervisor_url() in err
    assert not target.exists()


def test_the_apt_list_carries_each_package_once(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three tools share e2fsprogs; a slim host missing all of
    them installs the package once (#598)."""
    real_which = hostsetup.which

    def lacking(tool):
        return (
            None
            if tool in {"mkfs.ext4", "resize2fs", "e2fsck"}
            else real_which(tool)
        )

    monkeypatch.setattr(hostsetup, "which", lacking)
    assert main() == 0
    apt = debian_host.ran("apt-get")
    assert apt[1].count("e2fsprogs") == 1


def test_cloud_hypervisor_download_failure_names_the_source(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A network failure is a named refusal carrying the URL,
    after every other host piece landed (#598)."""

    def no_network(url: str, dest: str) -> None:
        raise OSError("connection refused")

    real_which = hostsetup.which

    def lacking(tool):
        return None if tool == "cloud-hypervisor" else real_which(tool)

    monkeypatch.setattr(hostsetup, "which", lacking)
    monkeypatch.setattr(hostsetup, "fetch_upstream", no_network)
    monkeypatch.setattr(
        hostsetup, "CLOUD_HYPERVISOR_BIN", str(tmp_path / "ch")
    )
    assert main() == 1
    err = capsys.readouterr().err
    assert "cloud-hypervisor download failed" in err
    assert hostsetup.cloud_hypervisor_url() in err
    # The VMM lands last: every other piece (the wrapper, the
    # sysctl, the modules) landed before the refusal, and the
    # partial download left nothing behind.
    assert debian_host.ran("setcap") == [["setcap", f"{CAPS}+ep", WRAPPER]]
    assert SYSCTL_FILE in debian_host.writes
    assert not (tmp_path / "ch").exists()
    assert not (tmp_path / "ch.part").exists()


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
    """A module the tree carries whose load fails keeps the plain
    warning; the consent summary marks it missing."""
    debian_host.modprobe_fails = {"nfnetlink_queue"}
    assert main() == 0
    out = capsys.readouterr()
    assert "WARNING modprobe nfnetlink_queue failed" in out.err
    assert "UNAVAILABLE on this kernel — missing: nfnetlink_queue" in (out.out)


def fake_uname(monkeypatch: pytest.MonkeyPatch, release: str, machine: str):
    """A fixed uname for the flavor-gap tests (release and machine
    both read from it)."""
    monkeypatch.setattr(
        hostsetup.os,
        "uname",
        lambda: SimpleNamespace(release=release, machine=machine),
    )


def test_absent_consent_modules_name_the_cause_and_fix(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The trimmed-tree gap (#550): absent modules warn with the
    observation, the cause shape, and the running release; the fix
    names the machine's generic package; the summary marks
    interactive consent unavailable naming every missing module."""
    debian_host.modprobe_absent = {
        "nft_reject",
        "nf_reject_ipv4",
        "nfnetlink_queue",
    }
    fake_uname(monkeypatch, "6.12.107+deb13-cloud-amd64", machine="amd64")
    monkeypatch.setattr(hostsetup, "module_tree_present", lambda: True)
    assert main() == 0
    out = capsys.readouterr()
    assert (
        "WARNING nfnetlink_queue is absent from this kernel's module "
        "tree — this kernel's build carries a trimmed module set "
        "(running release 6.12.107+deb13-cloud-amd64)" in out.err
    )
    assert "apt install linux-image-amd64, reboot, re-run" in out.err
    assert (
        "consent:    interactive egress consent UNAVAILABLE on this "
        "kernel — missing: nft_reject, nf_reject_ipv4, nfnetlink_queue"
        in out.out
    )


def test_absent_modules_with_no_tree_name_the_missing_package(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A host with no module tree for its release (a container, a
    pruned image): the cause says so — not a flavor claim the tool
    cannot verify (#550's review)."""
    debian_host.modprobe_absent = {"nfnetlink_queue"}
    fake_uname(monkeypatch, "6.12.107+deb13-amd64", machine="x86_64")
    monkeypatch.setattr(hostsetup, "module_tree_present", lambda: False)
    assert main() == 0
    out = capsys.readouterr()
    assert (
        "no module tree is installed on this host "
        "(running release 6.12.107+deb13-amd64)" in out.err
    )
    assert "apt install linux-image-amd64, reboot, re-run" in out.err


def test_the_fix_names_the_machines_own_package(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """An arm64 host gets the arm64 generic package, not amd64's
    (#550's review)."""
    debian_host.modprobe_absent = {"nfnetlink_queue"}
    fake_uname(monkeypatch, "6.12.107+deb13-arm64", machine="aarch64")
    monkeypatch.setattr(hostsetup, "module_tree_present", lambda: True)
    assert main() == 0
    assert "apt install linux-image-arm64, reboot, re-run" in (
        capsys.readouterr().err
    )


def test_an_infra_failure_is_a_load_failure_not_an_absence(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """sh's -1 (a timeout, a missing binary) reads as a load
    failure — the plain warning — never as the flavor-gap claim
    (#550's review)."""

    def flaky(cmd, timeout=10.0, env=None):
        debian_host.commands.append(cmd)
        if cmd[0] == "modprobe" and "--dry-run" in cmd:
            return -1, "", "modprobe: timed out"
        return debian_host.sh(cmd, timeout, env)

    monkeypatch.setattr(hostsetup, "sh", flaky)
    assert main() == 0
    out = capsys.readouterr()
    assert "WARNING modprobe tun failed — check the kernel" in out.err
    assert "absent from this kernel's module tree" not in out.err


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
        monkeypatch.setattr(hostsetup, "group_present", lambda name: False)
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


def test_module_tree_present_reads_the_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real seam behind the fake: a tree directory for the
    running release answers True, a missing one False."""
    release = "6.12.107-test-amd64"
    fake_uname(monkeypatch, release, machine="amd64")
    monkeypatch.setattr(hostsetup, "MODULES_ROOT", str(tmp_path))
    assert hostsetup.module_tree_present() is False
    (tmp_path / release).mkdir()
    assert hostsetup.module_tree_present() is True


def test_a_fresh_landing_adds_the_executing_user_to_kvm(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    """Bare runs and new logins need the kvm group on the account
    itself (#560): the tool adds the executing user when the
    group is missing from their membership, and the summary names
    the fresh-login fact."""
    assert main() == 0
    assert debian_host.ran("adduser") == [["adduser", "chrism", "kvm"]]
    out = capsys.readouterr().out
    assert (
        "/dev/kvm:   added chrism to kvm — a fresh login picks the group up"
        in (out)
    )


def test_an_existing_kvm_membership_is_left_alone(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hostsetup, "in_group", lambda user, name: True)
    assert main() == 0
    assert debian_host.ran("adduser") == []


def test_adduser_kvm_failure_refuses(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    debian_host.adduser_rc = 1
    assert main() == 1
    assert "adduser chrism kvm failed" in capsys.readouterr().err


def test_in_group_reads_membership_and_primary_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two gates: named membership in the group's member list,
    and the account's primary group — the same pair #555's peer
    policy reads. A database that answers nothing says no."""

    class MemberGrp:
        class entry:
            gr_mem = ["chrism"]
            gr_gid = 104

        def getgrnam(self, name):
            return self.entry

    monkeypatch.setattr(hostsetup, "grp", MemberGrp())
    monkeypatch.setattr(
        hostsetup,
        "pwd_getpwnam",
        lambda name: FakeEntry("chrism", 1000, "/home/chrism"),
    )
    assert hostsetup.in_group("chrism", "kvm") is True

    class PrimaryGrp:
        class entry:
            gr_mem = []
            gr_gid = 104

        def getgrnam(self, name):
            return self.entry

    monkeypatch.setattr(hostsetup, "grp", PrimaryGrp())
    monkeypatch.setattr(
        hostsetup,
        "pwd_getpwnam",
        lambda name: FakeEntry("chrism", 1000, "/home/chrism", gid=104),
    )
    assert hostsetup.in_group("chrism", "kvm") is True

    class MissingGrp:
        def getgrnam(self, name):
            raise KeyError(name)

    monkeypatch.setattr(hostsetup, "grp", MissingGrp())
    assert hostsetup.in_group("chrism", "kvm") is False


def fixture_unit_path(host: Host) -> str:
    """The unit path the fixture's executing user lands."""
    return hostsetup.unit_path(hostsetup.executing_user()[3])


def unit_writes(host: Host) -> dict[str, str]:
    """The unit file(s) a run wrote (any path under .config)."""
    return {
        path: text
        for path, text in host.writes.items()
        if path.endswith(hostsetup.USER_UNIT_RELATIVE)
    }


def test_chown_path_runs_the_real_chown(tmp_path: Path) -> None:
    """The seam behind the recorder: os.chown to the caller's own
    uid and gid (permitted unprivileged for the owner) exercises
    the body the suite otherwise patches away."""
    target = tmp_path / "owned"
    target.write_text("x")
    hostsetup.chown_path(target, os.getuid(), os.getgid())
    assert target.stat().st_uid == os.getuid()


def systemctl_ran(host: Host, sub: str) -> list[list[str]]:
    """The systemctl --user calls a run issued for one subcommand,
    as the runuser command lines carry them."""
    out = []
    for cmd in host.ran("runuser"):
        args = cmd[cmd.index("systemctl") + 2 :]
        if args[0] == sub:
            out.append(cmd)
    return out


def user_unit_path() -> str:
    """The unit path the fixture's executing user carries."""
    return hostsetup.unit_path("/tmp/fake-home/chrism")


def test_service_lands_the_unit_as_the_executing_user(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The service phase (#560): a user unit under the executing
    operator's own config — no User=/Group= lines (the user manager
    is the account), the wrapper raising the caps ambient (file
    capabilities grant any executor), the #555 socket's runtime
    directory under the user's $XDG_RUNTIME_DIR, egress pointed at
    the detected uplink — plus linger, so the daemon survives
    logout. First landing starts the service and does not restart
    it."""

    def strict_unit_read(path):
        # The real read_file raises on a missing file; the fake
        # answers "" — drive the real shape for the unit path.
        if path.endswith(hostsetup.USER_UNIT_RELATIVE):
            raise FileNotFoundError(path)
        return debian_host.read(path)

    monkeypatch.setattr(hostsetup, "read_file", strict_unit_read)
    assert main() == 0
    home = Path(hostsetup.executing_user()[3])
    path = hostsetup.unit_path(str(home))
    text = debian_host.writes[path]
    msksd = str(Path(hostsetup.sys.argv[0]).resolve().parent / "msksd")
    assert path.startswith(str(home))
    assert "User=" not in text
    assert "Group=" not in text
    assert "Environment=HOME=" not in text
    assert (
        "ExecStart=/usr/local/bin/msks-caps "
        "--inh-caps=+net_admin,+net_bind_service "
        "--ambient-caps=+net_admin,+net_bind_service "
        f'-- "{msksd}"' in text
    )
    assert "WantedBy=default.target" in text
    assert "RuntimeDirectory=msks" in text
    assert "Environment=MSKSD_EGRESS_ENABLED=true" in text
    assert "Environment=MSKSD_EGRESS_UPLINK=eno4" in text
    assert debian_host.ran("loginctl") == [
        ["loginctl", "enable-linger", "chrism"]
    ]

    def run(args):
        return hostsetup.user_systemctl("chrism", 1000, args)

    assert systemctl_ran(debian_host, "daemon-reload") == [
        run(["daemon-reload"])
    ]
    assert systemctl_ran(debian_host, "enable") == [
        run(["enable", "--now", "msksd.service"])
    ]
    assert systemctl_ran(debian_host, "is-active") == []
    assert systemctl_ran(debian_host, "restart") == []
    out = capsys.readouterr().out
    assert (
        "service:    user service msksd.service "
        "(chrism, enabled, started, lingering)" in out
    )
    assert "token-free (#555): $XDG_RUNTIME_DIR/msks/msksd.sock" in out


def test_unit_text_omits_the_uplink_when_none_was_found(
    debian_host: Host,
) -> None:
    """An uplink the tool could not detect writes no env line —
    the piece lands only when the host carries it."""
    text = hostsetup.unit_text("/opt/my msks/msksd", uplink_name="")
    assert "MSKSD_EGRESS_UPLINK" not in text
    assert "RuntimeDirectory=msks" in text
    assert '-- "/opt/my msks/msksd"' in text


def test_service_skips_when_no_msksd_beside_the_tool(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment whose msksd lives elsewhere (no bin sibling)
    keeps a working host: the skip names the cause, no unit is
    written, systemctl never runs."""
    Path(hostsetup.sys.argv[0]).parent.joinpath("msksd").unlink()
    assert main() == 0
    assert unit_writes(debian_host) == {}
    assert debian_host.ran("runuser") == []


def test_service_skips_without_a_systemd_manager(
    debian_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container or another init: the manager probe answers no
    and the phase stands down, naming it."""
    monkeypatch.setattr(hostsetup, "SYSTEMD_MANAGER", "/no/such/manager-dir")
    assert main() == 0
    assert unit_writes(debian_host) == {}
    assert debian_host.ran("runuser") == []
    assert debian_host.ran("loginctl") == []


def test_no_service_flag_keeps_today_s_behavior(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    """--no-service lands every host piece but the unit (a
    deployment that runs msksd its own way)."""
    assert main(["--no-service"]) == 0
    assert unit_writes(debian_host) == {}
    assert debian_host.ran("runuser") == []
    assert "service:    skipped (--no-service)" in capsys.readouterr().out


def test_a_rerun_with_an_unchanged_unit_does_not_restart(
    debian_host: Host,
) -> None:
    """Idempotent re-run: the unit on disk equals the one the tool
    writes, so the service stays as enable --now left it."""
    assert main() == 0
    debian_host.commands.clear()
    assert main() == 0
    assert systemctl_ran(debian_host, "is-active") == []
    assert systemctl_ran(debian_host, "restart") == []


def test_a_changed_unit_restarts_a_running_daemon(
    debian_host: Host,
) -> None:
    """A re-run that rewrites the unit (a refreshed ExecStart, a
    new uplink) restarts the daemon when it is running, so the
    change takes effect."""
    debian_host.writes[fixture_unit_path(debian_host)] = (
        "ExecStart=/old/msksd\n"
    )
    assert main() == 0

    def run(args):
        return hostsetup.user_systemctl("chrism", 1000, args)

    assert systemctl_ran(debian_host, "is-active") == [
        run(["is-active", "--quiet", "msksd.service"])
    ]
    assert systemctl_ran(debian_host, "restart") == [
        run(["restart", "msksd.service"])
    ]


def test_a_changed_unit_leaves_a_stopped_daemon_alone(
    debian_host: Host,
) -> None:
    """A stopped service (the daemon crashed out, the host just
    booted) needs no restart — enable --now starts it."""
    debian_host.writes[fixture_unit_path(debian_host)] = (
        "ExecStart=/old/msksd\n"
    )
    debian_host.is_active = 1
    assert main() == 0
    assert systemctl_ran(debian_host, "restart") == []


def test_daemon_reload_failure_refuses(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    debian_host.fail_systemctl = {"daemon-reload"}
    assert main() == 1
    assert "systemctl --user daemon-reload failed" in capsys.readouterr().err


def test_enable_failure_refuses_and_points_at_the_journal(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    debian_host.fail_systemctl = {"enable"}
    assert main() == 1
    err = capsys.readouterr().err
    assert "systemctl --user enable --now msksd.service failed" in err
    assert "journalctl --user -u msksd names the cause" in err


def test_linger_failure_refuses(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    """Without linger the daemon dies at the operator's logout —
    a failure to arm it is a named refusal, not a warning."""
    debian_host.loginctl_rc = 1
    assert main() == 1
    err = capsys.readouterr().err
    assert "loginctl enable-linger chrism failed" in err
    assert "stop at the operator's logout" in err


class FakeEntry:
    """A pwd database row with the fields executing_user and
    in_group read."""

    def __init__(
        self, name: str, uid: int, pw_dir: str, gid: int = 1000
    ) -> None:
        self.pw_name = name
        self.pw_uid = uid
        self.pw_gid = gid
        self.pw_dir = pw_dir


class FakeGroup:
    def __init__(self, name: str) -> None:
        self.gr_name = name


def test_executing_user_prefers_sudo_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sudo lifted the tool to root; the account that typed the
    command is the one the service runs as. This one drives the
    real getpwnam seam."""
    monkeypatch.setenv("SUDO_USER", "alice")

    class AlicePwd:
        def getpwnam(self, name):
            return FakeEntry(name, 1001, "/home/alice", gid=1001)

        def getpwuid(self, uid):
            return FakeEntry("root", 0, "/root")

    monkeypatch.setattr(hostsetup, "pwd", AlicePwd())
    monkeypatch.setattr(
        hostsetup, "pwd_getpwuid", lambda uid: FakeEntry("root", 0, "/root")
    )
    assert hostsetup.executing_user() == ("alice", 1001, 1001, "/home/alice")


def test_executing_user_falls_back_when_sudo_user_left_no_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale SUDO_USER (the account was removed after the sudo
    started) degrades to the effective account, which adduser-era
    refusals never see: the lookup failure is a fallback, not a
    stop."""
    monkeypatch.setenv("SUDO_USER", "ghost")

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(hostsetup, "pwd_getpwnam", missing)
    monkeypatch.setattr(
        hostsetup,
        "pwd_getpwuid",
        lambda uid: FakeEntry("root", 0, "/root", gid=0),
    )
    assert hostsetup.executing_user() == ("root", 0, 0, "/root")


def test_executing_user_reads_the_effective_account_without_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root shell that ran the tool directly (no sudo): root is
    the executing user, its own choice. This one drives the real
    pwd/grp seams (the lookup helpers are the suite's pin on
    them)."""
    monkeypatch.delenv("SUDO_USER", raising=False)

    class RootPwd:
        def getpwuid(self, uid):
            return FakeEntry("root", 0, "/root", gid=0)

        def getpwnam(self, name):
            raise KeyError(name)

    monkeypatch.setattr(hostsetup, "pwd", RootPwd())
    assert hostsetup.executing_user() == ("root", 0, 0, "/root")


def test_group_present_degrades_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An NSS failure answers no — the same degrade kvm_hint's
    probe took — and a carried group answers yes."""

    class FlakyGrp:
        def getgrnam(self, name):
            raise OSError("nss unavailable")

    monkeypatch.setattr(hostsetup, "grp", FlakyGrp())
    assert hostsetup.group_present("kvm") is False

    class CarryingGrp:
        def getgrnam(self, name):
            return FakeGroup(name)

    monkeypatch.setattr(hostsetup, "grp", CarryingGrp())
    assert hostsetup.group_present("kvm") is True


def test_the_units_parent_chain_lands_user_owned(
    debian_host: Host, tmp_path: Path
) -> None:
    """A fresh Debian account ships no ~/.config, and write_file's
    mkdir would leave root-owned parents there — the user
    manager's enable symlink (written as the user) gets refused
    forever. Every missing level is created and handed to the
    executing user; the re-run finds them and chowns nothing."""
    home = Path(hostsetup.executing_user()[3])
    chain = [home / ".config", home / ".config" / "systemd"]
    chain.append(chain[-1] / "user")
    for level in chain:
        assert not level.exists()
    assert main() == 0
    for level in chain:
        assert level.is_dir()
    assert debian_host.chowns == [(str(level), 1000, 1000) for level in chain]
    debian_host.commands.clear()
    debian_host.chowns.clear()
    assert main() == 0
    assert debian_host.chowns == []


def test_kvm_joined_under_a_running_manager_names_the_restart(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    """The manager a login session already started forked before
    this run's adduser, so the just-started daemon holds the old
    supplementary groups — the service line names the symptom, the
    manager restart that hands the group over, and what that
    restart really stops."""
    assert main() == 0
    out = capsys.readouterr().out
    assert "the manager may predate the kvm membership" in out
    assert "sudo systemctl restart user@1000.service" in out
    assert "login sessions survive" in out


def test_kvm_note_survives_a_member_rerun(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The round-2 wash-out: run one added kvm under a running
    manager; a re-run finds the membership present and the unit
    unchanged — and still prints the note, because the running
    daemon's manager cannot be proven to postdate the membership
    it already carries."""
    monkeypatch.setattr(hostsetup, "in_group", lambda user, name: True)
    assert main() == 0
    out = capsys.readouterr().out
    assert "the manager may predate the kvm membership" in out


def test_kvm_joined_before_the_manager_started_needs_no_restart(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Linger started the manager after the adduser, so the fresh
    manager — and the daemon it forks — already carry kvm: the
    service line carries no restart note."""
    monkeypatch.setattr(
        hostsetup, "RUNTIME_DIR_FMT", str(tmp_path / "absent-{}")
    )
    assert main() == 0
    out = capsys.readouterr().out
    assert "user@1000.service" not in out
    assert "(chrism, enabled, started, lingering)" in out


def test_the_first_user_manager_call_retries_the_bus_race(
    debian_host: Host,
) -> None:
    """enable-linger returns after logind enqueues the user
    manager; the first systemctl --user can beat the bus into
    existence. A "Failed to connect to bus" answer retries — a
    transient miss recovers, the run stays one shot."""
    debian_host.bus_fails = 2
    assert main() == 0
    assert len(systemctl_ran(debian_host, "daemon-reload")) == 3


def test_an_unyielding_bus_race_refuses(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    """A bus that never comes up is a named refusal, not an
    idempotent-silent one."""
    debian_host.bus_fails = 99
    assert main() == 1
    assert "systemctl --user daemon-reload failed" in capsys.readouterr().err


def test_restart_failure_refuses(
    debian_host: Host, capsys: pytest.CaptureFixture
) -> None:
    """The restart a changed unit triggers is waited out (the
    daemon stops workspaces first) and its failure is named — a
    crash-looping unit must not print as started."""
    debian_host.writes[fixture_unit_path(debian_host)] = "ExecStart=/old\n"
    debian_host.fail_systemctl = {"restart"}
    assert main() == 1
    err = capsys.readouterr().err
    assert "systemctl --user restart msksd.service failed" in err
    assert "journalctl --user -u msksd" in err


def test_a_missing_home_is_a_named_refusal(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Round-3 blocking: the parent-chain walk over a home that
    does not exist raises inside the wrap — the operator reads a
    named refusal, never a traceback out of main."""
    home = tmp_missing_home(monkeypatch, debian_host)
    assert main() == 1
    assert "landing the user unit failed" in capsys.readouterr().err
    assert home  # the failing home was the one under test


def tmp_missing_home(monkeypatch, debian_host):
    import tempfile

    home = Path(tempfile.mkdtemp()) / "ghost"
    monkeypatch.setattr(
        hostsetup, "executing_user", lambda: ("ghost", 1042, 1042, str(home))
    )
    return home


def test_a_config_that_is_a_file_is_a_named_refusal(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """~/.config as a regular file (a leftover, a mistaken
    redirect) refuses by name too — the round-3 live repro's
    second shape."""
    real_home = Path(hostsetup.executing_user()[3])
    (real_home / ".config").write_text("not a directory\n")
    assert main() == 1
    assert "landing the user unit failed" in capsys.readouterr().err


def test_the_kvm_note_needs_a_kvm_group(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A host with no kvm group has no membership the manager
    could predate — the note stays off and the kvm line names the
    stack to install."""
    monkeypatch.setattr(hostsetup, "group_present", lambda name: False)
    assert main() == 0
    out = capsys.readouterr().out
    assert "the manager may predate" not in out
    assert "the kvm group is absent" in out


def test_a_unit_read_error_is_a_named_refusal(
    debian_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Only a missing unit file reads as a first landing; anything
    else (a directory at the path, permissions) is a named
    refusal, never a traceback out of main."""

    def broken_unit_read(path):
        if path.endswith(hostsetup.USER_UNIT_RELATIVE):
            raise IsADirectoryError(path)
        return debian_host.read(path)

    monkeypatch.setattr(hostsetup, "read_file", broken_unit_read)
    assert main() == 1
    assert "landing the user unit failed" in capsys.readouterr().err
