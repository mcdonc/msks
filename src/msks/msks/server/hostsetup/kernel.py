"""The routing-kernel-settings phase.

The forwarding sysctl as a boot-time setting and the
netfilter/tun modules at boot — the settings egress verifies or
needs, with the module states the summary's consent line reads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import HostProfile

from .profile import DEBIAN_PROFILE, HostProfile
from .util import HostSetupError, read_file, remove_file, sh, write_file

SYSCTL_FILE = "/etc/sysctl.d/msks-egress.conf"

MODULES_FILE = "/etc/modules-load.d/msks.conf"

IP_FORWARD_PROC = "/proc/sys/net/ipv4/ip_forward"

SYSCTL_TEXT = """\
# msksd workspace egress (docs/networking.md): route packets between
# per-VM taps and the host uplink. The daemon reads this key at
# startup — net.ipv4.ip_forward=1 — and refuses every egress
# workspace with the cause named while it reads 0.
net.ipv4.ip_forward = 1
"""

# nix/module.nix's boot.kernelModules, plus the reject and NFQUEUE
# pieces its list leaves to autoload (the deny RST's
# `reject with tcp reset`, the interactive queue's `queue num`).
MODULES = (
    "tun",
    "nf_tables",
    "nft_chain_nat",
    "nft_masq",
    "nft_ct",
    "nft_reject",
    "nf_reject_ipv4",
    "nf_nat",
    "nf_conntrack",
    "nfnetlink_queue",
)

# command → package for the wrapper path, the phases the setup
# itself runs, and the daemon's and client's bare-name exec
# surface — the tools `msksd doctor` probes (#598).


def land_sysctl() -> None:
    """The forwarding sysctl, boot-time and now; the daemon refuses
    egress workspaces naming this key while it reads 0."""
    write_file(SYSCTL_FILE, SYSCTL_TEXT)
    rc, _out, err = sh(["sysctl", "--load", SYSCTL_FILE])
    if read_file(IP_FORWARD_PROC).strip() != "1":
        detail = f"sysctl rc={rc}" + (
            f", {err.strip()}" if err.strip() else ""
        )
        raise HostSetupError(
            f"{IP_FORWARD_PROC} still reads 0 after loading "
            f"{SYSCTL_FILE} ({detail})"
        )


# A module's state after land_modules: it loaded, the kernel's
# module tree does not carry it, or the load itself failed.
LOADED = "loaded"

ABSENT = "absent"

LOAD_FAILED = "load-failed"

# The three the consent chain's rulesets reference (the deny RST's
# `reject with tcp reset`, the interactive queue's `queue num`):
# interactive egress consent cannot work without them, and the
# daemon refuses consent-egress workspaces (#550).
CONSENT_MODULES = ("nft_reject", "nf_reject_ipv4", "nfnetlink_queue")


def land_modules(profile: HostProfile = DEBIAN_PROFILE) -> dict[str, str]:
    """The modules file, boot-time (nix/module.nix's deterministic
    posture) and modprobe'd now. Returns each module's state for
    the summary's consent line: a name the kernel tree lacks gets
    the named observation and remediation (#550), not the plain
    load-failure warning."""
    write_file(
        MODULES_FILE, profile.modules_header + "\n".join(MODULES) + "\n"
    )
    return report_module_states(module_states(), profile)


def module_states() -> dict[str, str]:
    """Every module's state, probed in list order."""
    return {module: load_module(module) for module in MODULES}


def report_module_states(
    states: dict[str, str], profile: HostProfile = DEBIAN_PROFILE
) -> dict[str, str]:
    """Warn per failure kind and hand the states to the summary."""
    warn_load_failures(
        profile.tool,
        [m for m, state in states.items() if state == LOAD_FAILED],
    )
    warn_absent_modules(
        [m for m, state in states.items() if state == ABSENT],
        profile,
    )
    return states


def load_module(module: str) -> str:
    """One module's state: the dry-run resolves whether the kernel
    tree carries it at all (a built-in answers yes; kmod answers 1
    for a name the tree lacks — every other answer, including sh's
    -1 infra failure, is a load failure, not an absence), then the
    real load runs."""
    rc, _out, _err = sh(["modprobe", "--dry-run", module])
    if rc == 1:
        return ABSENT
    if rc != 0:
        return LOAD_FAILED
    rc, _out, _err = sh(["modprobe", module])
    return LOADED if rc == 0 else LOAD_FAILED


def warn_load_failures(tool: str, failed: list[str]) -> None:
    """A module the tree carries whose load failed: a genuine
    kernel-level failure, the generic warning (the daemon's
    ruleset load fails closed naming the cause)."""
    for module in failed:
        print(
            f"{tool}: WARNING modprobe {module} failed — check the kernel",
            file=sys.stderr,
        )


MODULES_ROOT = "/lib/modules"


def module_tree_present() -> bool:
    """Whether /lib/modules carries a tree for the running release
    at all — a container or a pruned host can hold none."""
    return Path(MODULES_ROOT, os.uname().release).is_dir()


def tree_cause() -> str:
    """Why the tree lacks a module, as the warning states it: no
    tree for this release (the kernel package is not installed —
    a container, a trimmed image) or a tree without the module (a
    trimmed kernel build; stock Debian flavors, cloud included,
    carry the consent modules — #550's evidence host ran a pruned
    tree, not a stock flavor)."""
    if not module_tree_present():
        return "no module tree is installed on this host"
    return "this kernel's build carries a trimmed module set"


def warn_absent_modules(
    absent: list[str], profile: HostProfile = DEBIAN_PROFILE
) -> None:
    """Name the absence and its remediation (#550): the observation
    (the module is not in this kernel's tree) plus the cause shape
    and the stock-kernel fix — a causal claim beyond that is one
    the tool cannot verify."""
    for module in absent:
        print(
            f"{profile.tool}: WARNING {module} is absent from this kernel's "
            f"module tree — {tree_cause()} "
            f"(running release {os.uname().release})",
            file=sys.stderr,
        )
    if absent:
        print(
            f"{profile.tool}: the stock kernel carries them: "
            f"{profile.package_manager} install "
            f"{profile.kernel_package(os.uname().machine, os.uname().release)}"
            ", reboot, re-run",
            file=sys.stderr,
        )


def drop_sysctl(profile: HostProfile = DEBIAN_PROFILE) -> str:
    """The forwarding sysctl's mirror: the boot-time file leaves,
    then `sysctl --system` re-applies every remaining sysctl.d
    file — the same set a boot applies. A second owner that
    wrote its own file keeps its value; a live-only setter the
    tool never saw (a container runtime's own forwarding) keeps
    its value too, because --system names nothing itself. A
    --system run that trips on some other file's keys is a
    warning, not a refusal — the file removal is this tool's
    piece, the rest of sysctl.d is not."""
    if not remove_file(SYSCTL_FILE):
        return f"{SYSCTL_FILE} already absent"
    rc, _out, err = sh(["sysctl", "--system"])
    if rc != 0:
        print(
            f"{profile.tool}: WARNING sysctl --system failed "
            f"({err.strip()}) — the remaining sysctl.d files apply "
            "at the next boot",
            file=sys.stderr,
        )
    return (
        f"{SYSCTL_FILE} removed (sysctl --system re-applied the rest — "
        "net.ipv4.ip_forward keeps its live value until the host reboots "
        "where no remaining file names it; sysctl -w "
        "net.ipv4.ip_forward=0 turns it off now)"
    )


def drop_modules_file() -> str:
    """The modules file's mirror: the boot-time list leaves; the
    loaded modules stay — the kernel shares them with whatever
    else loaded them, and a reboot clears the live set."""
    if not remove_file(MODULES_FILE):
        return f"{MODULES_FILE} already absent"
    return f"{MODULES_FILE} removed (loaded modules stay until reboot)"
