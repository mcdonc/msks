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
    detect_package_manager,
    doctor_main,
    format_report,
    install_hint,
    library_probe_result,
    result_marker,
    run,
    run_doctor,
    settings_for_doctor,
)
from msks.settings import Settings, VmmSettings

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


def test_install_hint_fallbacks() -> None:
    assert install_hint("curl", None) == "install curl"
    hint = install_hint("never-packaged", "dnf")
    assert hint == "sudo dnf install never-packaged"


def test_install_hint_pinned_binaries() -> None:
    assert "secretspec/releases" in install_hint("secretspec", "apt")
    assert "devenv" in install_hint("jscpd", "dnf")


# ---------------------------------------------------------------------------
# The run helper
# ---------------------------------------------------------------------------


def test_run_reports_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_missing(*a, **kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(doctor_mod.subprocess, "run", raise_missing)
    rc, out, err = run(["ghost", "--version"])
    assert (rc, out) == (-1, "")
    assert "not found" in err


def test_run_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="slow", timeout=10)

    monkeypatch.setattr(doctor_mod.subprocess, "run", raise_timeout)
    rc, _out, err = run(["slow", "--version"])
    assert rc == -1
    assert "timed out" in err


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


def all_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every binary on PATH, every probe green, libraries present."""
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name else None,
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "lib x", "OpenSSH_x")
    )


def test_run_doctor_grades_and_settings_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_found(monkeypatch)
    settings = Settings(vmm=VmmSettings(mkisofs="xmkisofs"))
    report = run_doctor(settings)
    names = {r.name for r in report.results}
    assert "xmkisofs" in names  # the settings name, not the default
    assert report.passed is True
    # The error-grade rows: daemon core paths, both libraries included.
    assert {
        "cloud-hypervisor",
        "ch-remote",
        "mkfs.ext4",
        "nft",
        "conntrack",
        "qemu-img",
        "secretspec",
        "libnetfilter_queue",
        "libnfnetlink",
    } <= names


def test_run_doctor_warning_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: None if name in {"curl", "ssh"} else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "lib x", "")
    )
    report = run_doctor(Settings())
    warn_names = {r.name for r in report.warnings}
    assert warn_names == {"curl", "ssh"}
    assert report.passed is True  # warnings do not fail the run


def test_run_doctor_error_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_mod.shutil,
        "which",
        lambda name: None if name == "nft" else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        doctor_mod, "run", lambda cmd, timeout=10.0: (0, "lib x", "")
    )
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
        raise ValueError("config file not found")

    monkeypatch.setattr(doctor_mod, "load_settings", refuse)
    settings, notice = settings_for_doctor(None)
    assert notice is None
    assert settings == Settings.from_env()


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
    assert "All 18 checks passed." in out


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
    assert "1 passed, 2 errors, 1 warnings" in text
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
    assert "1 passed, 1 errors" in text
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
