"""The system-deployment checks.

The #1080 rows: the msks access group and the caller's grant,
the msksd system unit's state, and the shared /run/msks socket a
serving daemon leaves behind (#1093). Warning grade every one —
a daemon serving TCP alone (the dev daemon, a remote
deployment) keeps a green report without them.
"""

from __future__ import annotations

import grp
import os
import sys
from pathlib import Path

from ...spec.sockets import is_socket_file, system_socket_path
from ..hostsetup import (
    SYSTEMD_MANAGER,
    UNIT_NAME,
    executing_user,
    group_present,
    in_group,
)
from .hints import group_add_hint
from .probes import probe_which, run
from .report import CheckResult

ACCESS_GROUP = "msks"


def group_absent_result() -> CheckResult:
    """The access group is not installed — the shared socket's
    connect gate cannot exist."""
    return CheckResult(
        name="msks-group",
        ok=False,
        is_warning=True,
        message=(
            "the msks group is absent — the shared socket's "
            "connect gate is not installed"
        ),
        hint=(
            "run the host setup tool (msks-setup-debian-host or "
            "msks-setup-fedora-host)"
        ),
    )


def not_a_member_result(user: str, manager: str | None) -> CheckResult:
    """The database knows the group and the caller sits outside
    it — the client cannot connect through the shared socket. The
    fix names the group command the detected manager runs, the
    kvm row's own distro awareness."""
    return CheckResult(
        name="msks-group",
        ok=False,
        is_warning=True,
        message=(
            f"{user} is not in the msks group, which gates the "
            "shared socket's connect permission"
        ),
        hint=f"{group_add_hint(user, ACCESS_GROUP, manager)}, "
        "then log out and back in",
    )


def session_predates_result(user: str) -> CheckResult:
    """The database says member, this session's live groups say
    no — a client started from it still meets EACCES on the
    socket, the kvm row's own trap on the client side."""
    return CheckResult(
        name="msks-group",
        ok=False,
        is_warning=True,
        message=(
            f"{user} is in the msks group, but this session "
            "predates the membership — a client started from it "
            "still cannot connect through the shared socket"
        ),
        hint="log out and back in once the group has landed",
    )


def session_lacks_group(uid: int) -> bool:
    """Whether this session lacks the group the database grants:
    the run is that user's own (a sudo lift cannot speak for the
    user's login), and neither the live supplementary groups nor
    the primary gid carries it."""
    if os.geteuid() != uid:
        return False
    group_gid = grp.getgrnam(ACCESS_GROUP).gr_gid
    return group_gid not in os.getgroups() and os.getgid() != group_gid


def msks_group_result(manager: str | None = None) -> CheckResult:
    """The access-group row (#1080): the group exists, and the
    caller holds it — root passes without holding anything, the
    same bypass every owner check takes."""
    user, uid, _gid, _home = executing_user()
    if not group_present(ACCESS_GROUP):
        return group_absent_result()
    if os.geteuid() == 0:
        return CheckResult(
            name="msks-group",
            ok=True,
            message="the msks group exists (root passes)",
        )
    if not in_group(user, ACCESS_GROUP):
        return not_a_member_result(user, manager)
    if session_lacks_group(uid):
        return session_predates_result(user)
    return CheckResult(
        name="msks-group",
        ok=True,
        message=f"the msks group exists ({user} is a member)",
    )


def no_manager_result() -> CheckResult:
    """No systemd manager, or no systemctl to reach one — the
    system unit cannot run here at all."""
    return CheckResult(
        name="msksd-unit",
        ok=False,
        is_warning=True,
        message=(
            "no systemd manager on this host — the msksd system "
            "unit cannot run here"
        ),
    )


def unit_state_result(state: str) -> CheckResult:
    """The verdict for one is-active answer: active passes, and
    anything else warns with the state and the journal that
    names the cause."""
    return CheckResult(
        name="msksd-unit",
        ok=False,
        is_warning=True,
        message=(
            f"the msksd system unit is not active ({state}) — "
            "journalctl -u msksd names the cause"
        ),
        hint=(
            "systemctl status msksd names the cause; the host "
            "setup tool lands the unit"
        ),
    )


def system_manager_reachable() -> str | None:
    """The systemctl that reaches this host's manager, or None
    when there is nothing to reach (no systemctl, no manager)."""
    systemctl = probe_which("systemctl")
    if systemctl is None or not Path(SYSTEMD_MANAGER).is_dir():
        return None
    return systemctl


def msks_unit_result() -> CheckResult:
    """The system-unit row (#1080): the msksd.service unit is
    loaded and running — the shape a deployment host serves
    /run/msks from."""
    if sys.platform != "linux":  # pragma: no cover — caller guards
        return no_manager_result()
    systemctl = system_manager_reachable()
    if systemctl is None:
        return no_manager_result()
    rc, out, _err = run([systemctl, "is-active", UNIT_NAME])
    if rc == 0:
        return CheckResult(
            name="msksd-unit",
            ok=True,
            message=f"{UNIT_NAME} is active",
        )
    return unit_state_result(out.strip() or "not answering")


def shared_socket_result() -> CheckResult:
    """The shared-socket row (#1093): /run/msks/msksd.sock names
    a socket a serving daemon left behind — the path an
    unconfigured member's client dials."""
    path = system_socket_path()
    if is_socket_file(path):
        return CheckResult(
            name="shared-socket",
            ok=True,
            message=f"{path} answers",
        )
    return CheckResult(
        name="shared-socket",
        ok=False,
        is_warning=True,
        message=(
            f"no daemon is serving the shared socket at {path} "
            "— local clients have nothing to dial"
        ),
        hint="systemctl status msksd names the cause",
    )
