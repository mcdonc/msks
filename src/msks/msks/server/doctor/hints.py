"""The install hints: manager detection and package maps.

Which package manager the host runs, the upstream pointers for
the pieces the archives lack, and the per-manager install lines
the hints print.
"""

from __future__ import annotations

import os
import shutil

from ..hostsetup import CLOUD_HYPERVISOR_RELEASE, cloud_hypervisor_assets

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


def cloud_hypervisor_hint(machine: str) -> str:
    """The paste-able upstream sequence (#557) for one machine's
    architecture (#831): the static cloud-hypervisor and
    ch-remote pair its release ships, each installed under its
    unsuffixed command name — every line a host command, nothing
    to look up, no tarball that upstream does not ship."""
    vmm_asset, remote_asset = cloud_hypervisor_assets(machine)
    return "\n".join(
        [
            f"curl -LO {CLOUD_HYPERVISOR_RELEASE}/{vmm_asset}",
            f"curl -LO {CLOUD_HYPERVISOR_RELEASE}/{remote_asset}",
            f"sudo install -m755 {vmm_asset} /usr/local/bin/cloud-hypervisor",
            f"sudo install -m755 {remote_asset} /usr/local/bin/ch-remote",
        ]
    )


# Checks whose fix is not a distro package, so every manager
# gets the same hint: the upstream source itself. Doctor runs on
# deployment hosts (Debian and the like) where the repo's own dev
# tooling is absent, so the hints stay host-usable — a command
# sequence the operator can paste, one command a line. The
# cloud-hypervisor entry carries both release binaries — the
# daemon drives the VMM over its --api-socket HTTP API, while
# ch-remote serves the operator's hands-on control (the demo and
# dev tooling) — named for this host's machine: the release's
# assets are per-architecture (#831), the same map hostsetup
# downloads from.
UPSTREAM_HINTS = {
    "cloud-hypervisor": cloud_hypervisor_hint(os.uname().machine),
}

# Alpine names its system-emulator package per target
# architecture (#832): the KVM row's apk entry follows the
# host's machine, the same per-arch shape the VMM hint's
# release assets take (#831).
APK_QEMU_SYSTEM_PACKAGES = {
    "x86_64": "qemu-system-x86_64",
    "aarch64": "qemu-system-aarch64",
}


def apk_qemu_system_package(machine: str) -> str:
    """The apk package that carries one machine's system
    emulator — a machine the table lacks falls back to the
    x86_64 name, the row's historical prescription."""
    return APK_QEMU_SYSTEM_PACKAGES.get(
        machine, APK_QEMU_SYSTEM_PACKAGES["x86_64"]
    )


# Each entry: check name → {manager → package}. A manager missing
# from an entry falls back to the check name in the generic hint.
# The package name differs from the binary it ships on several
# rows: e2fsprogs ships mkfs.ext4/resize2fs/e2fsck, genisoimage
# (Debian family) and cdrtools (pacman, brew) both serve mkisofs,
# qemu-utils/qemu-img is Debian's name for the conversion tool,
# and conntrack comes from conntrack-tools everywhere except apt.
# cloud-hypervisor carries no row: Debian's archive has it not,
# so its hint is the upstream release (UPSTREAM_HINTS).
PACKAGE_HINTS: dict[str, dict[str, str]] = {
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
    # The host-mount daemon (#620, part #622): the name is its
    # own package on the families that carry it; the doctor row
    # is warning-grade — a daemon without allowed host roots
    # never execs it.
    "virtiofsd": {
        "dnf": "virtiofsd",
        "yum": "virtiofsd",
        "apt": "virtiofsd",
        "pacman": "virtiofsd",
        "zypper": "virtiofsd",
        "apk": "virtiofsd",
    },
    "libnetfilter_queue": {
        "dnf": "libnetfilter_queue",
        "yum": "libnetfilter_queue",
        "apt": "libnetfilter-queue1",
        "zypper": "libnetfilter_queue1",
        # Alpine carries the underscored name (aports
        # pkgname=libnetfilter_queue); Homebrew's formula is the
        # hyphenated one, Linux-only.
        "apk": "libnetfilter_queue",
        "pacman": "libnetfilter_queue",
        "brew": "libnetfilter-queue",
    },
    # The header rows retired with #542 (the vendored ctypes
    # binding compiles nothing): a load miss names the runtime
    # soname package alone.
    "libnfnetlink": {
        "dnf": "libnfnetlink",
        "yum": "libnfnetlink",
        "apt": "libnfnetlink0",
        "zypper": "libnfnetlink0",
        "apk": "libnfnetlink",
        "pacman": "libnfnetlink",
    },
    # The KVM stack the /dev/kvm rows point at (#559): the name
    # splits the way the library rows' do — Arch carries KVM in
    # its split qemu packages (qemu-desktop is the x86_64 one;
    # the aarch64 pacman host's answer is open as #837),
    # Alpine's is the arch-named system emulator picked for this
    # host's machine (#832), and brew's formula is qemu under
    # either platform.
    "qemu-kvm": {
        "dnf": "qemu-kvm",
        "yum": "qemu-kvm",
        "apt": "qemu-kvm",
        "zypper": "qemu-kvm",
        "apk": apk_qemu_system_package(os.uname().machine),
        "pacman": "qemu-desktop",
        "brew": "qemu",
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


# The /dev/kvm fix is a group add, not a package install (#559),
# so its table keys on the detected manager the way the hint
# table does, with one command shape per platform family. A
# manager the table lacks — brew on a brew-only Linux host,
# above all — falls back to the Debian form, the documented
# deployment host.
GROUP_ADD: dict[str, str] = {
    "apt": "sudo adduser {user} kvm",
    "dnf": "sudo usermod -aG kvm {user}",
    "yum": "sudo usermod -aG kvm {user}",
    "zypper": "sudo usermod -aG kvm {user}",
    "pacman": "sudo gpasswd -a {user} kvm",
    "apk": "sudo addgroup {user} kvm",
}


def group_add_hint(user: str, manager: str | None) -> str:
    """The command that puts *user* in the kvm group on the
    detected manager's platform."""
    shape = GROUP_ADD.get(manager or "", "sudo adduser {user} kvm")
    return shape.format(user=user)
