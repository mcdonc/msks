"""``msksd doctor`` (#464): report logic, probes, hints, dispatch."""

import argparse
import os
import pathlib
import subprocess
from types import SimpleNamespace

import msks.server.doctor as doctor_mod
import msks.server.hostsetup as hostsetup_mod
import msks.server.main as main_mod
import pytest
from msks.server.doctor import (
    CheckResult,
    DoctorReport,
    append_failure_block,
    append_result,
    apple_silicon_kvm_result,
    check_binary,
    check_library,
    check_ssh,
    check_tmux,
    detect_package_manager,
    doctor_main,
    format_report,
    group_add_hint,
    install_hint,
    kvm_access_result,
    library_probe_result,
    parse_tmux_version,
    result_marker,
    run,
    run_doctor,
    settings_for_doctor,
)
from msks.settings import Settings, VmmSettings
from msks.spec.version import __version__

# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------


def ok(name: str) -> CheckResult:
    return CheckResult(name=name, ok=True, message=f"{name} ok")


def err(name: str) -> CheckResult:
    return CheckResult(name=name, ok=False, message=f"{name} bad")


def warn(name: str) -> CheckResult:
    return CheckResult(
        name=name, ok=False, is_warning=True, message=f"{name} degraded"
    )


def test_report_rollup() -> None:
    report = DoctorReport()
    report.add(ok("a"))
    report.add(warn("b"))
    report.add(err("c"))
    assert report.passed is False
    assert [r.name for r in report.errors] == ["c"]
    assert [r.name for r in report.warnings] == ["b"]


def test_report_passes_with_only_warnings() -> None:
    report = DoctorReport([ok("a"), warn("b")])
    assert report.passed is True
    assert report.errors == []


def test_result_marker() -> None:
    assert result_marker(ok("a")) == "✓"
    assert result_marker(warn("a")) == "⚠"
    assert result_marker(err("a")) == "✗"


# ---------------------------------------------------------------------------
# Package manager detection and hints
# ---------------------------------------------------------------------------


def which_map(present: set[str]):
    def fake_which(binary: str):
        return f"/usr/bin/{binary}" if binary in present else None

    return fake_which


def test_detect_manager_specificity(monkeypatch: pytest.MonkeyPatch) -> None:
    # dnf wins over yum (Fedora ships both); apt-get maps to "apt".
    monkeypatch.setattr(doctor_mod.shutil, "which", which_map({"yum", "dnf"}))
    assert detect_package_manager() == "dnf"
    monkeypatch.setattr(
        doctor_mod.shutil, "which", which_map({"apt-get", "pacman"})
    )
    assert detect_package_manager() == "apt"


def test_detect_manager_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", which_map(set()))
    assert detect_package_manager() is None


def test_install_hint_table_and_managers() -> None:
    hint = install_hint("conntrack", "dnf")
    assert hint == "sudo dnf install conntrack-tools"
    assert install_hint("conntrack", "apt") == "sudo apt install conntrack"
    assert install_hint("mkisofs", "pacman") == "sudo pacman -S cdrtools"
    assert install_hint("mkisofs", "brew") == "brew install cdrtools"
    assert install_hint("rsync", "zypper") == "sudo zypper install rsync"
    assert install_hint("nft", "apk") == "sudo apk add nftables"


def test_install_hint_names_the_kvm_stack_per_manager() -> None:
    """#559: the /dev/kvm rows' stack hint names each manager's
    real package — Arch's split qemu and Alpine's arch-named
    emulator, not a qemu-kvm that neither archive carries."""
    assert install_hint("qemu-kvm", "apt") == "sudo apt install qemu-kvm"
    assert install_hint("qemu-kvm", "pacman") == "sudo pacman -S qemu-desktop"
    assert install_hint("qemu-kvm", "apk") == "sudo apk add qemu-system-x86_64"


def test_install_hint_fallbacks() -> None:
    assert install_hint("mkisofs", None) == "install mkisofs"
    hint = install_hint("never-packaged", "dnf")
    assert hint == "sudo dnf install never-packaged"


def test_install_hint_pinned_binaries() -> None:
    hint = install_hint("cloud-hypervisor", "apt")
    # The upstream hint is the paste-able sequence (#557): fetch
    # the release's static binaries and install each under its
    # unsuffixed name — every line a host command, nothing to
    # look up, no tarball that upstream does not ship.
    assert (
        "curl -LO https://github.com/cloud-hypervisor/cloud-hypervisor"
        "/releases/latest/download/cloud-hypervisor-static" in hint
    )
    assert (
        "curl -LO https://github.com/cloud-hypervisor/cloud-hypervisor"
        "/releases/latest/download/ch-remote-static" in hint
    )
    assert (
        "sudo install -m755 cloud-hypervisor-static"
        " /usr/local/bin/cloud-hypervisor" in hint
    )
    assert (
        "sudo install -m755 ch-remote-static /usr/local/bin/ch-remote" in hint
    )
    assert install_hint("cloud-hypervisor", None) == hint


# ---------------------------------------------------------------------------
# The run helper
# ---------------------------------------------------------------------------


def test_run_reports_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_missing(*a, **kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(doctor_mod.subprocess, "run", raise_missing)
    rc, out, err = run(["ghost", "--version"])
    assert (rc, out) == (-1, "")
    assert err == "ghost: nope"  # the binary and the OS error both land


def test_run_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="slow", timeout=10)

    monkeypatch.setattr(doctor_mod.subprocess, "run", raise_timeout)
    rc, _out, err = run(["slow", "--version"])
    assert rc == -1
    assert "timed out" in err


def test_run_reports_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_exec_format(*a, **kw):
        raise OSError("Exec format error")

    monkeypatch.setattr(doctor_mod.subprocess, "run", raise_exec_format)
    rc, _out, err = run(["/opt/broken/tool", "--version"])
    assert rc == -1
    assert "Exec format error" in err


def test_run_captures_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        returncode = 3
        stdout = "out"
        stderr = "err"

    monkeypatch.setattr(
        doctor_mod.subprocess, "run", lambda *a, **kw: FakeProc()
    )
    assert run(["x"]) == (3, "out", "err")


# ---------------------------------------------------------------------------
# Binary checks
# ---------------------------------------------------------------------------


def test_check_binary_missing_names_use_and_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    # a host whose /usr/sbin really carries the tool must not
    # rescue the miss (the daemon-shaped extension stays out)
    monkeypatch.setattr(doctor_mod, "admin_dir_candidate", lambda name: None)
    result = check_binary(
        "mkisofs", ["mkisofs", "--version"], "apt", use="seed disks (#41)"
    )
    assert result.ok is False
    assert result.is_warning is False
    assert "mkisofs not found on PATH (seed disks (#41))" == result.message
    assert result.hint == "sudo apt install genisoimage"


def test_check_binary_missing_warns_when_graded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    # a host whose /usr/sbin really carries the tool must not
    # rescue the miss (the daemon-shaped extension stays out)
    monkeypatch.setattr(doctor_mod, "admin_dir_candidate", lambda name: None)
    result = check_binary(
        "ssh", ["ssh", "-V"], "apt", is_warning=True, use="debug"
    )
    assert result.is_warning is True


def test_check_binary_path_only_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: "/sbin/resize2fs"
    )
    result = check_binary("resize2fs", [], None)
    assert result.ok is True
    assert "/sbin/resize2fs" in result.message
    assert result.hint == ""


def test_check_binary_probe_failure_carries_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: "/usr/bin/nft"
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (1, "", "boom")
    )
    result = check_binary("nft", ["nft", "--version"], None)
    assert result.ok is False
    assert "probe failed: boom" in result.message


def test_check_binary_probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: "/usr/bin/ip")
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "iproute2", "")
    )
    result = check_binary("ip", ["ip", "-V"], None)
    assert result.ok is True


# ---------------------------------------------------------------------------
# The ssh special case
# ---------------------------------------------------------------------------


def test_check_ssh_missing_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    # a host whose /usr/sbin really carries the tool must not
    # rescue the miss (the daemon-shaped extension stays out)
    monkeypatch.setattr(doctor_mod, "admin_dir_candidate", lambda name: None)
    result = check_ssh(None)
    assert result.ok is False and result.is_warning is True
    assert "msks forward" in result.message


def test_check_ssh_accepts_rc1_version_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: "/usr/bin/ssh"
    )
    monkeypatch.setattr(
        doctor_mod,
        "run",
        lambda cmd, timeout=10.0: (1, "", "OpenSSH_9.6p1, OpenSSL 3"),
    )
    result = check_ssh(None)
    assert result.ok is True


def test_check_ssh_rejects_other_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: "/usr/bin/ssh"
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (1, "", "dropbear")
    )
    result = check_ssh(None)
    assert result.ok is False


def test_check_ssh_stdout_banner_names_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A wrapper printing its banner to stdout with rc 0: the miss
    # names what the probe actually printed, not a bare "exit 0".
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: "/usr/bin/ssh"
    )
    monkeypatch.setattr(
        doctor_mod,
        "run",
        lambda cmd, timeout=10.0: (0, "OpenSSH_9.6 (wrapper)", ""),
    )
    result = check_ssh(None)
    assert result.ok is False
    assert "OpenSSH_9.6 (wrapper)" in result.message


def test_check_ssh_rejects_rc2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: "/usr/bin/ssh"
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (2, "", "OpenSSH_9")
    )
    result = check_ssh(None)
    assert result.ok is False


# ---------------------------------------------------------------------------
# The tmux floor
# ---------------------------------------------------------------------------


def test_parse_tmux_version() -> None:
    assert parse_tmux_version("tmux 3.6a") == (3, 6)
    assert parse_tmux_version("tmux 3.2") == (3, 2)
    assert parse_tmux_version("weird output") is None


def tmux_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", lambda name: "/usr/bin/tmux"
    )


def test_check_tmux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    # a host whose admin dirs really carry the tool must not
    # rescue the miss (merged-usr Fedora shows /usr/bin under
    # /usr/sbin); the daemon-shaped extension stays out
    monkeypatch.setattr(doctor_mod, "admin_dir_candidate", lambda name: None)
    result = check_tmux("apt")
    assert result.ok is False and result.is_warning is False
    assert result.hint == "sudo apt install tmux"


def test_check_tmux_ok_at_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux_on_path(monkeypatch)
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "tmux 3.2", "")
    )
    result = check_tmux(None)
    assert result.ok is True
    assert "3.2" in result.message


def test_check_tmux_old_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux_on_path(monkeypatch)
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "tmux 3.1b", "")
    )
    result = check_tmux(None)
    assert result.ok is False and result.is_warning is False
    assert "display-popup" in result.message


def test_check_tmux_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux_on_path(monkeypatch)
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "mystery", "")
    )
    result = check_tmux(None)
    assert result.ok is False
    assert "unparseable (mystery)" in result.message


def test_check_tmux_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux_on_path(monkeypatch)
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (1, "", "denied")
    )
    result = check_tmux(None)
    assert result.ok is False
    assert "tmux -V failed" in result.message


# ---------------------------------------------------------------------------
# Library checks
# ---------------------------------------------------------------------------


def test_library_found_by_pkg_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", which_map({"pkg-config", "ldconfig"})
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "", "")
    )
    result = check_library("libnetfilter_queue", None)
    assert result.ok is True
    assert "pkg-config" in result.message


def test_library_pkg_config_miss_falls_to_ldconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def fake_run(cmd, timeout=10.0):
        seen.append(cmd[0])
        if cmd[0].endswith("pkg-config"):
            return (1, "", "")
        return (0, "libnetfilter_queue.so.1", "")

    monkeypatch.setattr(
        doctor_mod.shutil, "which", which_map({"pkg-config", "ldconfig"})
    )
    monkeypatch.setattr(doctor_mod, "run", fake_run)
    result = check_library("libnetfilter_queue", None)
    assert result.ok is True
    assert "ldconfig" in result.message


def test_library_probers_miss_falls_to_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd, timeout=10.0):
        if len(cmd) == 3 and "CDLL" in cmd[2]:
            return (0, "", "")
        return (1, "", "nope")

    monkeypatch.setattr(
        doctor_mod.shutil, "which", which_map({"pkg-config", "ldconfig"})
    )
    monkeypatch.setattr(doctor_mod, "run", fake_run)
    assert library_probe_result("libnfnetlink") == "ctypes load"


def test_library_absent_probers_falls_to_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", which_map(set()))
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "", "")
    )
    assert library_probe_result("libnfnetlink") == "ctypes load"


def test_library_missing_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil, "which", which_map({"pkg-config", "ldconfig"})
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (1, "", "no")
    )
    result = check_library("libnetfilter_queue", "apt")
    assert result.ok is False
    assert result.hint == "sudo apt install libnetfilter-queue1"


def test_nfq_load_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "", "")
    )
    result = doctor_mod.check_nfq_load(None)
    assert result.ok is True
    assert "ctypes load" in result.message


def test_nfq_load_miss_names_the_runtime_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load miss names the runtime soname package for the
    detected manager — nothing compiles anymore, so no header
    package rides the fix (#542)."""

    def refuse(cmd, timeout=10.0):
        if "CDLL" in cmd[2]:
            return (1, "", "OSError")
        return (0, "", "")

    monkeypatch.setattr(doctor_mod, "run", refuse)
    result = doctor_mod.check_nfq_load("apt")
    assert result.ok is False
    assert result.hint == "sudo apt install libnetfilter-queue1"


def test_nfq_load_miss_without_a_manager_names_the_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (1, "", "")
    )
    result = doctor_mod.check_nfq_load(None)
    assert result.ok is False
    assert "libnetfilter_queue" in result.message


def test_nfq_load_off_linux_names_the_platform_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off Linux the library is a Linux package, so the miss names
    the platform contract — no package hint can fix a Mac
    (#542)."""
    monkeypatch.setattr(doctor_mod.sys, "platform", "darwin")
    result = doctor_mod.check_nfq_load("brew")
    assert result.ok is False
    assert "Linux library" in result.message
    assert result.hint == "run msksd on a Linux host"


def test_probe_which_finds_sbin_tools_the_session_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon-shaped probe (#601): a tool the session's PATH
    cannot see but /usr/sbin carries answers, so a login shell
    without the admin directories still reports the daemon's
    truth; an explicit settings path is never extended."""
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "PATH_EXTRA_DIRS", ("/mock/sbin",))
    root = pathlib.Path("/mock/sbin")
    real_is_file = pathlib.Path.is_file
    real_access = os.access

    monkeypatch.setattr(
        pathlib.Path,
        "is_file",
        lambda self: str(self).startswith(str(root)) or real_is_file(self),
    )
    monkeypatch.setattr(
        doctor_mod.os,
        "access",
        lambda path, mode: (
            str(path).startswith(str(root)) or real_access(path, mode)
        ),
    )
    assert doctor_mod.probe_which("nft") == "/mock/sbin/nft"
    assert doctor_mod.probe_which("/opt/nft") is None  # explicit path
    assert doctor_mod.rescued_from_admin_dirs("nft") is True


def test_the_real_probe_execs_a_rescued_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The real run(), no rigging: a tool visible only through the
    admin directories execs by its resolved path and the row
    reads green (#601 review round 2). A silent revert to the
    bare name fails this test with the false error the round
    found."""
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    tool = sbin / "faketool"
    tool.write_text("#!/bin/sh\necho 'faketool 9.9'\n")
    tool.chmod(0o755)
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "PATH_EXTRA_DIRS", (str(sbin),))
    monkeypatch.delenv("PATH", raising=False)
    result = doctor_mod.check_binary(
        "faketool", ["faketool", "--version"], None
    )
    assert result.ok, result.message
    assert result.message == f"faketool ok ({tool})"


def test_session_lacks_admin_dirs_reads_the_session_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    assert doctor_mod.session_lacks_admin_dirs() is True
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/sbin:/usr/bin")
    assert doctor_mod.session_lacks_admin_dirs() is False


def test_a_rescued_tool_adds_the_relogin_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session without the admin directories whose tools only
    the daemon-shaped probe found gains the PATH warning row
    (#601) — a warning, never an error, and never when the
    session already carries the directories."""
    all_found(monkeypatch)  # session which() finds everything
    kvm_seam(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    report = run_doctor(Settings())
    names = [r.name for r in report.results]
    assert "PATH" not in names  # nothing rescued: the session sees all

    # Now only the daemon-shaped probe finds the sbin trio.
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: (
            None
            if name in {"nft", "conntrack", "mkfs.ext4"}
            else f"/usr/bin/{name}"
        ),
    )
    monkeypatch.setattr(doctor_mod, "PATH_EXTRA_DIRS", ("/mock/sbin",))
    real_is_file = pathlib.Path.is_file
    monkeypatch.setattr(
        pathlib.Path,
        "is_file",
        lambda self: str(self).startswith("/mock/sbin") or real_is_file(self),
    )
    real_access = os.access
    monkeypatch.setattr(
        doctor_mod.os,
        "access",
        lambda path, mode: (
            str(path).startswith("/mock/sbin") or real_access(path, mode)
        ),
    )
    report = run_doctor(Settings())
    row = next(r for r in report.results if r.name == "PATH")
    assert row.is_warning and not row.ok
    assert "log out and back in" in row.hint
    assert report.passed is True  # the note never fails the run

    # A session that carries the directories gets no note at all.
    monkeypatch.setattr(doctor_mod, "PATH_EXTRA_DIRS", ("/usr/sbin", "/sbin"))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/sbin:/usr/bin")
    report = run_doctor(Settings())
    assert "PATH" not in [r.name for r in report.results]


def test_run_doctor_off_linux_skips_the_library_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off Linux the two library rows stay off the report — their
    install candidates there are Linux-only packages — and the
    binding row carries the contract (#542)."""
    all_found(monkeypatch)
    monkeypatch.setattr(doctor_mod.sys, "platform", "darwin")
    report = run_doctor(Settings())
    names = {r.name for r in report.results}
    assert "libnetfilter_queue" not in names
    assert "libnfnetlink" not in names
    assert "nfq-binding" in names


# ---------------------------------------------------------------------------
# The /dev/kvm gate (#559)
# ---------------------------------------------------------------------------


def kvm_seam(
    monkeypatch: pytest.MonkeyPatch,
    *,
    member: bool = True,
    session_carries: bool = True,
    sudo_lifted: bool = False,
) -> None:
    """Pin the kvm row's seams: a daemon user `op` (uid 1000), a
    kvm group at gid 108, a node that exists, and the two
    database answers the row reads. *session_carries* says the
    live session holds the gid (supplementary list or primary);
    *sudo_lifted* makes the effective uid root's, the run that
    cannot read the daemon user's session."""
    monkeypatch.setattr(
        doctor_mod, "executing_user", lambda: ("op", 1000, 1000, "/home/op")
    )
    monkeypatch.setattr(doctor_mod, "group_present", lambda name: True)
    monkeypatch.setattr(doctor_mod, "in_group", lambda user, name: member)
    monkeypatch.setattr(
        doctor_mod.os.path, "exists", lambda path: path == "/dev/kvm"
    )
    monkeypatch.setattr(
        doctor_mod.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=108),
    )
    monkeypatch.setattr(
        doctor_mod.os, "geteuid", lambda: 0 if sudo_lifted else 1000
    )
    monkeypatch.setattr(
        doctor_mod.os,
        "getgroups",
        lambda: [108] if session_carries else [24, 27],
    )
    monkeypatch.setattr(doctor_mod.os, "getgid", lambda: 1000)


def apple_seam(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    midr: str | None,
    machine: str = "aarch64",
    *,
    cmdline: str | None = None,
    mode_param: str | None = None,
    kernel_log: str | None = None,
) -> None:
    """The Apple-kvm row's seam: the platform, the machine, and
    cpu0's MIDR as sysfs presents it (0x-prefixed hex, or no file
    at all on a host that publishes none), plus the three NV-mode
    markers (#664) — the kernel command line, sysfs's kvm mode
    parameter, and the kernel log — each left absent by default,
    so no test reads the host's real markers. The kernel-log seam
    wraps whatever `run` a prior seam installed, delegating every
    non-dmesg probe to it."""
    monkeypatch.setattr(doctor_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        doctor_mod.os, "uname", lambda: SimpleNamespace(machine=machine)
    )
    midr_path = tmp_path / "midr_el1"
    if midr is not None:
        midr_path.write_text(midr)
    monkeypatch.setattr(doctor_mod, "MIDR_EL1", midr_path)
    cmdline_path = tmp_path / "cmdline"
    if cmdline is not None:
        cmdline_path.write_text(cmdline)
    monkeypatch.setattr(doctor_mod, "PROC_CMDLINE", cmdline_path)
    param_path = tmp_path / "kvm-mode"
    if mode_param is not None:
        param_path.write_text(mode_param)
    monkeypatch.setattr(doctor_mod, "KVM_MODE_PARAM", param_path)
    outer_run = doctor_mod.run

    def probe(cmd: list[str], timeout: float = 10.0):
        if cmd[:1] == ["dmesg"]:
            if kernel_log is None:
                # the restricted-log shape: unreadable reads as
                # absent, never as a failure
                return (1, "", "dmesg: read kernel buffer failed")
            return (0, kernel_log, "")
        return outer_run(cmd, timeout)

    monkeypatch.setattr(doctor_mod, "run", probe)


def test_the_apple_kvm_row_warns_on_apple_silicon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """An Apple implementer in cpu0's MIDR with the kernel's KVM
    in NV mode earns the #649 warning: the row names the stall
    (kernels boot, ssh never answers) and grades as a warning, so
    the report still passes — everything short of a guest boot
    works on such a host."""
    # cpu0's actual MIDR on the M3 the #649 evidence came from,
    # with the option line the Asahi bootloader sets.
    apple_seam(
        monkeypatch,
        tmp_path,
        "0x00000000612f0430",
        cmdline="BOOT_IMAGE=/vmlinuz root=UUID=x kvm-arm.mode=nested",
    )
    result = apple_silicon_kvm_result()
    assert result is not None and not result.ok
    assert result.is_warning
    assert result.name == "apple-kvm"
    assert "#649" in result.message
    assert "ssh" in result.message


def test_the_apple_kvm_row_needs_the_nv_mode_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Apple silicon alone earns no row (#664): macOS's
    Virtualization.framework passes the host MIDR through to its
    Linux guests, and a VZ-nested VM's own KVM delivers guest
    interrupts — workspaces boot and ssh answers there — so the
    row stays off until the NV-mode markers name the Asahi
    kernel."""
    apple_seam(monkeypatch, tmp_path, "0x00000000612f0430")
    assert apple_silicon_kvm_result() is None


def test_the_apple_kvm_row_fires_on_the_kernel_log_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The running mode is its own marker: a kernel that selected
    NV mode without a command-line option still names it in
    KVM's init line — the `VHE+NV2 mode initialized` the #649
    retest recorded on the Asahi host."""
    apple_seam(
        monkeypatch,
        tmp_path,
        "0x00000000612f0430",
        kernel_log=(
            "[    0.000000] kvm [1]: VHE+NV2 mode initialized\n"
            "[    0.000000] GICv3: no GICV resource entry\n"
        ),
    )
    result = apple_silicon_kvm_result()
    assert result is not None and result.is_warning


def test_the_apple_kvm_row_fires_on_the_module_parameter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A kernel that publishes kvm's mode parameter through
    sysfs answers the same question there — the module-parameter
    spelling #664's fix direction names."""
    apple_seam(
        monkeypatch,
        tmp_path,
        "0x00000000612f0430",
        mode_param="nested\n",
    )
    result = apple_silicon_kvm_result()
    assert result is not None and result.is_warning


def test_the_apple_kvm_row_ignores_a_non_nv_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Every marker present and none naming nested — a
    kvm-arm.mode set to a non-NV value, sysfs's parameter at
    nvhe, a kernel log whose KVM init names plain VHE (the
    VZ-nested VM's shape) — leaves the row off: only the
    nested mode carries the #649 limitation."""
    apple_seam(
        monkeypatch,
        tmp_path,
        "0x00000000612f0430",
        cmdline="BOOT_IMAGE=/vmlinuz kvm-arm.mode=none",
        mode_param="nvhe\n",
        kernel_log="[    0.000000] kvm [1]: VHE mode initialized\n",
    )
    assert apple_silicon_kvm_result() is None


def test_the_apple_kvm_row_survives_a_garbage_midr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A MIDR file whose text is not hex parses to no marker:
    the row stays off rather than guessing, whatever the NV
    markers say."""
    apple_seam(
        monkeypatch,
        tmp_path,
        "garbage",
        cmdline="BOOT_IMAGE=/vmlinuz kvm-arm.mode=nested",
    )
    assert apple_silicon_kvm_result() is None


def test_the_apple_kvm_row_stays_off_other_arm64_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """An ARM-implementer MIDR (a Neoverse server) earns no row:
    the warning is about Apple Silicon's kernel, not arm64."""
    apple_seam(monkeypatch, tmp_path, "0x00000000411fd480")
    assert apple_silicon_kvm_result() is None


def test_the_apple_kvm_row_stays_off_without_the_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A host that publishes no MIDR earns no row — the marker is
    the whole claim, and an exotic arm64 platform gets silence
    rather than a guess."""
    apple_seam(monkeypatch, tmp_path, None)
    assert apple_silicon_kvm_result() is None


def test_the_apple_kvm_row_stays_off_x86_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """x86_64 machines earn no row before the MIDR is ever read."""
    apple_seam(monkeypatch, tmp_path, "0x00000000612f0430", machine="x86_64")
    assert apple_silicon_kvm_result() is None


def test_the_kvm_row_passes_for_a_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kvm_seam(monkeypatch)
    result = kvm_access_result("apt")
    assert result.name == "/dev/kvm"
    assert result.ok and "op in kvm" in result.message


def test_the_kvm_row_passes_when_the_primary_group_is_kvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary-group membership counts (in_group's second gate,
    the #555 posture): the session carries the gid as its
    primary, the supplementary list stays empty."""
    kvm_seam(monkeypatch)
    monkeypatch.setattr(doctor_mod.os, "getgroups", lambda: [])
    monkeypatch.setattr(doctor_mod.os, "getgid", lambda: 108)
    result = kvm_access_result("apt")
    assert result.ok and "op in kvm" in result.message


def test_the_kvm_row_errors_when_the_user_is_not_a_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kvm_seam(monkeypatch, member=False)
    result = kvm_access_result("apt")
    assert result.ok is False and result.is_warning is False
    assert "op is not in the kvm group" in result.message
    assert result.hint == "sudo adduser op kvm"


def test_the_kvm_group_add_hint_matches_the_manager() -> None:
    assert group_add_hint("op", "dnf") == "sudo usermod -aG kvm op"
    assert group_add_hint("op", "zypper") == "sudo usermod -aG kvm op"
    assert group_add_hint("op", "pacman") == "sudo gpasswd -a op kvm"
    assert group_add_hint("op", "apk") == "sudo addgroup op kvm"
    # No manager detected falls back to the Debian form — the
    # documented deployment host.
    assert group_add_hint("op", None) == "sudo adduser op kvm"


def test_the_kvm_row_names_a_session_that_predates_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database says member, the live session says no — the
    group landed after this login, and a daemon started from the
    session still meets EACCES (#559's second acceptance)."""
    kvm_seam(monkeypatch, session_carries=False)
    result = kvm_access_result("apt")
    assert result.ok is False and result.is_warning is False
    assert "predates the membership" in result.message
    assert "newgrp kvm" in result.hint


def test_a_sudo_lifted_run_stands_on_the_database_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root session cannot speak for the daemon user's login:
    the live-group line stays off, and the database's yes passes
    even though root's own groups lack the gid."""
    kvm_seam(monkeypatch, session_carries=False, sudo_lifted=True)
    result = kvm_access_result("apt")
    assert result.ok and "op in kvm" in result.message


def test_the_kvm_row_names_the_missing_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host without /dev/kvm (a VM without nested virt) reports
    the node's absence — its own message, never a group
    failure."""
    kvm_seam(monkeypatch)
    monkeypatch.setattr(doctor_mod.os.path, "exists", lambda path: False)
    result = kvm_access_result("apt")
    assert result.ok is False
    assert "/dev/kvm is absent" in result.message
    assert "group" not in result.message
    assert result.hint == "sudo apt install qemu-kvm"


def test_the_kvm_row_names_the_missing_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kvm_seam(monkeypatch)
    monkeypatch.setattr(doctor_mod, "group_present", lambda name: False)
    result = kvm_access_result("apt")
    assert result.ok is False
    assert "kvm group is absent" in result.message
    assert result.hint == "sudo apt install qemu-kvm"


def test_run_doctor_carries_the_kvm_row_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row rides beside the cloud-hypervisor check — the VMM
    opens the node the group gates (#559)."""
    all_found(monkeypatch)
    kvm_seam(monkeypatch)
    report = run_doctor(Settings())
    names = [r.name for r in report.results]
    assert "/dev/kvm" in names
    assert names.index("/dev/kvm") == names.index("cloud-hypervisor") + 1


def test_run_doctor_off_linux_skips_the_kvm_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is Linux-only like the consent stack (#542):
    /dev/kvm is a Linux node, and the group-add commands the row
    hints at prescribe Linux ones elsewhere."""
    all_found(monkeypatch)
    kvm_seam(monkeypatch)
    monkeypatch.setattr(doctor_mod.sys, "platform", "darwin")
    report = run_doctor(Settings())
    assert "/dev/kvm" not in {r.name for r in report.results}


# ---------------------------------------------------------------------------
# The check set
# ---------------------------------------------------------------------------


def probes_ok(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """A green probe answer: every binary answers, tmux reports
    3.4, virtiofsd reports a floor-clearing version."""
    if cmd[0].endswith("tmux"):
        return (0, "tmux 3.4", "")
    if cmd[0].endswith("virtiofsd"):
        return (0, "virtiofsd 1.13.3", "")
    return (0, "lib x", "OpenSSH_x")


def all_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every binary on PATH, every probe green, libraries present."""
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name else None,
    )
    monkeypatch.setattr(doctor_mod, "run", probes_ok)


def test_run_doctor_grades_and_settings_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_found(monkeypatch)
    kvm_seam(monkeypatch)  # the kvm row's answer is host state
    settings = Settings(vmm=VmmSettings(mkisofs="xmkisofs"))
    report = run_doctor(settings)
    names = {r.name for r in report.results}
    assert "xmkisofs" in names  # the settings name, not the default
    assert report.passed is True
    # The error-grade rows: daemon core paths, both libraries and
    # the tmux floor included.
    assert {
        "cloud-hypervisor",
        "mkfs.ext4",
        "nft",
        "conntrack",
        "qemu-img",
        "libnetfilter_queue",
        "libnfnetlink",
        "nfq-binding",
        "tmux",
    } <= names


def test_a_missing_vmm_report_prints_the_install_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cloud-hypervisor miss names the fix the operator can
    paste: the three commands the static release needs, in the
    result list and in the closing error block (#557)."""

    def without_vmm(name):
        return None if name == "cloud-hypervisor" else f"/usr/bin/{name}"

    monkeypatch.setattr(doctor_mod.shutil, "which", without_vmm)
    # a host whose admin dirs really carry the VMM must not
    # rescue the miss (the landed /usr/local/bin copy shows under
    # a symlinked /usr/local/sbin); the extension stays out
    monkeypatch.setattr(doctor_mod, "admin_dir_candidate", lambda name: None)
    monkeypatch.setattr(doctor_mod, "run", probes_ok)
    monkeypatch.setattr(doctor_mod, "detect_package_manager", lambda: "apt")
    report = run_doctor(Settings())
    assert any(
        r.name == "cloud-hypervisor" for r in report.errors
    )  # the miss is error-grade: the daemon execs the VMM
    text = format_report(report)
    assert "    Run:  curl -LO " in text
    assert (
        "          sudo install -m755 cloud-hypervisor-static"
        " /usr/local/bin/cloud-hypervisor" in text
    )
    assert (
        "          sudo install -m755 ch-remote-static"
        " /usr/local/bin/ch-remote" in text
    )
    assert text.count("curl -LO ") == 4  # two assets, row and block


def test_path_note_hints_the_distros_own_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The relogin hint names the setup tool os-release identifies
    (#635): the Debian and Fedora entries by name, and a distro
    neither targets keeps the export alone."""
    release = tmp_path / "os-release"

    def distro(identifier: str) -> None:
        release.write_text(f"ID={identifier}\n")
        monkeypatch.setattr(hostsetup_mod, "OS_RELEASE", str(release))

    distro("fedora")
    assert "(msks-setup-fedora-host adds" in doctor_mod.path_note_result().hint
    distro("fedora-asahi-remix")
    assert "(msks-setup-fedora-host adds" in doctor_mod.path_note_result().hint
    distro("debian")
    assert "(msks-setup-debian-host adds" in doctor_mod.path_note_result().hint
    distro("ubuntu")
    assert "(msks-setup-debian-host adds" in doctor_mod.path_note_result().hint
    distro("linuxmint")
    assert "(msks-setup-debian-host adds" in doctor_mod.path_note_result().hint
    distro("arch")
    assert doctor_mod.path_note_result().hint == (
        "log out and back in, or export PATH=$PATH:/usr/sbin:/sbin"
    )


def test_run_doctor_carries_the_apple_kvm_row_where_it_applies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The wiring (#649, tightened by #664): when cpu0's MIDR
    names Apple and the kernel's KVM runs in NV mode, the full
    run carries the apple-kvm warning and the report still
    passes. The seam pins the platform, the machine, the MIDR,
    and the NV-mode markers, so the row fires identically on
    x86_64 CI and on an Apple dev machine — where the unseamed
    row would otherwise cover this line only by host accident."""
    all_found(monkeypatch)
    kvm_seam(monkeypatch)
    apple_seam(
        monkeypatch,
        tmp_path,
        "0x00000000612f0430",
        cmdline="BOOT_IMAGE=/vmlinuz kvm-arm.mode=nested",
    )
    report = run_doctor(Settings())
    names = {result.name for result in report.warnings}
    assert "apple-kvm" in names
    assert report.passed is True  # the warning does not fail the run


def test_run_doctor_warning_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: None if name in {"ssh", "rsync"} else f"/usr/bin/{name}",
    )
    # a host whose /usr/sbin really carries the pair must not
    # rescue the miss (the daemon-shaped extension stays out)
    monkeypatch.setattr(doctor_mod, "admin_dir_candidate", lambda name: None)
    monkeypatch.setattr(doctor_mod, "run", probes_ok)
    kvm_seam(monkeypatch)  # the kvm row's answer is host state
    # The apple-kvm row reads cpu0's MIDR — real host state on an
    # Apple dev machine; the full-run tests pin it off so the
    # report is the same on every host.
    monkeypatch.setattr(
        doctor_mod, "MIDR_EL1", pathlib.Path("/nonexistent-midr")
    )
    report = run_doctor(Settings())
    warn_names = {r.name for r in report.warnings}
    assert warn_names == {"ssh", "rsync"}
    assert report.passed is True  # warnings do not fail the run


def test_run_doctor_error_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: None if name == "nft" else f"/usr/bin/{name}",
    )
    # The daemon-shaped extension stays out: a host whose /usr/sbin
    # really carries nft (CI runners) must not rescue the miss.
    monkeypatch.setattr(doctor_mod, "admin_dir_candidate", lambda name: None)
    kvm_seam(monkeypatch)  # the kvm row's answer is host state
    monkeypatch.setattr(doctor_mod, "run", probes_ok)
    report = run_doctor(Settings())
    assert report.passed is False
    assert [r.name for r in report.errors] == ["nft"]


# ---------------------------------------------------------------------------
# Settings resolution and the entry
# ---------------------------------------------------------------------------


def test_settings_for_doctor_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    monkeypatch.setattr(
        doctor_mod, "load_settings", lambda config, generate: settings
    )
    assert settings_for_doctor("x.yaml") == (settings, None)


def test_settings_for_doctor_default_path_miss_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(config, generate):
        raise ValueError(
            "config file not found: /home/u/.config/msksd/msksd.yaml"
        )

    monkeypatch.setattr(doctor_mod, "load_settings", refuse)
    settings, notice = settings_for_doctor(None)
    assert notice is None
    assert settings == Settings.from_env()


def test_settings_for_doctor_broken_default_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A default config that exists but will not load is a finding:
    # doctor would otherwise go green over names the daemon refuses.
    def refuse(config, generate):
        raise ValueError("bad yaml at line 3")

    monkeypatch.setattr(doctor_mod, "load_settings", refuse)
    settings, notice = settings_for_doctor(None)
    assert notice is not None and notice.is_warning
    assert "bad yaml" in notice.message


def test_settings_for_doctor_explicit_failure_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(config, generate):
        raise ValueError("bad yaml")

    monkeypatch.setattr(doctor_mod, "load_settings", refuse)
    settings, notice = settings_for_doctor("/tmp/msksd.yaml")
    assert notice is not None and notice.is_warning
    assert "bad yaml" in notice.message


def test_doctor_main_exit_codes_and_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    all_found(monkeypatch)
    kvm_seam(monkeypatch)  # the kvm row's answer is host state
    # Ditto the apple-kvm row's MIDR: pinned off so the check
    # count (17) holds on Apple dev machines.
    monkeypatch.setattr(
        doctor_mod, "MIDR_EL1", pathlib.Path("/nonexistent-midr")
    )
    monkeypatch.setattr(
        doctor_mod, "load_settings", lambda config, generate: Settings()
    )
    assert doctor_main(None) == 0
    out = capsys.readouterr().out
    assert "msksd doctor" in out
    assert "All 17 checks passed." in out


def test_doctor_main_appends_config_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    all_found(monkeypatch)
    kvm_seam(monkeypatch)  # the kvm row's answer is host state
    monkeypatch.setattr(
        doctor_mod,
        "load_settings",
        lambda config, generate: (_ for _ in ()).throw(ValueError("boom")),
    )
    assert doctor_main("/tmp/msksd.yaml") == 0  # a warning still passes
    out = capsys.readouterr().out
    assert "config load failed (boom)" in out


def test_doctor_main_error_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (1, "", "no")
    )
    monkeypatch.setattr(
        doctor_mod, "load_settings", lambda config, generate: Settings()
    )
    assert doctor_main(None) == 1


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def test_append_result_with_and_without_hint() -> None:
    lines: list[str] = []
    append_result(lines, ok("a"))
    append_result(
        lines,
        CheckResult(name="b", ok=False, message="gone", hint="install b"),
    )
    assert lines == ["  ✓ a: a ok", "  ✗ b: gone", "    Run:  install b"]


def test_append_failure_block_skips_empty() -> None:
    lines: list[str] = []
    append_failure_block(lines, [], "✗", "Errors:")
    assert lines == []


def test_a_multiline_hint_aligns_its_commands() -> None:
    """An upstream hint is a command sequence — the first line
    carries the Run: label, the rest align beneath it so the
    block pastes as written (#557), in the result list and in the
    closing failure block alike."""
    result = CheckResult(
        name="cloud-hypervisor",
        ok=False,
        message="missing",
        hint="curl -LO url\ntar -xf t\nsudo install -m755 x y /usr/local/bin/",
    )
    lines: list[str] = []
    append_result(lines, result)
    assert lines == [
        "  ✗ cloud-hypervisor: missing",
        "    Run:  curl -LO url",
        "          tar -xf t",
        "          sudo install -m755 x y /usr/local/bin/",
    ]
    block: list[str] = []
    append_failure_block(
        block,
        [
            CheckResult(
                name="cloud-hypervisor",
                ok=False,
                message="missing",
                hint="curl -LO url\ntar -xf t",
            )
        ],
        "✗",
        "Errors:",
    )
    assert "    Run:  curl -LO url" in block
    assert "          tar -xf t" in block


def test_format_report_all_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod, "detect_package_manager", lambda: "apt")
    report = DoctorReport([ok("a"), ok("b")])
    assert "All 2 checks passed." in format_report(report)


def test_format_report_counts_and_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod, "detect_package_manager", lambda: None)
    report = DoctorReport(
        [
            ok("a"),
            warn("b"),
            err("c"),
            CheckResult(name="d", ok=False, message="bad d", hint="fix d"),
        ]
    )
    text = format_report(report)
    assert "1 passed, 2 errors, 1 warning" in text
    assert "(none detected)" in text
    assert "Errors (the daemon needs these):" in text
    assert "Warnings (degraded paths, not core):" in text
    # The failure block repeats each miss with its fix.
    assert "✗ c: c bad" in text
    assert "Run:  fix d" in text


def test_format_report_errors_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod, "detect_package_manager", lambda: "apt")
    report = DoctorReport([ok("a"), err("c")])
    text = format_report(report)
    assert "1 passed, 1 error" in text
    assert "Warnings" not in text


# ---------------------------------------------------------------------------
# Entry-point dispatch (msksd doctor / serve)
# ---------------------------------------------------------------------------


def test_parser_accepts_flags_before_and_after_subcommand() -> None:
    parser = main_mod.build_parser()
    # bpo-9351 pin: a value the main parser consumed survives the
    # serve subparser applying its own defaults.
    args = parser.parse_args(["--config=none", "serve"])
    assert args.command == "serve" and args.config == "none"
    args = parser.parse_args(["serve", "--config=none", "--no-tls"])
    assert args.config == "none" and args.no_tls is True
    # The bare invocation keeps every legacy flag.
    args = parser.parse_args(["--no-tls"])
    assert args.command is None and args.no_tls is True
    # A parse that saw no flags leaves SUPPRESS gaps; main() fills
    # them (the serve dispatch tests pin that path).
    args = parser.parse_args([])
    assert args.command is None
    assert not hasattr(args, "config")


def test_version_on_every_parser(
    capsys: pytest.CaptureFixture,
) -> None:
    parser = main_mod.build_parser()
    for argv in (
        ["--version"],
        ["serve", "--version"],
        ["doctor", "--version"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(argv)
        assert excinfo.value.code == 0
        assert __version__ in capsys.readouterr().out


def test_doctor_rejects_serve_flags() -> None:
    parser = main_mod.build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["doctor", "--no-tls"])
    assert excinfo.value.code == 2


def test_main_dispatches_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    def fake_doctor(config):
        calls["config"] = config
        return 7

    monkeypatch.setattr(doctor_mod, "doctor_main", fake_doctor)
    assert main_mod.main(["doctor", "--config=none"]) == 7
    assert calls == {"config": "none"}


def test_main_legacy_flags_still_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = {}
    monkeypatch.setattr(
        main_mod, "load_settings", lambda config, generate=True: Settings()
    )
    monkeypatch.setattr(
        main_mod,
        "serve",
        lambda app, no_tls: served.update(no_tls=no_tls, app=app),
    )
    assert main_mod.main(["--no-tls", "--config=none"]) == 0
    assert served["no_tls"] is True
    assert served["app"].state.settings.server.tls_cert is None


def test_main_serve_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    served = {}
    monkeypatch.setattr(
        main_mod, "load_settings", lambda config, generate=True: Settings()
    )
    monkeypatch.setattr(
        main_mod, "serve", lambda app, no_tls: served.update(no_tls=no_tls)
    )
    assert main_mod.main(["serve", "--no-tls", "--config=none"]) == 0
    assert served == {"no_tls": True}


def test_main_bare_invocation_fills_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = {}
    monkeypatch.setattr(
        main_mod, "load_settings", lambda config, generate=True: Settings()
    )
    monkeypatch.setattr(main_mod, "arm_tls", lambda app, no_tls: None)
    monkeypatch.setattr(
        main_mod,
        "serve",
        lambda app, no_tls: served.update(no_tls=no_tls, config="seen"),
    )
    assert main_mod.main([]) == 0
    assert served == {"no_tls": False, "config": "seen"}


def test_shared_flags_missing_values_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SUPPRESS defaults leave gaps the entry fills in — a direct
    # parse must not see attributes the subparser never set.
    parser = argparse.ArgumentParser(parents=[main_mod.SHARED_FLAGS])
    args = parser.parse_args([])
    assert not hasattr(args, "config")
