"""The account phase: the msksd account, the msks access group,
the executing user.

Who invoked the tool (SUDO_USER under sudo), the dedicated
account the system unit runs the daemon under (#1080), the
access group the shared socket's connect permission gates on,
the kvm membership a bare run still needs, and the egress
uplink the phases name in their messages.
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

#: The dedicated account the system unit runs the daemon under
#: (#1080): a system account, nologin, whose home is the daemon's
#: state directory — the NixOS module's own naming.
DAEMON_USER = "msksd"
DAEMON_HOME = "/var/lib/msksd"
NOLOGIN = "/usr/sbin/nologin"

#: The access group (#1080): membership is the deliberate,
#: auditable grant — the socket's group gate (#555's policy, the
#: #1093 chgrp) reads it, and the wrapper's execute permission
#: rides on it (decision 7).
ACCESS_GROUP = "msks"


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


def user_present(name: str) -> bool:
    """Whether the account database carries *name* (an NSS
    failure answers no — the same degrade group_present takes)."""
    try:
        pwd_getpwnam(name)
    except KeyError, OSError:
        return False
    return True


def ensure_daemon_account() -> str:
    """The msksd system account (#1080): a system account,
    nologin, home /var/lib/msksd — the unit's ``User=`` and
    ``StateDirectory=`` pair. Idempotent on re-run; returns the
    summary's phrase for it."""
    if user_present(DAEMON_USER):
        return f"the {DAEMON_USER} system account already present"
    argv = [
        "useradd",
        "-r",
        "--home-dir",
        DAEMON_HOME,
        "--shell",
        NOLOGIN,
        DAEMON_USER,
    ]
    rc, _out, err = sh(argv)
    if rc != 0:
        raise HostSetupError(f"{' '.join(argv)} failed: {err}")
    return f"the {DAEMON_USER} system account created (nologin, {DAEMON_HOME})"


def ensure_access_group(user: str, profile: HostProfile) -> str:
    """The msks access group and the executing user's grant
    (#1080): ``groupadd --system`` when the database carries no
    group, then the deliberate membership that keeps the
    single-user workstation at zero new friction. Idempotent on
    re-run; returns the summary's phrase for the pair."""
    notes: list[str] = []
    if not group_present(ACCESS_GROUP):
        argv = ["groupadd", "--system", ACCESS_GROUP]
        rc, _out, err = sh(argv)
        if rc != 0:
            raise HostSetupError(f"{' '.join(argv)} failed: {err}")
        notes.append(f"the {ACCESS_GROUP} group created")
    else:
        notes.append(f"the {ACCESS_GROUP} group already present")
    if in_group(user, ACCESS_GROUP):
        notes.append(f"{user} holds {ACCESS_GROUP}")
    else:
        argv = profile.group_add(user, ACCESS_GROUP)
        rc, _out, err = sh(argv)
        if rc != 0:
            raise HostSetupError(f"{' '.join(argv)} failed: {err}")
        notes.append(
            f"{user} added to {ACCESS_GROUP} — a fresh login picks "
            "the group up"
        )
    return "; ".join(notes)


def ensure_accounts(user: str, profile: HostProfile = DEBIAN_PROFILE) -> str:
    """Land the system deployment's accounts (#1080): the msksd
    system account (nologin, /var/lib/msksd — the unit's
    ``User=``/``StateDirectory=`` pair), the msks access group,
    and the executing user's grant — the deliberate membership
    that keeps the single-user workstation at zero new friction.
    Every piece is idempotent on re-run; the daemon reaches kvm
    through the unit's ``SupplementaryGroups``, so the human's
    kvm membership is never the daemon's path in. Returns the
    summary's account line."""
    return "; ".join(
        (
            ensure_daemon_account(),
            ensure_access_group(user, profile),
        )
    )


def remove_grant(user: str, profile: HostProfile) -> str:
    """The executing user's grant leaves first: groupdel is
    cleaner with no members this tool added."""
    if group_present(ACCESS_GROUP) and in_group(user, ACCESS_GROUP):
        argv = profile.group_remove(user, ACCESS_GROUP)
        rc, _out, err = sh(argv)
        if rc != 0:
            raise HostSetupError(f"{' '.join(argv)} failed: {err}")
        return (
            f"removed {user} from {ACCESS_GROUP} "
            f"({profile.group_restore(user, ACCESS_GROUP)} restores it)"
        )
    return f"{user} holds no {ACCESS_GROUP} membership"


def remove_daemon_account() -> str:
    """The msksd account goes whole; a process still running
    under it blocks the delete, and the refusal names that."""
    if not user_present(DAEMON_USER):
        return f"the {DAEMON_USER} account is absent"
    argv = ["userdel", DAEMON_USER]
    rc, _out, err = sh(argv)
    if rc != 0:
        raise HostSetupError(
            f"userdel {DAEMON_USER} failed: {err} "
            "(a running process under the account blocks it; "
            "stop the daemon first)"
        )
    return f"the {DAEMON_USER} account removed"


def remove_access_group() -> str:
    """The msks group goes once the grant is gone."""
    if not group_present(ACCESS_GROUP):
        return f"the {ACCESS_GROUP} group is absent"
    argv = ["groupdel", ACCESS_GROUP]
    rc, _out, err = sh(argv)
    if rc != 0:
        raise HostSetupError(f"groupdel {ACCESS_GROUP} failed: {err}")
    return f"the {ACCESS_GROUP} group removed"


#: The ownership repair a reinstall may need: a fresh useradd -r
#: draws the next free system uid, and the kept state tree still
#: carries the deleted account's. The line names the command; the
#: tree can hold tens of gigabytes of images, and the tool does
#: not walk it.
OWNERSHIP_REPAIR = f"chown -R msksd:msksd {DAEMON_HOME}"


def remove_accounts(user: str, profile: HostProfile = DEBIAN_PROFILE) -> str:
    """The account phase's mirror: the executing user's grant,
    the msksd account, the msks group — /var/lib/msksd stays:
    the VM overlays, images, and secrets there survive by design.
    The line names the kept directory and the ownership repair a
    reinstall needs when the new msksd account draws a different
    system uid than the one that owned the tree."""
    return "; ".join(
        (
            remove_grant(user, profile),
            remove_daemon_account(),
            remove_access_group(),
            f"{DAEMON_HOME} stays (VM overlays, images, and secrets "
            f"survive by design; a reinstall that draws a new system "
            f"uid repairs the ownership: {OWNERSHIP_REPAIR})",
        )
    )


def ensure_kvm_membership(
    user: str, profile: HostProfile = DEBIAN_PROFILE
) -> tuple[str, bool]:
    """Make sure the executing user reaches /dev/kvm (#560):
    bare runs (msks-dev, a hand-started msksd) need the kvm group
    on the account — the tool adds the user when missing. The
    system unit's daemon is not this path's business: it reaches
    kvm through the unit's ``SupplementaryGroups`` (#1080).
    Returns the summary's /dev/kvm line and whether the
    membership was added this run."""
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
    """The operator the account phase grants and the profile
    block serves: name, uid, gid, home — SUDO_USER when sudo
    lifted the tool to root (the account that typed the command),
    the effective account otherwise (a root shell's own choice).
    The gid chowns the profile block into that user's hands."""
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
        f"removed {user} from kvm ({profile.group_restore(user, 'kvm')} "
        "restores it)"
    )
