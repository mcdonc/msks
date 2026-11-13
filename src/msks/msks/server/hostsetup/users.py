"""The daemon user phase: executing user, kvm group, uplink.

Who invoked the tool (SUDO_USER under sudo), the supplementary
group that grants /dev/kvm, and the egress uplink the phases
name in their messages.
"""

from __future__ import annotations

import grp
import os
import pwd
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import HostProfile

from .profile import DEBIAN_PROFILE, HostProfile
from .util import HostSetupError, sh


def uplink() -> str:
    """The host's default-route interface, for the summary's
    MSKSD_EGRESS_UPLINK hint."""
    rc, out, _err = sh(["ip", "route", "show", "default"])
    if rc != 0:
        return ""
    words = out.split()
    for index, word in enumerate(words[:-1]):
        if word == "dev":
            return words[index + 1]
    return ""


def in_group(user: str, name: str) -> bool:
    """Whether *user* holds group *name* — as its primary group
    or by membership, resolved the same way #555's peer policy
    resolves the socket group: ``getgrouplist`` answers with
    every supplementary group the account database holds for
    the user, including the nesting a directory backend (SSSD
    with AD) expands itself. A database that answers nothing
    says no, and the caller's adduser is idempotent either
    way."""
    try:
        entry = grp.getgrnam(name)
        account = pwd_getpwnam(user)
        return entry.gr_gid in os.getgrouplist(user, account.pw_gid)
    except KeyError, OSError:
        return False


def ensure_kvm_membership(
    user: str, profile: HostProfile = DEBIAN_PROFILE
) -> tuple[str, bool]:
    """Make sure the executing user reaches /dev/kvm (#560): bare
    runs (msks-dev, a hand-started msksd) need the kvm group on
    the account — the tool adds the user when missing. Returns
    the summary's /dev/kvm line and whether the membership was
    added this run (a user manager already running forked before
    the group existed — its services, this daemon included, hold
    the group only after that manager restarts)."""
    if not group_present("kvm"):
        return (
            "the kvm group is absent — install the KVM stack (qemu-kvm) first",
            False,
        )
    if in_group(user, "kvm"):
        return (
            f"{user} is a member of kvm (the kvm group gates the node)",
            False,
        )
    argv = profile.group_add(user, "kvm")
    rc, _out, err = sh(argv)
    if rc != 0:
        raise HostSetupError(f"{' '.join(argv)} failed: {err}")
    return (
        f"added {user} to kvm — a fresh login picks the group up",
        True,
    )


def group_present(name: str) -> bool:
    """Whether the group database carries *name* (an NSS failure
    answers no — the same degrade kvm_hint's probe took)."""
    try:
        grp.getgrnam(name)
    except KeyError, OSError:
        return False
    return True


def executing_user() -> tuple[str, int, int, str]:
    """The operator the user service belongs to: name, uid, gid,
    home — SUDO_USER when sudo lifted the tool to root (the
    account that typed the command), the effective account
    otherwise (a root shell's own choice). The #555 posture: the
    daemon and its clients share one user, whose state dir, image
    catalog, and token-free socket identity the service keeps;
    the gid chowns the unit's parent chain into that user's
    hands."""
    name = os.environ.get("SUDO_USER")
    entry = None
    if name:
        try:
            entry = pwd_getpwnam(name)
        except KeyError, OSError:
            entry = None
    if entry is None:
        entry = pwd_getpwuid(os.geteuid())
    return entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir


def pwd_getpwnam(name: str):
    """pwd.getpwnam, as a seam the suite can pin (the same shape
    sh and write_file take)."""
    return pwd.getpwnam(name)


def pwd_getpwuid(uid: int):
    """pwd.getpwuid, as a seam the suite can pin."""
    return pwd.getpwuid(uid)


def remove_kvm_membership(
    user: str, profile: HostProfile = DEBIAN_PROFILE
) -> str:
    """The kvm membership's mirror: the account leaves the kvm
    group. The tool cannot prove which run added a membership —
    it keeps no state — so any member leaves, including one the
    tool never added, and the line names the restore because
    /dev/kvm is a node other uses share."""
    if not group_present("kvm"):
        return "the kvm group is absent (nothing was removed)"
    if not in_group(user, "kvm"):
        return f"{user} is not a member of kvm"
    argv = profile.group_remove(user, "kvm")
    rc, _out, err = sh(argv)
    if rc != 0:
        raise HostSetupError(f"{' '.join(argv)} failed: {err}")
    return (
        f"removed {user} from kvm ({profile.group_restore(user)} restores it)"
    )
