"""``msksd doctor`` (#464): report logic, probes, hints, dispatch."""

import argparse
import subprocess

import msks.server.doctor as doctor_mod
import msks.server.main as main_mod
import pytest
from msks.server.doctor import (
    CheckResult,
    DoctorReport,
    append_failure_block,
    append_result,
    check_binary,
    check_library,
    check_ssh,
    check_tmux,
    detect_package_manager,
    doctor_main,
    format_report,
    install_hint,
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
    assert install_hint("curl", "apk") == "sudo apk add curl"
    assert install_hint("curl", "apk") == "sudo apk add curl"


def test_install_hint_fallbacks() -> None:
    assert install_hint("curl", None) == "install curl"
    hint = install_hint("never-packaged", "dnf")
    assert hint == "sudo dnf install never-packaged"


def test_install_hint_pinned_binaries() -> None:
    assert "secretspec/releases" in install_hint("secretspec", "apt")
    assert "devenv" in install_hint("jscpd", "dnf")
    hint = install_hint("cloud-hypervisor", "apt")
    assert "cloud-hypervisor/releases" in hint
    assert install_hint("ch-remote", None) == install_hint(
        "cloud-hypervisor", None
    )


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
    result = check_binary(
        "curl", ["curl", "--version"], "apt", is_warning=True, use="debug"
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
        if len(cmd) == 3 and cmd[2] == "import netfilterqueue":
            return (0, "", "")
        return (1, "", "nope")

    monkeypatch.setattr(
        doctor_mod.shutil, "which", which_map({"pkg-config", "ldconfig"})
    )
    monkeypatch.setattr(doctor_mod, "run", fake_run)
    assert library_probe_result("libnfnetlink") == "netfilterqueue import"


def test_library_absent_probers_falls_to_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", which_map(set()))
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "", "")
    )
    assert library_probe_result("libnfnetlink") == "netfilterqueue import"


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


# ---------------------------------------------------------------------------
# The check set
# ---------------------------------------------------------------------------


def probes_ok(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """A green probe answer: every binary answers, tmux reports 3.4."""
    if cmd[0].endswith("tmux"):
        return (0, "tmux 3.4", "")
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
        "secretspec",
        "libnetfilter_queue",
        "libnfnetlink",
        "tmux",
    } <= names


def test_run_doctor_warning_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: (
            None
            if name in {"curl", "ssh", "ch-remote"}
            else f"/usr/bin/{name}"
        ),
    )
    monkeypatch.setattr(doctor_mod, "run", probes_ok)
    report = run_doctor(Settings())
    warn_names = {r.name for r in report.warnings}
    assert warn_names == {"curl", "ssh", "ch-remote"}
    assert report.passed is True  # warnings do not fail the run


def test_run_doctor_error_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: None if name == "nft" else f"/usr/bin/{name}",
    )
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
    monkeypatch.setattr(
        doctor_mod, "load_settings", lambda config, generate: Settings()
    )
    assert doctor_main(None) == 0
    out = capsys.readouterr().out
    assert "msksd doctor" in out
    assert "All 19 checks passed." in out


def test_doctor_main_appends_config_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    all_found(monkeypatch)
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
