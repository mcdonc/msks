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

from __future__ import annotations

import grp
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import load_settings
from ..microvm.fsdaemons import (
    virtiofsd_floor_message,
    virtiofsd_meets_floor,
    virtiofsd_version,
)
from ..settings import Settings
from .hostsetup import (
    PATH_EXTRA_DIRS,
    executing_user,
    group_present,
    in_group,
    os_release_id,
)

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
# tooling is absent, so the hints stay host-usable — a command
# sequence the operator can paste, one command a line.
UPSTREAM_HINTS = {
    # The release ships pre-built static binaries — cloud-hypervisor
    # itself and ch-remote — as x86-64 assets with -aarch64
    # siblings; the daemon drives the VMM over its --api-socket
    # HTTP API, while ch-remote serves the operator's hands-on
    # control (the demo and dev tooling). The sequence installs
    # both under their unsuffixed names with curl and install(1) —
    # tools every deployment host already has.
    "cloud-hypervisor": "\n".join(
        [
            "curl -LO "
            "https://github.com/cloud-hypervisor/cloud-hypervisor"
            "/releases/latest/download/cloud-hypervisor-static",
            "curl -LO "
            "https://github.com/cloud-hypervisor/cloud-hypervisor"
            "/releases/latest/download/ch-remote-static",
            "sudo install -m755 cloud-hypervisor-static"
            " /usr/local/bin/cloud-hypervisor",
            "sudo install -m755 ch-remote-static /usr/local/bin/ch-remote",
        ]
    ),
}

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
    # its split qemu packages (qemu-desktop is the x86_64 one),
    # Alpine's is the arch-named system emulator, and brew's
    # formula is qemu under either platform.
    "qemu-kvm": {
        "dnf": "qemu-kvm",
        "yum": "qemu-kvm",
        "apt": "qemu-kvm",
        "zypper": "qemu-kvm",
        "apk": "qemu-system-x86_64",
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


def admin_dir_candidate(name: str) -> str | None:
    """The first admin directory carrying an executable *name*."""
    for directory in PATH_EXTRA_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def probe_which(name: str) -> str | None:
    """Find a binary on the daemon-shaped search path (#601): the
    caller's own PATH first, then Debian's admin directories —
    the systemd user manager's default PATH carries them, so
    the daemon finds what lives in /usr/sbin. A login shell
    without the admin directories therefore reports the
    daemon's truth, not the shell's."""
    found = shutil.which(name)
    if found is not None:
        return found
    if os.pathsep in name or "/" in name:
        return None  # an explicit path needs no extension
    return admin_dir_candidate(name)


def rescued_from_admin_dirs(name: str) -> bool:
    """Whether the daemon-shaped path found a binary the caller's
    own PATH cannot see (#601) — the relogin note's trigger."""
    return probe_which(name) is not None and shutil.which(name) is None


def session_lacks_admin_dirs() -> bool:
    """Whether the caller's own PATH skips /usr/sbin (#601) — the
    directory the relogin note names; /usr/local/sbin alone does
    not carry the tools the note is about."""
    session = os.environ.get("PATH", "")
    return "/usr/sbin" not in session.split(os.pathsep)


def setup_tool_name() -> str | None:
    """The host-setup tool this host's os-release names — the two
    console entries the wheel ships, one per targeted distro. A
    distro neither entry targets answers None, and the relogin
    hint keeps the export alone."""
    return {
        "debian": "msks-setup-debian-host",
        # the derivatives carry their own IDs over the Debian
        # package set (#639)
        "ubuntu": "msks-setup-debian-host",
        "linuxmint": "msks-setup-debian-host",
        "fedora": "msks-setup-fedora-host",
        # the Asahi spin carries its own ID (ID_LIKE names fedora)
        "fedora-asahi-remix": "msks-setup-fedora-host",
    }.get(os_release_id())


def path_note_result() -> CheckResult:
    """The relogin encouragement (#601): a warning row, never an
    error. The message names both deployment shapes honestly — a
    daemon under the systemd user unit finds the directories on
    its own PATH, while a hand-started or dev daemon inherits
    this session's PATH and needs the same fix."""
    tool = setup_tool_name()
    relogin = "log out and back in"
    if tool:
        relogin += f" ({tool} adds the directories to ~/.profile)"
    return CheckResult(
        name="PATH",
        ok=False,
        is_warning=True,
        message=(
            "this session's PATH lacks /usr/sbin, where the "
            "deployment host's packaging may keep nft, conntrack, "
            "and the e2fsprogs trio; a "
            "daemon started from it inherits the gap, while the "
            "systemd user unit's own PATH carries the directories"
        ),
        hint=f"{relogin}, or export PATH=$PATH:/usr/sbin:/sbin",
    )


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
    path = probe_which(name)
    if not path:
        return binary_missing(name, manager, is_warning=is_warning, use=use)
    if not probe:
        return CheckResult(name=name, ok=True, message=f"{name} ok ({path})")
    # The probe execs the resolved path, never the bare name: a
    # tool found only through the admin directories would exec
    # through the session PATH again and miss (#601 review).
    rc, _out, err = run([path, *probe[1:]])
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


def check_virtiofsd(binary: str, manager: str | None) -> CheckResult:
    """The host-mount daemon's check (#655): presence, then the
    version floor. The mounts' argument vectors carry
    ``--translate-uid``/``--translate-gid`` squash mappings and
    ``--readonly``, which landed upstream in virtiofsd 1.13.0 —
    older builds parse none of them and exit at start (Linux
    Mint 22's archive ships 1.10.0), so a below-floor binary
    warns with the gap named instead of passing a check whose
    mounts then fail one start at a time. Warning grade like the
    presence check it replaces: a daemon without allowed host
    roots never execs virtiofsd."""
    use = "serves host mounts into each workspace (#620)"
    path = probe_which(binary)
    if not path:
        return binary_missing(binary, manager, is_warning=True, use=use)
    rc, out, err = run([path, "--version"])
    if rc != 0:
        detail = (err or f"exit {rc}").strip()[:200]
        return CheckResult(
            name=binary,
            ok=False,
            is_warning=True,
            message=f"{binary} found at {path} but probe failed: {detail}",
            hint=install_hint(binary, manager),
        )
    return virtiofsd_version_result(binary, path, virtiofsd_version(out))


def virtiofsd_version_result(
    binary: str, path: str, version: tuple[int, ...] | None
) -> CheckResult:
    """The version half of the virtiofsd row: the floor verdict
    for a binary whose ``--version`` answered."""
    if not virtiofsd_meets_floor(version):
        return CheckResult(
            name=binary,
            ok=False,
            is_warning=True,
            message=virtiofsd_floor_message(version),
        )
    found = ".".join(str(part) for part in version)
    return CheckResult(
        name=binary, ok=True, message=f"virtiofsd {found} ok ({path})"
    )


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
    path = probe_which("ssh")
    if not path:
        return ssh_result(
            manager,
            "ssh not found on PATH (the documented ssh workflow "
            "and the forward-path smoke run over `msks forward`)",
        )
    # The resolved path, never the bare name: a session without
    # the admin directories would exec through it again (#601).
    rc, out, err = run([path, "-V"])
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
    path = probe_which("tmux")
    if not path:
        return CheckResult(
            name="tmux",
            ok=False,
            message="tmux not found on PATH",
            hint=install_hint("tmux", manager),
        )
    # Same as ssh: exec the resolved path (#601).
    rc, out, _err = run([path, "-V"])
    version = parse_tmux_version(out) if rc == 0 else None
    if version is None:
        return tmux_unparseable(out, manager)
    return tmux_version_result(version, path, manager)


# The definitive capability test for the two libraries: the
# vendored ctypes binding (#542) dlopens libnetfilter_queue by
# soname — the same load msks.net.nfqbind performs at import — so
# it exercises exactly what the consent path loads at runtime.
NFQ_LOAD_PROBE = [
    sys.executable,
    "-c",
    "import ctypes\n"
    "for soname in ('libnetfilter_queue.so.1', "
    "'libnetfilter_queue.so'):\n"
    " try:\n"
    "  ctypes.CDLL(soname); break\n"
    " except OSError:\n"
    "  pass\n"
    "else:\n"
    " raise SystemExit(1)",
]


def check_library(name: str, manager: str | None) -> CheckResult:
    """Check a shared library is loadable, through layered probers.

    pkg-config first (it answers when the -dev package
    ships the .pc file), then ``ldconfig -p`` (the runtime soname
    cache, present without any -dev package), then the ctypes
    soname load itself — which also covers hosts whose linker
    database lists nothing (the daemon's environment carries the
    store paths). Each layer that answers "present" passes; all
    three failing is the error.
    """
    via = library_probe_result(name)
    if via is not None:
        return CheckResult(name=name, ok=True, message=f"{name} ok ({via})")
    return CheckResult(
        name=name,
        ok=False,
        message=(
            f"{name} not found via pkg-config, ldconfig, or "
            "the ctypes soname load"
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
    """ "ctypes load" when a soname loads."""
    rc, _out, _err = run(NFQ_LOAD_PROBE)
    return "ctypes load" if rc == 0 else None


def library_probe_result(name: str) -> str | None:
    """The evidence string naming how *name* was found, or None."""
    return (
        pkg_config_evidence(name)
        or ldconfig_evidence(name)
        or import_evidence()
    )


def check_nfq_load(manager: str | None) -> CheckResult:
    """Check the consent binding's library loads — the definitive
    capability test: the vendored ctypes binding (#542) dlopens
    the soname the consent path dlopens. Off Linux the library is
    a Linux package, so the miss names the platform contract; on
    Linux a miss means the runtime soname is absent, and the fix
    names the package that carries it."""
    if sys.platform != "linux":
        return CheckResult(
            name="nfq-binding",
            ok=False,
            message=(
                "libnetfilter_queue is a Linux library — the consent "
                "stack does not run on this platform (#542)"
            ),
            hint="run msksd on a Linux host",
        )
    rc, _out, _err = run(NFQ_LOAD_PROBE)
    if rc == 0:
        return CheckResult(
            name="nfq-binding",
            ok=True,
            message="nfq-binding ok (ctypes load)",
        )
    return CheckResult(
        name="nfq-binding",
        ok=False,
        message=(
            "libnetfilter_queue did not load (the probe tries the "
            "versioned and unversioned sonames) — the consent "
            "stack's vendored binding needs it at runtime"
        ),
        hint=install_hint("libnetfilter_queue", manager),
    )


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


def hint_lines(hint: str) -> list[str]:
    """A hint as printed lines: the first carries the Run: label,
    the rest align beneath it — an upstream hint is a command
    sequence, one command a line, and the sequence reads as the
    operator pastes it."""
    parts = hint.split("\n")
    return [f"    Run:  {parts[0]}"] + [
        f"          {part}" for part in parts[1:]
    ]


def append_result(lines: list[str], result: CheckResult) -> None:
    """One result line with its marker; the hint follows on its own."""
    marker = result_marker(result)
    lines.append(f"  {marker} {result.name}: {result.message}")
    if result.hint:
        lines.extend(hint_lines(result.hint))


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
            lines.extend(hint_lines(result.hint))
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
