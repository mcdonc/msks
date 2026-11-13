"""``msksd doctor`` — pre-flight host dependency checker (#464).

The daemon and the msks client drive tools outside their Python
environment: the cloud-hypervisor VMM (#1), the e2fsprogs pair
that grows and checks workspace volumes (#184), the ``mkisofs``
that packs the #41 cidata seed disks, the iproute2 / nftables /
conntrack trio that wires and polices each workspace's tap (#52),
``qemu-img`` for the root overlay, the two libraries the
interceptor's NFQUEUE
path links against, and the client-side ssh/rsync pair and tmux
the documented workflows run on. A missing tool surfaces today as
a late runtime failure with an opaque error; doctor names each
one before that, with an install hint matched to the detected
package manager. The check set covers what msksd and the client
exec at runtime — debugging and development tools stay out of
it. One permission fact sits beside the tools: the ``/dev/kvm``
group gate every workspace boot passes (#559), read from the
group database the #555 posture takes.

Design (ported from klangkd doctor, klangk #1612): capabilities
first, never platform predictions. Every check runs on every
host; only the package-hint table varies by detected manager. One
deliberate exception (#542): the consent-stack rows — the two
NFQUEUE libraries and the binding's soname load — run on Linux
hosts only, because libnetfilter_queue is a Linux library (the
binding itself is vendored in the wheel on every platform) and
every install candidate for it is a Linux-only package; the
binding row carries that contract.
Checks are
graded — an error keeps the daemon from its core paths, a warning
names a client-side tool the daemon itself runs without. Exit
code is 0 when every check passes or only warns, 1 when any check
errors.

Tool names come from the daemon's own settings wherever a setting
exists (``load_settings``, env vars included), so doctor checks
what this host's daemon will actually exec; a config that fails to
load falls back to env-plus-defaults with a warning result naming
the error.
"""

import grp as grp  # noqa: F401 — suite seam
import os as os  # noqa: F401 — suite seam
import shutil as shutil  # noqa: F401 — suite seam
import subprocess as subprocess  # noqa: F401 — suite seam

# The composition submodule is ``main`` (run_doctor, doctor_main);
# ``run`` stays the probes' function name the suites patch.
import sys as _sys  # noqa: E402
import sys as sys  # noqa: F401 — suite seam

from ...config import load_settings as load_settings
from ..hostsetup import executing_user as executing_user
from . import checks, hints, kvm, probes, report
from .checks import (
    NFQ_LOAD_PROBE,
    TMUX_MIN_VERSION,
    binary_missing,
    check_binary,
    check_library,
    check_nfq_load,
    check_ssh,
    check_tmux,
    check_virtiofsd,
    import_evidence,
    ldconfig_evidence,
    library_probe_result,
    parse_tmux_version,
    pkg_config_evidence,
    ssh_banner_ok,
    ssh_result,
    tmux_unparseable,
    tmux_version_result,
    virtiofsd_version_result,
)
from .hints import (
    APK_QEMU_SYSTEM_PACKAGES,
    GROUP_ADD,
    MANAGER_PROBE_ORDER,
    PACKAGE_HINTS,
    UPSTREAM_HINTS,
    apk_qemu_system_package,
    cloud_hypervisor_hint,
    detect_package_manager,
    group_add_hint,
    install_hint,
    manager_install,
)
from .kvm import (
    APPLE_IMPLEMENTER,
    KVM_MODE_NESTED,
    KVM_MODE_PARAM,
    MIDR_EL1,
    NV_INIT_MARKER,
    PROC_CMDLINE,
    apple_silicon_kvm_result,
    cmdline_names_nv_mode,
    kernel_log_names_nv_mode,
    kvm_access_result,
    kvm_group_absent,
    kvm_node_absent,
    kvm_not_a_member,
    kvm_nv_mode,
    kvm_session_predates,
    kvm_session_stale,
    midr_names_apple,
    module_param_names_nv_mode,
)
from .main import (
    add_path_note,
    default_path_absent,
    doctor_main,
    report_document,
    run_doctor,
    settings_for_doctor,
)
from .probes import (
    admin_dir_candidate,
    path_note_result,
    probe_which,
    rescued_from_admin_dirs,
    run,
    session_lacks_admin_dirs,
    setup_tool_name,
)
from .report import (
    CheckResult,
    DoctorReport,
    append_failure_block,
    append_result,
    append_summary,
    format_report,
    hint_lines,
    result_marker,
    summary_line,
)

_main_module = _sys.modules[__name__ + ".main"]  # noqa: E402

SUBMODULES = (
    report,
    hints,
    probes,
    checks,
    kvm,
    _main_module,
)

__all__ = [
    "CheckResult",
    "DoctorReport",
    "append_failure_block",
    "append_result",
    "append_summary",
    "format_report",
    "hint_lines",
    "result_marker",
    "summary_line",
    "APK_QEMU_SYSTEM_PACKAGES",
    "GROUP_ADD",
    "MANAGER_PROBE_ORDER",
    "PACKAGE_HINTS",
    "UPSTREAM_HINTS",
    "apk_qemu_system_package",
    "cloud_hypervisor_hint",
    "detect_package_manager",
    "group_add_hint",
    "install_hint",
    "manager_install",
    "admin_dir_candidate",
    "path_note_result",
    "probe_which",
    "rescued_from_admin_dirs",
    "run",
    "session_lacks_admin_dirs",
    "setup_tool_name",
    "NFQ_LOAD_PROBE",
    "TMUX_MIN_VERSION",
    "binary_missing",
    "check_binary",
    "check_library",
    "check_nfq_load",
    "check_ssh",
    "check_tmux",
    "check_virtiofsd",
    "import_evidence",
    "ldconfig_evidence",
    "library_probe_result",
    "parse_tmux_version",
    "pkg_config_evidence",
    "ssh_banner_ok",
    "ssh_result",
    "tmux_unparseable",
    "tmux_version_result",
    "virtiofsd_version_result",
    "APPLE_IMPLEMENTER",
    "KVM_MODE_NESTED",
    "KVM_MODE_PARAM",
    "MIDR_EL1",
    "NV_INIT_MARKER",
    "PROC_CMDLINE",
    "apple_silicon_kvm_result",
    "cmdline_names_nv_mode",
    "kernel_log_names_nv_mode",
    "kvm_access_result",
    "kvm_group_absent",
    "kvm_node_absent",
    "kvm_not_a_member",
    "kvm_nv_mode",
    "kvm_session_predates",
    "kvm_session_stale",
    "midr_names_apple",
    "module_param_names_nv_mode",
    "add_path_note",
    "default_path_absent",
    "doctor_main",
    "report_document",
    "run_doctor",
    "settings_for_doctor",
]
