"""The /dev/kvm access checks.

Node and group presence, membership, the session-predates-group
stale case, and the nested-virt detection an Apple-Silicon host
answers with.
"""

from __future__ import annotations

import grp
import os
import sys
from pathlib import Path

from ..hostsetup import executing_user, group_present, in_group
from .hints import group_add_hint, install_hint
from .probes import run
from .report import CheckResult

# ---------------------------------------------------------------------------
# The /dev/kvm group gate (#559)
# ---------------------------------------------------------------------------


def kvm_node_absent(manager: str | None) -> CheckResult:
    """The host carries no /dev/kvm at all — its own miss, never
    a group failure: nested virtualization off, or the stack
    that creates the node missing."""
    return CheckResult(
        name="/dev/kvm",
        ok=False,
        message=(
            "/dev/kvm is absent — the daemon cannot boot "
            "workspaces without it (nested virtualization off, "
            "or the KVM stack missing)"
        ),
        hint=install_hint("qemu-kvm", manager),
    )


def kvm_group_absent(manager: str | None) -> CheckResult:
    """The node stands but the group database carries no kvm
    group — the stack that creates it is missing."""
    return CheckResult(
        name="/dev/kvm",
        ok=False,
        message=(
            "the kvm group is absent — the KVM stack that "
            "creates it is missing"
        ),
        hint=install_hint("qemu-kvm", manager),
    )


def kvm_not_a_member(user: str, manager: str | None) -> CheckResult:
    """The database knows the group and the user sits outside it
    — the row a skipped hostsetup run meets (#559)."""
    return CheckResult(
        name="/dev/kvm",
        ok=False,
        message=f"{user} is not in the kvm group, which gates /dev/kvm",
        hint=group_add_hint(user, manager),
    )


def kvm_session_predates(user: str) -> CheckResult:
    """The database says member, the session's live groups say
    no — the group landed after this login."""
    return CheckResult(
        name="/dev/kvm",
        ok=False,
        message=(
            f"{user} is in the kvm group, but this session "
            "predates the membership — a daemon started from "
            "it still meets EACCES on /dev/kvm"
        ),
        hint=(
            "log out and back in (or start msksd under "
            "newgrp kvm) once the group has landed"
        ),
    )


def kvm_session_stale(uid: int) -> bool:
    """Whether the daemon user's own session lacks the group the
    database grants: the run is that user's (not a sudo lift —
    a root session cannot speak for the user's login), and
    neither the live supplementary groups nor the primary gid
    carries the group."""
    if os.geteuid() != uid:
        return False
    kvm_gid = grp.getgrnam("kvm").gr_gid
    return kvm_gid not in os.getgroups() and os.getgid() != kvm_gid


def kvm_access_result(manager: str | None) -> CheckResult:
    """The gate every workspace boot passes on a KVM host: the
    node's presence, and the daemon user's kvm-group membership —
    the same group-database read #555's peer policy and
    hostsetup's membership step take, primary or supplementary,
    never a platform guess. A login that predates the membership
    fails on its own line: the database says yes, the session's
    live groups say no, and a daemon started from that session
    still meets EACCES on the node. A sudo-lifted run cannot
    read the daemon user's session — there the row stands on
    the database answer alone."""
    user, uid, _gid, _home = executing_user()
    if not os.path.exists("/dev/kvm"):
        return kvm_node_absent(manager)
    if not group_present("kvm"):
        return kvm_group_absent(manager)
    if not in_group(user, "kvm"):
        return kvm_not_a_member(user, manager)
    if kvm_session_stale(uid):
        return kvm_session_predates(user)
    return CheckResult(
        name="/dev/kvm", ok=True, message=f"/dev/kvm ok ({user} in kvm)"
    )


#: The MIDR's implementer field names Apple's CPU business
#: (0x61) — the silicon marker for the Asahi host class. Apple's
#: Virtualization.framework passes the host MIDR through to its
#: guests, so the marker alone names every Apple-silicon Linux
#: host, not just the ones the #649 limitation is about (#664);
#: the NV-mode markers below decide which of them earn the row.
APPLE_IMPLEMENTER = 0x61
#: Where a Linux arm64 kernel publishes cpu0's MIDR — the
#: register file sysfs presents per CPU.
MIDR_EL1 = Path("/sys/devices/system/cpu/cpu0/regs/identification/midr_el1")
#: The kernel command line, where the kvm-arm.mode early option
#: lands when the bootloader or the operator sets it.
PROC_CMDLINE = Path("/proc/cmdline")
#: Where sysfs publishes kvm's mode parameter on kernels that
#: expose the option as a module parameter.
KVM_MODE_PARAM = Path("/sys/module/kvm/parameters/mode")
#: The kvm-arm.mode value that runs arm64 KVM in nested-
#: virtualization (NV) mode — the mode the Asahi kernel runs on
#: Apple Silicon, whose vGIC does not yet deliver guest
#: interrupts (#649). The Asahi tree's arm64 KVM accepts only
#: none/protected/nvhe/nested; the numeric spelling the #649
#: record first sketched is a malformed early option.
KVM_MODE_NESTED = "nested"
#: What KVM's init line in the kernel log names when NV mode is
#: engaged (`VHE+NV2 mode initialized`, the NV generation
#: varying with the hardware) — the running mode's own record,
#: there whatever selected the mode.
NV_INIT_MARKER = "VHE+NV"


def cmdline_names_nv_mode() -> bool:
    """Whether the kernel command line sets kvm-arm.mode=nested."""
    try:
        cmdline = PROC_CMDLINE.read_text()
    except OSError:
        return False
    return f"kvm-arm.mode={KVM_MODE_NESTED}" in cmdline.split()


def module_param_names_nv_mode() -> bool:
    """Whether sysfs publishes kvm's mode parameter as nested."""
    try:
        mode = KVM_MODE_PARAM.read_text()
    except OSError:
        return False
    return mode.strip() == KVM_MODE_NESTED


def kernel_log_names_nv_mode() -> bool:
    """Whether the kernel log carries KVM's NV-mode init line —
    the runtime marker a kernel that selected the mode on its
    own still names. A restricted kernel log (dmesg_restrict,
    a missing dmesg) reads as absent, never as a failure."""
    rc, out, _err = run(["dmesg"])
    return rc == 0 and NV_INIT_MARKER in out


def kvm_nv_mode() -> bool:
    """Whether this kernel's KVM runs in NV mode (#664): the
    command-line option, the sysfs parameter, or the init line —
    any one of the three, each naming the same mode."""
    return (
        cmdline_names_nv_mode()
        or module_param_names_nv_mode()
        or kernel_log_names_nv_mode()
    )


def midr_names_apple() -> bool:
    """Whether cpu0's MIDR implementer names Apple (#649) — the
    silicon half of the row's two markers, on the Linux arm64
    hosts alone that publish a MIDR. A host that publishes none
    (an exotic arm64 platform, any other machine) answers no."""
    if sys.platform != "linux" or os.uname().machine != "aarch64":
        return False
    try:
        midr = int(MIDR_EL1.read_text(), 16)
    except OSError, ValueError:
        return False
    return (midr >> 24) & 0xFF == APPLE_IMPLEMENTER


def apple_silicon_kvm_result() -> CheckResult | None:
    """The Asahi-class warning (#649): on Apple Silicon with KVM
    in NV mode, this kernel's KVM does not yet deliver guest
    interrupts — a workspace starts and its kernel boots deep
    into first boot, then a vCPU's timer dies and the guest
    stalls before ssh can answer. Everything short of a guest
    boot (builds, imports, the console, workspace lifecycle)
    works, so the row warns and names the behavior instead of
    failing the report.

    None on every other host: the row fires only where the
    condition exists — the MIDR implementer names Apple and the
    kernel's KVM runs in NV mode (#664) — and stays off x86_64
    hosts and non-Apple arm64 servers alike, and off Apple-
    silicon hosts whose KVM is not in NV mode: a Linux VM under
    macOS's Virtualization.framework reads as Apple silicon
    (VZ passes the host MIDR through) while its own KVM
    delivers guest interrupts, so workspaces boot and ssh
    answers there.
    """
    if not midr_names_apple():
        return None
    if not kvm_nv_mode():
        return None
    return CheckResult(
        name="apple-kvm",
        ok=False,
        is_warning=True,
        message=(
            "Apple Silicon host with KVM in NV mode "
            "(kvm-arm.mode=nested): this kernel's KVM does not "
            "yet deliver guest interrupts, so workspaces start and "
            "their kernels boot, then stall before ssh answers "
            "(#649); everything that runs short of a guest boot "
            "works"
        ),
    )


# ---------------------------------------------------------------------------
# The check set
# ---------------------------------------------------------------------------
