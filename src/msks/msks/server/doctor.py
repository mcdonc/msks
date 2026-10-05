"""``msksd doctor`` — pre-flight host dependency checker (#464).

The daemon drives tools outside its Python environment: the
cloud-hypervisor VMM (#1), the e2fsprogs pair that grows and
checks workspace volumes (#184), the
``mkisofs`` that packs the #41 cidata seed disks, the iproute2 /
nftables / conntrack trio that wires and polices each workspace's
tap (#52), ``qemu-img`` for the root overlay, and the secretspec
CLI behind the #198 secret store. A missing tool surfaces today as
a late runtime failure with an opaque error; doctor names each one
before that, with an install hint matched to the detected package
manager.

Design (ported from klangkd doctor, klangk #1612): capabilities
first, never platform predictions. Every check runs on every host;
only the package-hint table varies by detected manager. Checks are
graded — an error keeps the daemon from its core paths, a warning
names a development-time or client-side tool the daemon itself
runs without). Exit code is 0 when every check passes or only
warns, 1 when any check errors.

Tool names come from the daemon's own settings wherever a setting
exists (``load_settings``, env vars included), so doctor checks
what this host's daemon will actually exec; a config that fails to
load falls back to env-plus-defaults with a warning result naming
the error.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from ..config import load_settings
from ..settings import Settings

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """One check's outcome; ``is_warning`` grades the failure."""

    name: str
    ok: bool
    message: str
    is_warning: bool = False  # False = error (core path), True = warning
    hint: str = ""


@dataclass
class DoctorReport:
    """The checks doctor ran, with the pass/fail roll-up."""

    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def passed(self) -> bool:
        return all(r.ok or r.is_warning for r in self.results)

    @property
    def errors(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and not r.is_warning]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and r.is_warning]


# ---------------------------------------------------------------------------
# Package manager detection (by binary presence, not /etc/os-release)
# ---------------------------------------------------------------------------

# Ordered by specificity: dnf before yum (Fedora ships both),
# apt-get before apt (scripts prefer apt-get for non-interactive
# use). The name recorded is the manager's own — "apt", not
# "apt-get" — because the hint table keys on it.
MANAGER_PROBE_ORDER = [
    ("dnf", "dnf"),
    ("yum", "yum"),
    ("apt-get", "apt"),
    ("zypper", "zypper"),
    ("apk", "apk"),
    ("pacman", "pacman"),
    ("brew", "brew"),
]


def detect_package_manager() -> str | None:
    """Return the package manager's name, or None when none matches."""
    for binary, name in MANAGER_PROBE_ORDER:
        if shutil.which(binary):
            return name
    return None


# ---------------------------------------------------------------------------
# Package hints and install hints
# ---------------------------------------------------------------------------

# Checks whose fix is not a distro package, so every manager
# gets the same hint: the upstream source itself. Doctor runs on
# deployment hosts (Debian and the like) where the repo's own dev
# tooling is absent, so the hints stay host-usable: the upstream
# release URL or the package manager that actually serves the
# binary.
UPSTREAM_HINTS = {
    "cloud-hypervisor": (
        "upstream static release (ships cloud-hypervisor and "
        "ch-remote) — install from "
        "https://github.com/cloud-hypervisor/cloud-hypervisor/releases"
    ),
    "ch-remote": (
        "upstream static release (ships cloud-hypervisor and "
        "ch-remote) — install from "
        "https://github.com/cloud-hypervisor/cloud-hypervisor/releases"
    ),
    "secretspec": (
        "release binary — install from "
        "https://github.com/cachix/secretspec/releases"
    ),
    "jscpd": (
        "npm install -g jscpd (a development tool; the daemon runs without it)"
    ),
}

# Each entry: check name → {manager → package}. A manager missing
# from an entry falls back to the check name in the generic hint.
# The package name differs from the binary it ships on several
# rows: e2fsprogs ships mkfs.ext4/resize2fs/e2fsck, genisoimage
# (Debian family) and cdrtools (pacman, brew) both serve mkisofs,
# qemu-utils/qemu-img is Debian's name for the conversion tool,
# and conntrack comes from conntrack-tools everywhere except apt.
# cloud-hypervisor and ch-remote carry no rows: Debian's archive
# has neither, so their hint is the upstream release (UPSTREAM_HINTS).
PACKAGE_HINTS: dict[str, dict[str, str]] = {
    "curl": {
        "dnf": "curl",
        "yum": "curl",
        "apt": "curl",
        "pacman": "curl",
        "zypper": "curl",
        "apk": "curl",
        "brew": "curl",
    },
    "mkfs.ext4": {
        "dnf": "e2fsprogs",
        "yum": "e2fsprogs",
        "apt": "e2fsprogs",
        "pacman": "e2fsprogs",
        "zypper": "e2fsprogs",
        "apk": "e2fsprogs",
        "brew": "e2fsprogs",
    },
    "resize2fs": {
        "dnf": "e2fsprogs",
        "yum": "e2fsprogs",
        "apt": "e2fsprogs",
        "pacman": "e2fsprogs",
        "zypper": "e2fsprogs",
        "apk": "e2fsprogs",
        "brew": "e2fsprogs",
    },
    "e2fsck": {
        "dnf": "e2fsprogs",
        "yum": "e2fsprogs",
        "apt": "e2fsprogs",
        "pacman": "e2fsprogs",
        "zypper": "e2fsprogs",
        "apk": "e2fsprogs",
        "brew": "e2fsprogs",
    },
    "mkisofs": {
        "dnf": "genisoimage",
        "yum": "genisoimage",
        "apt": "genisoimage",
        "zypper": "genisoimage",
        "pacman": "cdrtools",
        "apk": "cdrkit",
        "brew": "cdrtools",
    },
    "ip": {
        # iproute on the Red Hat family, iproute2 everywhere else.
        "dnf": "iproute",
        "yum": "iproute",
        "apt": "iproute2",
        "zypper": "iproute2",
        "apk": "iproute2",
        "pacman": "iproute2",
        "brew": "iproute2",
    },
    "iptables": {
        "dnf": "iptables",
        "yum": "iptables",
        "apt": "iptables",
        "pacman": "iptables",
        "zypper": "iptables",
        "apk": "iptables",
        "brew": "iptables",
    },
    "nft": {
        "dnf": "nftables",
        "yum": "nftables",
        "apt": "nftables",
        "pacman": "nftables",
        "zypper": "nftables",
        "apk": "nftables",
        "brew": "nftables",
    },
    "conntrack": {
        "dnf": "conntrack-tools",
        "yum": "conntrack-tools",
        "apt": "conntrack",
        "zypper": "conntrack-tools",
        "apk": "conntrack-tools",
        "pacman": "conntrack-tools",
    },
    "ssh": {
        "dnf": "openssh-clients",
        "yum": "openssh-clients",
        "apt": "openssh-client",
        "zypper": "openssh",
        "apk": "openssh-client",
        "pacman": "openssh",
        "brew": "openssh",
    },
    "qemu-img": {
        "dnf": "qemu-img",
        "yum": "qemu-img",
        "apt": "qemu-utils",
        "zypper": "qemu-tools",
        "apk": "qemu-img",
        "pacman": "qemu",
        "brew": "qemu",
    },
    "rsync": {
        "dnf": "rsync",
        "yum": "rsync",
        "apt": "rsync",
        "pacman": "rsync",
        "zypper": "rsync",
        "apk": "rsync",
        "brew": "rsync",
    },
    "tmux": {
        "dnf": "tmux",
        "yum": "tmux",
        "apt": "tmux",
        "pacman": "tmux",
        "zypper": "tmux",
        "apk": "tmux",
        "brew": "tmux",
    },
    "libnetfilter_queue": {
        "dnf": "libnetfilter_queue",
        "yum": "libnetfilter_queue",
        "apt": "libnetfilter-queue1",
        "zypper": "libnetfilter_queue1",
        "apk": "libnetfilter-queue",
        "pacman": "libnetfilter_queue",
    },
    "libnfnetlink": {
        "dnf": "libnfnetlink",
        "yum": "libnfnetlink",
        "apt": "libnfnetlink0",
        "zypper": "libnfnetlink0",
        "apk": "libnfnetlink",
        "pacman": "libnfnetlink",
    },
}


def install_hint(name: str, manager: str | None) -> str:
    """Return an install hint string for a missing check *name*."""
    if name in UPSTREAM_HINTS:
        return UPSTREAM_HINTS[name]
    if manager is None:
        return f"install {name}"
    pkg = PACKAGE_HINTS.get(name, {}).get(manager, name)
    return manager_install(manager, pkg)


def manager_install(manager: str, pkg: str) -> str:
    """The install command line for one manager's package."""
    if manager == "brew":
        return f"brew install {pkg}"
    if manager == "apt":
        return f"sudo apt install {pkg}"
    if manager == "pacman":
        # pacman has no "install" subcommand — the package action is -S.
        return f"sudo pacman -S {pkg}"
    if manager == "apk":
        # apk's install verb is "add".
        return f"sudo apk add {pkg}"
    return f"sudo {manager} install {pkg}"


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a probe command, return (returncode, stdout, stderr).

    A missing binary or a timeout reads as a failed probe (-1) with
    the reason in stderr, so callers report one shape of failure.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"{cmd[0]}: timed out"
    except OSError as exc:
        # Covers a missing binary and an unexecutable one (ENOEXEC
        # on a corrupt settings-named path) with the same shape.
        return -1, "", f"{cmd[0]}: {exc}"


def binary_missing(
    name: str,
    manager: str | None,
    *,
    is_warning: bool,
    use: str,
) -> CheckResult:
    """The miss for a PATH-absent binary, naming the path that breaks."""
    suffix = f" ({use})" if use else ""
    return CheckResult(
        name=name,
        ok=False,
        is_warning=is_warning,
        message=f"{name} not found on PATH{suffix}",
        hint=install_hint(name, manager),
    )


def check_binary(
    name: str,
    probe: list[str],
    manager: str | None,
    *,
    is_warning: bool = False,
    use: str = "",
) -> CheckResult:
    """Check a binary is on PATH and answers its probe.

    *probe* holds the capability command (``["mkisofs", "--version"]``);
    an empty list only verifies PATH presence, for tools whose every
    invocation mutates something (``resize2fs`` has no read-only mode
    worth trusting). *use* names the path that needs the tool, and
    appears in the missing message so the operator knows what breaks.
    """
    path = shutil.which(name)
    if not path:
        return binary_missing(name, manager, is_warning=is_warning, use=use)
    if not probe:
        return CheckResult(name=name, ok=True, message=f"{name} ok ({path})")
    rc, _out, err = run(probe)
    if rc != 0:
        detail = (err or f"exit {rc}").strip()[:200]
        return CheckResult(
            name=name,
            ok=False,
            is_warning=is_warning,
            message=f"{name} found at {path} but probe failed: {detail}",
            hint=install_hint(name, manager),
        )
    return CheckResult(name=name, ok=True, message=f"{name} ok ({path})")


def ssh_result(manager: str | None, message: str) -> CheckResult:
    """A warning-grade ssh miss carrying the install hint."""
    return CheckResult(
        name="ssh",
        ok=False,
        is_warning=True,
        message=message,
        hint=install_hint("ssh", manager),
    )


def ssh_banner_ok(rc: int, err: str) -> bool:
    """True when the probe answered with OpenSSH's version banner."""
    return rc in (0, 1) and "OpenSSH" in err


def check_ssh(manager: str | None) -> CheckResult:
    """Check the host ssh client (#110's forward smoke, #112's workflow).

    ``ssh -V`` writes its version to stderr; the exit code varies
    by release (0 or 1), so the rc check steps aside for the
    "OpenSSH" banner in the probe's output instead.
    """
    path = shutil.which("ssh")
    if not path:
        return ssh_result(
            manager,
            "ssh not found on PATH (the documented ssh workflow "
            "and the forward-path smoke run over `msks forward`)",
        )
    rc, out, err = run(["ssh", "-V"])
    if ssh_banner_ok(rc, err):
        return CheckResult(name="ssh", ok=True, message=f"ssh ok ({path})")
    detail = (err.strip() or out.strip() or f"exit {rc}")[:200]
    return ssh_result(
        manager, f"ssh found at {path} but probe failed: {detail}"
    )


# The consent terminal's documented floor (#379): display-popup
# and the launch chain's bind-time-validated commands both need
# tmux 3.2.
TMUX_MIN_VERSION = (3, 2)


def parse_tmux_version(out: str) -> tuple[int, int] | None:
    """Parse ``tmux -V`` output (``"tmux 3.6a"``) → ``(3, 6)``.

    None when no MAJOR.MINOR pair is present, so the caller
    reports an unparseable version instead of guessing.
    """
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def tmux_unparseable(out: str, manager: str | None) -> CheckResult:
    """The miss for a ``tmux -V`` answer with no version in it."""
    probe = out.strip()[:40] or "tmux -V failed"
    return CheckResult(
        name="tmux",
        ok=False,
        message=(
            f"tmux version unparseable ({probe}); "
            "the consent terminal needs >= 3.2"
        ),
        hint=install_hint("tmux", manager),
    )


def tmux_version_result(
    version: tuple[int, int], path: str, manager: str | None
) -> CheckResult:
    """The result for one parsed tmux version, against the floor."""
    label = ".".join(map(str, version))
    if version < TMUX_MIN_VERSION:
        return CheckResult(
            name="tmux",
            ok=False,
            message=(
                f"tmux {label} < 3.2 — the consent terminal needs "
                "display-popup"
            ),
            hint=install_hint("tmux", manager),
        )
    return CheckResult(
        name="tmux", ok=True, message=f"tmux {label} ok ({path})"
    )


def check_tmux(manager: str | None) -> CheckResult:
    """Check the host tmux meets the 3.2 floor the consent
    terminal documents (#379).

    ``display-popup`` landed in 3.2 and the launch chain's
    bind-time-validated commands stay inside the 3.2 grammar, so
    an older or unparseable tmux is an error — the consent flow
    cannot launch its terminal on it.
    """
    path = shutil.which("tmux")
    if not path:
        return CheckResult(
            name="tmux",
            ok=False,
            message="tmux not found on PATH",
            hint=install_hint("tmux", manager),
        )
    rc, out, _err = run(["tmux", "-V"])
    version = parse_tmux_version(out) if rc == 0 else None
    if version is None:
        return tmux_unparseable(out, manager)
    return tmux_version_result(version, path, manager)


# The definitive capability test for the two libraries: the
# netfilterqueue extension links them, so importing it exercises
# exactly what the interceptor's NFQUEUE path loads at runtime.
NFQ_IMPORT_PROBE = [sys.executable, "-c", "import netfilterqueue"]


def check_library(name: str, manager: str | None) -> CheckResult:
    """Check a shared library is loadable, through layered probers.

    ``pkg-config --exists`` first (it answers when the -dev package
    ships the .pc file), then ``ldconfig -p`` (the runtime soname
    cache, present without any -dev package), then the
    netfilterqueue import itself — which also covers hosts whose
    linker database lists nothing (the extension binds its own
    paths, as on NixOS). Each layer that answers "present" passes;
    all three failing is the error.
    """
    via = library_probe_result(name)
    if via is not None:
        return CheckResult(name=name, ok=True, message=f"{name} ok ({via})")
    return CheckResult(
        name=name,
        ok=False,
        message=(
            f"{name} not found via pkg-config, ldconfig, or "
            "the netfilterqueue import"
        ),
        hint=install_hint(name, manager),
    )


def pkg_config_evidence(name: str) -> str | None:
    """ "pkg-config" when the -dev package ships the .pc file."""
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        return None
    rc, _out, _err = run([pkg_config, "--exists", name])
    return "pkg-config" if rc == 0 else None


def ldconfig_evidence(name: str) -> str | None:
    """ "ldconfig" when the runtime soname cache lists the library."""
    ldconfig = shutil.which("ldconfig")
    if ldconfig is None:
        return None
    rc, out, _err = run([ldconfig, "-p"])
    if rc == 0 and name in out:
        return "ldconfig"
    return None


def import_evidence() -> str | None:
    """ "netfilterqueue import" when the linked extension loads."""
    rc, _out, _err = run(NFQ_IMPORT_PROBE)
    return "netfilterqueue import" if rc == 0 else None


def library_probe_result(name: str) -> str | None:
    """The evidence string naming how *name* was found, or None."""
    return (
        pkg_config_evidence(name)
        or ldconfig_evidence(name)
        or import_evidence()
    )


# ---------------------------------------------------------------------------
# The check set
# ---------------------------------------------------------------------------


def run_doctor(settings: Settings) -> DoctorReport:
    """Run every check against one settings set, return the report."""
    report = DoctorReport()
    manager = detect_package_manager()
    vmm = settings.vmm
    net = settings.net

    # Error grade: the daemon execs each of these on its core paths.
    report.add(
        check_binary(
            vmm.cloud_hypervisor,
            [vmm.cloud_hypervisor, "--version"],
            manager,
            use="the VMM behind the local backend (#1)",
        )
    )
    report.add(
        check_binary(
            vmm.mkfs_ext4,
            [vmm.mkfs_ext4, "-V"],
            manager,
            use="builds each workspace volume",
        )
    )
    report.add(
        # resize2fs answers every flag with a mutation or a usage
        # error; PATH presence is the honest probe.
        check_binary(vmm.resize2fs, [], manager, use="grows volumes (#184)")
    )
    report.add(
        check_binary(
            vmm.e2fsck,
            [vmm.e2fsck, "-V"],
            manager,
            use="checks volumes before a resize (#184)",
        )
    )
    report.add(
        check_binary(
            vmm.mkisofs,
            [vmm.mkisofs, "--version"],
            manager,
            use="packs the cidata seed disk (#41)",
        )
    )
    report.add(
        check_binary(
            net.ip_tool,
            [net.ip_tool, "-V"],
            manager,
            use="taps and addresses for each workspace",
        )
    )
    report.add(
        check_binary(
            net.nft_tool,
            [net.nft_tool, "--version"],
            manager,
            use="egress chains and NAT (#52)",
        )
    )
    report.add(
        check_binary(
            net.conntrack_tool,
            [net.conntrack_tool, "--version"],
            manager,
            use="kills a revoked destination's established flows (#52)",
        )
    )
    report.add(
        check_binary(
            vmm.qemu_img,
            [vmm.qemu_img, "--version"],
            manager,
            use="builds the root overlay and probes volume formats",
        )
    )
    report.add(
        check_binary(
            settings.secret_store.cli,
            [settings.secret_store.cli, "--version"],
            manager,
            use="the secret store's CLI (#198)",
        )
    )
    report.add(check_library("libnetfilter_queue", manager))
    report.add(check_library("libnfnetlink", manager))
    report.add(check_tmux(manager))

    # Warning grade: these name development-time or client-side
    # paths — a debugging aid, a diagnostic for foreign firewall
    # policy, the clone scanner, and the two client-side tools the
    # documented workflows run over `msks forward`. The daemon
    # itself runs without them.
    report.add(
        check_binary(
            "curl",
            ["curl", "--version"],
            manager,
            is_warning=True,
            use="unix-socket REST poking during VMM debugging",
        )
    )
    report.add(
        check_binary(
            "ch-remote",
            ["ch-remote", "--version"],
            manager,
            is_warning=True,
            use=(
                "pokes the VMM's API socket by hand (dev and demo "
                "flows; the daemon speaks the socket itself)"
            ),
        )
    )
    report.add(
        check_binary(
            "iptables",
            ["iptables", "--version"],
            manager,
            is_warning=True,
            use=(
                "diagnoses foreign FORWARD drops that block the "
                "egress forward path (#75/#52)"
            ),
        )
    )
    report.add(
        check_binary(
            "jscpd",
            ["jscpd", "--version"],
            manager,
            is_warning=True,
            use="the token-clone scanner (#71)",
        )
    )
    report.add(check_ssh(manager))
    report.add(
        check_binary(
            "rsync",
            ["rsync", "--version"],
            manager,
            is_warning=True,
            use="host-side sync over the forward (#110)",
        )
    )
    return report


# ---------------------------------------------------------------------------
# Settings resolution and the console entry
# ---------------------------------------------------------------------------


def default_path_absent(exc: Exception) -> bool:
    """True when the load failure is the missing default-path file.

    The not-found raise is config.py's stable in-tree contract
    (``"config file not found: <path>"``); every other failure —
    unreadable file, invalid YAML, a bad value — names a config
    that exists and is broken, which doctor must surface.
    """
    return str(exc).startswith("config file not found")


def settings_for_doctor(
    config: str | None,
) -> tuple[Settings, CheckResult | None]:
    """Settings that name the tools doctor checks, plus a notice.

    ``load_settings`` reads the same sources the daemon will (env
    over file over defaults) without ever generating the first-run
    template — doctor takes no side effects. A default-path miss
    on a fresh host is the normal pre-first-run state and stays
    silent; any config that exists but fails to load produces a
    warning result naming the error, and the checks fall back to
    env-plus-defaults so the run still reports the host's tools.
    """
    try:
        return load_settings(config, generate=False), None
    except Exception as exc:  # any operator config mistake, foreseen or not
        fallback = Settings.from_env()
        if config is None and default_path_absent(exc):
            return fallback, None
        notice = CheckResult(
            name="config",
            ok=False,
            is_warning=True,
            message=(
                f"config load failed ({exc}); checking env/default tool names"
            ),
        )
        return fallback, notice


def doctor_main(config: str | None) -> int:
    """Run all checks, print the report, return the exit code."""
    settings, notice = settings_for_doctor(config)
    report = run_doctor(settings)
    if notice is not None:
        report.add(notice)
    print(format_report(report))
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def result_marker(result: CheckResult) -> str:
    """The ✓/⚠/✗ marker for one result."""
    if result.ok:
        return "✓"
    if result.is_warning:
        return "⚠"
    return "✗"


def append_result(lines: list[str], result: CheckResult) -> None:
    """One result line with its marker; the hint follows on its own."""
    marker = result_marker(result)
    lines.append(f"  {marker} {result.name}: {result.message}")
    if result.hint:
        lines.append(f"    Run:  {result.hint}")


def append_failure_block(
    lines: list[str], results: list[CheckResult], marker: str, heading: str
) -> None:
    """A closing block that repeats each failure with its fix."""
    if not results:
        return
    lines.append(heading)
    lines.append("")
    for result in results:
        lines.append(f"  {marker} {result.name}: {result.message}")
        if result.hint:
            lines.append(f"    Run:  {result.hint}")
        lines.append("")


def summary_line(ok_count: int, errors: int, warnings: int) -> str:
    """The closing tally, naming only the nonzero grades."""
    parts = [f"{ok_count} passed"]
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    return ", ".join(parts)


def append_summary(lines: list[str], report: DoctorReport) -> None:
    """The tally plus, when anything failed, the repeated fix list."""
    errors = report.errors
    warnings = report.warnings
    ok_count = len(report.results) - len(errors) - len(warnings)
    if not errors and not warnings:
        lines.append(f"All {ok_count} checks passed.")
        return
    lines.append(summary_line(ok_count, len(errors), len(warnings)))
    lines.append("")
    append_failure_block(
        lines, errors, "✗", "Errors (the daemon needs these):"
    )
    append_failure_block(
        lines, warnings, "⚠", "Warnings (degraded paths, not core):"
    )


def format_report(report: DoctorReport) -> str:
    """Format a doctor report for the terminal."""
    lines = ["msksd doctor", "=" * 40]
    manager = detect_package_manager()
    lines.append(f"Package manager: {manager or '(none detected)'}")
    lines.append("")
    for result in report.results:
        append_result(lines, result)
    lines.append("")
    append_summary(lines, report)
    return "\n".join(lines)
