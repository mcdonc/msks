"""The doctor run: the composition and the CLI.

``run_doctor`` walks every check into a report; ``doctor_main``
loads the settings and prints it, human or JSON.
"""

from __future__ import annotations

import json
import sys

from ...config import load_settings
from ...settings import Settings
from .checks import (
    check_binary,
    check_library,
    check_nfq_load,
    check_ssh,
    check_tmux,
    check_virtiofsd,
)
from .hints import detect_package_manager
from .kvm import apple_silicon_kvm_result, kvm_access_result
from .probes import (
    path_note_result,
    rescued_from_admin_dirs,
    session_lacks_admin_dirs,
)
from .report import CheckResult, DoctorReport, format_report


def add_path_note(report: DoctorReport) -> None:
    """The relogin note (#601): a session without the admin
    directories whose tools only the daemon-shaped probe found —
    every row the daemon will exec reads green, and the operator
    learns why their own shell cannot type the same names."""
    if session_lacks_admin_dirs() and any(
        rescued_from_admin_dirs(result.name)
        for result in report.results
        if result.ok
    ):
        report.add(path_note_result())


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
    if sys.platform == "linux":
        # The node's group gate rides beside the VMM that opens
        # it — Linux-only like the consent-stack rows (#542):
        # /dev/kvm is a Linux node, and every group-add command
        # the row can hint at prescribes one on another platform.
        report.add(kvm_access_result(manager))
        apple_kvm = apple_silicon_kvm_result()
        if apple_kvm is not None:
            report.add(apple_kvm)
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
    if sys.platform == "linux":
        report.add(check_library("libnetfilter_queue", manager))
        report.add(check_library("libnfnetlink", manager))
    # Off Linux the two library rows stay off the report: their
    # install candidates there are Linux-only packages (Homebrew's
    # formulae above all), so the rows would prescribe installs
    # that cannot work. The binding row below carries the
    # platform contract (#542).
    report.add(check_nfq_load(manager))
    report.add(check_tmux(manager))

    # Warning grade: the client-side tools the documented
    # workflows run over `msks forward` — the ssh client and the
    # host rsync it drives. The daemon itself runs without them,
    # so a miss warns instead of failing the run.
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
    # Warning grade: the host-mount machinery (#620, part #622).
    # A daemon without allowed host roots never execs virtiofsd,
    # so a miss warns (it only breaks the mounts feature). The
    # check reads the VERSION, not only presence: the mounts'
    # flag set needs >= 1.13.0 (#655).
    report.add(check_virtiofsd(vmm.virtiofsd, manager))
    add_path_note(report)
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


def doctor_main(config: str | None, as_json: bool = False) -> int:
    """Run all checks, print the report, return the exit code.
    ``--json`` prints the rows as one document (#841) — the same
    verdicts, the same exit code, machine-readable."""
    settings, notice = settings_for_doctor(config)
    report = run_doctor(settings)
    if notice is not None:
        report.add(notice)
    if as_json:
        print(json.dumps(report_document(report), indent=2))
        return 0 if report.passed else 1
    print(format_report(report))
    return 0 if report.passed else 1


def report_document(report) -> list[dict]:
    """The report's rows as documents (#841): one per check, the
    verdict booleans and the message the prose report carries."""
    return [
        {
            "name": result.name,
            "ok": result.ok,
            "warning": bool(result.is_warning),
            "message": result.message,
            "hint": result.hint,
        }
        for result in report.results
    ]
