from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from .util import (
    HostSetupError,
    chown_path,
    os_release_id,
    read_file,
    remove_file,
    replace_file,
    write_file,
)

TOOL = "msks-setup-debian-host"

# The service phase's names (#560): the user unit's location
# under the executing user's home, and the directory whose
# presence says systemd is the manager (the same probe systemctl
# itself gates on; a container or another init leaves it absent).
# The service is a user service of the executing user — their
# state dir, image catalog, and #555 socket identity carry over —
# so no system unit, no User=/Group= lines, and no state
# relocation are involved.

# Root's login PATH carries /usr/sbin on Debian; a sudo invocation
# can arrive with a stripped PATH, and setcap/nft/conntrack/modprobe
# live there.
DEBIAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

MODULES_HEADER = """\
# msksd egress prerequisites (msks-setup-debian-host): taps, the
# nftables machinery, NAT, conntrack, the deny RST, and the
# interactive consent queue.
"""

# nix/module.nix's boot.kernelModules, plus the reject and NFQUEUE
# pieces its list leaves to autoload (the deny RST's
# `reject with tcp reset`, the interactive queue's `queue num`).

#: The idempotent marker for the ~/.profile block (#601).
PROFILE_MARKER = "# msks: Debian keeps the admin tools in /usr/sbin and /sbin"

#: The block the tool appends to the operator's ~/.profile (#601):
#: guarded additions at the tail of PATH — Debian's own order puts
#: /usr/local and the account's own bins ahead, so the block never
#: shadows a user-local tool that shares a name with an sbin one.

#: The block the tool appends to the operator's ~/.profile (#601):
#: guarded additions at the tail of PATH — Debian's own order puts
#: /usr/local and the account's own bins ahead, so the block never
#: shadows a user-local tool that shares a name with an sbin one.
PROFILE_PATH_BLOCK = f"""\
{PROFILE_MARKER}
# added by msks-setup-debian-host; applies at the next login
case ":$PATH:" in
  *":/usr/sbin:"*) ;;
  *) PATH="$PATH:/usr/sbin" ;;
esac
case ":$PATH:" in
  *":/sbin:"*) ;;
  *) PATH="$PATH:/sbin" ;;
esac
export PATH
"""

# command → package for the wrapper path, the phases the setup
# itself runs, and the daemon's and client's bare-name exec
# surface — the tools `msksd doctor` probes (#598).
TOOL_PACKAGES = {
    "setpriv": "util-linux",
    "setcap": "libcap2-bin",
    "ip": "iproute2",
    "nft": "nftables",
    "conntrack": "conntrack",
    "modprobe": "kmod",
    # the routing phase's load step; procps ships on full Debian
    # and is absent from slim images (#546's container run found it)
    "sysctl": "procps",
    # the workspace-volume trio (#184) and the image tools the
    # daemon execs: the root overlay's prober and the seed
    # disk's packer
    "mkfs.ext4": "e2fsprogs",
    "resize2fs": "e2fsprogs",
    "e2fsck": "e2fsprogs",
    "qemu-img": "qemu-utils",
    "mkisofs": "genisoimage",
    # the client-side pair the documented workflows run over the
    # forward, and the consent terminal's host tmux (3.2 floor)
    "ssh": "openssh-client",
    "rsync": "rsync",
    "tmux": "tmux",
    # the VMM download's own trust root: slim images ship no
    # CA bundle, and urllib refuses the TLS handshake without
    # one. The package's probeable command names it.
    "update-ca-certificates": "ca-certificates",
    # The host-mount daemon (#620): the virtiofsd package
    # installs its binary off PATH (#643) — the probe and the
    # daemon's own default both resolve the archive locations
    # themselves — so apt lands it like every other doctor-read
    # package and a present binary still answers the probe.
    "virtiofsd": "virtiofsd",
}

# soname → package for the consent stack's NFQUEUE libraries
# (#598): no binary answers a `which`, so the linker cache is
# the probe.

# soname → package for the consent stack's NFQUEUE libraries
# (#598): no binary answers a `which`, so the linker cache is
# the probe.
SONAME_PACKAGES = {
    "libnetfilter_queue.so.1": "libnetfilter-queue1",
    "libnfnetlink.so.0": "libnfnetlink0",
}

# The VMM (#598): Debian's archive carries cloud-hypervisor in
# no suite, so the tool lands the documented upstream static
# release itself (docs/installation.md names the same source).
# The release carries one static build per architecture — the
# unsuffixed assets are x86-64, aarch64's carry -aarch64 — and
# each entry names the (VMM, ch-remote) pair that architecture
# ships; doctor's fix hint prints the same pair (#831).


def kernel_package(machine: str, release: str) -> str:
    """The generic flavor's package for this machine — Debian's
    arch names, not uname's machine names (x86_64 installs
    linux-image-amd64, aarch64 linux-image-arm64). The release
    the caller names is the sibling profiles' input; Debian's
    packaging ignores it. Ubuntu and Mint name the flavor
    linux-image-generic on every arch."""
    if os_release_id() in {"ubuntu", "linuxmint"}:
        return "linux-image-generic"
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    return f"linux-image-{arch}"


@dataclass(frozen=True)
class HostProfile:
    """The per-distro surface the shared phases read: this
    tool's name and the platform gate's os-release ID and label,
    the package maps the probes install through, the installer
    and removal verbs, the admin PATH a sudo run needs, and the
    handful of landed texts that name the tool. Debian's
    instance is this module's own constants; the phases
    themselves are one implementation."""

    tool: str
    label: str
    #: every os-release ID the platform gate accepts — the base
    #: ID and the spins that carry their own (Fedora Asahi's is
    #: fedora-asahi-remix).
    os_ids: tuple[str, ...]
    #: os-release ID → minimum VERSION_ID the gate accepts. An ID
    #: absent from the map carries no floor; a floored ID whose
    #: VERSION_ID is missing or below the floor is refused with
    #: the floor named.
    version_floors: dict[str, str]
    description: str
    tool_packages: dict[str, str]
    soname_packages: dict[str, str]
    #: dnf refreshes metadata as part of install; apt-get wants
    #: an explicit update first. None skips the refresh step.
    refresh_command: tuple[str, ...] | None
    install_command: tuple[str, ...]
    install_timeout: float
    #: the manager's own name ("apt", not "apt-get") for the
    #: error lines and the module-absence hint.
    package_manager: str
    remove_command: tuple[str, ...]
    #: extra environment the install runs with (debconf's
    #: noninteractive frontend); an empty dict runs as-is.
    install_env: dict[str, str]
    admin_path: str
    modules_header: str
    profile_marker: str
    profile_path_block: str
    unit_header: str
    kernel_package: Callable[[str, str], str]
    group_add: Callable[[str, str], list[str]]
    group_remove: Callable[[str, str], list[str]]
    group_restore: Callable[[str], str]


DEBIAN_PROFILE = HostProfile(
    tool=TOOL,
    label="Debian",
    # The derivatives carry their own IDs — Ubuntu's ID_LIKE is
    # debian, Linux Mint's is ubuntu debian — and their archives
    # carry the full package set from these floors (#639):
    # Ubuntu gains the virtiofsd package at noble, Mint 22 is
    # noble-based. Mint's LMDE releases number from 6 and sit
    # under the floor; the gate refuses them.
    os_ids=("debian", "ubuntu", "linuxmint"),
    version_floors={"ubuntu": "24.04", "linuxmint": "22"},
    description=(
        "Prepare a Debian deployment host for msksd "
        "(#546): the capability wrapper, the routing kernel "
        "settings, and a systemd service for the daemon (#560)."
    ),
    tool_packages=TOOL_PACKAGES,
    soname_packages=SONAME_PACKAGES,
    refresh_command=("apt-get", "-qq", "update"),
    install_command=("apt-get", "install", "-y"),
    install_timeout=600.0,
    package_manager="apt",
    remove_command=("apt-get", "remove", "--purge"),
    install_env={"DEBIAN_FRONTEND": "noninteractive"},
    admin_path=DEBIAN_PATH,
    modules_header=MODULES_HEADER,
    profile_marker=PROFILE_MARKER,
    profile_path_block=PROFILE_PATH_BLOCK,
    unit_header=(
        "# Generated by msks-setup-debian-host (#560): re-run "
        "the tool to refresh this file."
    ),
    kernel_package=kernel_package,
    group_add=lambda user, group: ["adduser", user, group],
    group_remove=lambda user, group: ["deluser", user, group],
    group_restore=lambda user: f"adduser {user} kvm",
)


def profile_text(profile: Path) -> str:
    """The profile's current text; absent reads as empty, and a
    decode failure is the named refusal (the tool never truncates
    a profile it cannot parse)."""
    try:
        return read_file(str(profile))
    except OSError:
        return ""
    except ValueError:
        raise HostSetupError(
            f"{profile} is not UTF-8 text; add /usr/sbin and /sbin "
            "to your PATH by hand"
        ) from None


def write_profile(
    profile: Path, text: str, uid: int, gid: int, fix: str
) -> None:
    """The profile's atomic write, both directions' shared tail:
    a sibling temp, one rename, the operator's own ownership
    (root-owned dotfiles would lock the account out of its own
    profile). *fix* is the by-hand remediation a refusal names."""
    partial = profile.with_name(".profile.msks-new")
    try:
        write_file(str(partial), text)
        replace_file(partial, profile)
    except OSError as exc:
        # The refusal leaves no partial behind (the VMM phase's
        # #599 pattern): best-effort, the error is the message.
        with contextlib.suppress(OSError):
            Path(partial).unlink(missing_ok=True)
        raise HostSetupError(f"cannot write {profile} ({exc}); {fix}") from exc
    chown_path(profile, uid, gid)


def land_user_path(
    home: str,
    uid: int,
    gid: int,
    profile: HostProfile = DEBIAN_PROFILE,
) -> str:
    """Persist the admin directories on the operator's login PATH
    (#601): an idempotent block in their ~/.profile, so the
    session that typed the command — and the next one — sees the
    tools the package phases land. Re-runs never duplicate the
    block; the marker answers. The file lands atomically and
    stays the operator's own; anything unreadable is a named
    refusal, and a re-run leaves an unparseable profile
    untouched."""
    profile_file = Path(home) / ".profile"
    text = profile_text(profile_file)
    if profile.profile_marker in text:
        return "already there (the next login carries /usr/sbin and /sbin)"
    separator = "" if not text or text.endswith("\n") else "\n"
    fresh = text + separator + profile.profile_path_block
    write_profile(
        profile_file,
        fresh,
        uid,
        gid,
        fix="add /usr/sbin and /sbin to your PATH by hand",
    )
    return f"{profile_file} gains /usr/sbin and /sbin at the next login"


def drop_user_path(
    home: str,
    uid: int,
    gid: int,
    profile: HostProfile = DEBIAN_PROFILE,
) -> str:
    """The ~/.profile block's mirror (#604): the block leaves with
    the tool and the operator's own lines stay — a profile that
    held nothing else was the tool's own file and goes whole. A
    block the operator edited inside stays put with the by-hand
    fix named — the tool never rewrites a profile it cannot
    recognize."""
    profile_file = Path(home) / ".profile"
    text = profile_text(profile_file)
    if profile.profile_marker not in text:
        return f"no msks block in {profile_file}"
    if profile.profile_path_block not in text:
        return (
            f"the block in {profile_file} was edited by hand — remove it "
            "yourself (it runs from the msks marker line to the "
            "export)"
        )
    stripped = text.replace(profile.profile_path_block, "")
    if not stripped.strip():
        remove_file(str(profile_file))
        return f"{profile_file} held only the block (removed)"
    write_profile(
        profile_file,
        stripped.rstrip("\n") + "\n",
        uid,
        gid,
        fix="remove the msks block by hand",
    )
    return f"{profile_file} carries the operator's own lines only"
