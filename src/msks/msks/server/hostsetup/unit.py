"""The systemd system service phase.

``/etc/systemd/system/msksd.service`` running the daemon under
the dedicated msksd account (#1080): the shape the NixOS module
already ships — ``User=``/``Group=``, kvm through the unit's own
``SupplementaryGroups``, the two capabilities ambient and
bounded natively — with the shared socket's path and group
(#1093) through its Environment lines, and the teardown mirror
(#604).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import HostProfile

from ...spec.sockets import system_socket_path
from .links import msksd_sibling
from .profile import DEBIAN_PROFILE, HostProfile
from .users import ACCESS_GROUP, DAEMON_HOME, DAEMON_USER, group_present
from .util import (
    HostSetupError,
    file_exists,
    read_file,
    remove_file,
    sh,
    which,
    write_file,
)
from .wrapper import CAPS

#: Where the system unit lands.
UNIT_PATH = "/etc/systemd/system/msksd.service"

#: The enablement symlink `enable --now` lands (the unit's
#: WantedBy) — the piece `disable --now` is otherwise the only
#: thing that removes, so a teardown without a bus call takes it
#: by path.
SYSTEM_WANTS_PATH = "/etc/systemd/system/multi-user.target.wants/msksd.service"

UNIT_NAME = "msksd.service"

#: The caps the unit grants natively (decision 7): the same pair
#: the wrapper's file capabilities carry, in systemd's own
#: space-separated spelling.
UNIT_CAPS = " ".join(CAPS.split(","))

# The directory whose presence says systemd is the manager (the
# same probe systemctl itself gates on; a container or another
# init leaves it absent).
SYSTEMD_MANAGER = "/run/systemd/system"


def systemd_manages() -> bool:
    """Whether a systemd manager owns this host and systemctl is
    there to reach it — the unit is dead text without both."""
    return Path(SYSTEMD_MANAGER).is_dir() and which("systemctl") is not None


def unit_text(
    msksd: str, uplink_name: str, profile: HostProfile = DEBIAN_PROFILE
) -> str:
    """The msksd.service system unit: the NixOS module's shape
    (#1080) — the daemon under its dedicated account, kvm
    through the unit's own supplementary groups, the two
    capabilities ambient and bounded natively (the wrapper is the
    dev daemon's path, not the service's) — with the shared
    socket's path and group (#1093) through its Environment
    lines and egress enabled against the uplink the tool
    detected. ExecStart resolves the shared venv's msksd
    (#1118 — ``msksd_sibling()`` reads ``/opt/msks/venv/bin``
    first, the deployment's bin sibling as the fallback)."""
    lines = [
        profile.unit_header,
        "[Unit]",
        "Description=msks daemon (workspaces as cloud-hypervisor microvms)",
        "Documentation=https://github.com/mcdonc/msks",
        "After=network-online.target",
        "Wants=network-online.target",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        f"User={DAEMON_USER}",
        f"Group={DAEMON_USER}",
        f"SupplementaryGroups={ACCESS_GROUP} kvm",
        f"AmbientCapabilities={UNIT_CAPS}",
        f"CapabilityBoundingSet={UNIT_CAPS}",
        f'ExecStart="{msksd}"',
        "Environment=MSKSD_EGRESS_ENABLED=true",
    ]
    if uplink_name:
        lines.append(f"Environment=MSKSD_EGRESS_UPLINK={uplink_name}")
    lines += [
        f"Environment=MSKSD_SOCKET={system_socket_path()}",
        f"Environment=MSKSD_SOCKET_GROUP={ACCESS_GROUP}",
        # The state dir env line pins the daemon's tree to the
        # unit's StateDirectory: systemd sets $HOME to the
        # account's home for User= units, and the daemon's own
        # default would otherwise nest under
        # /var/lib/msksd/.local/state/msksd instead of the state
        # directory the unit creates — the NixOS module's own
        # state_dir pin, one env line here.
        f"Environment=MSKSD_STATE_DIR={DAEMON_HOME}",
        "StateDirectory=msksd",
        "RuntimeDirectory=msks",
        "Restart=always",
        "RestartSec=1",
        "TimeoutStopSec=180s",
        "StandardInput=null",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def verify_daemon_binary(msksd: str) -> None:
    """The landing proof: the msksd account can read and execute
    the resolved binary. The unit's ExecStart runs under the
    dedicated account (#1080), and a deployment whose daemon
    lives behind a permission the account cannot cross — a venv
    under a 0700 home — would crash-loop at boot; setpriv drops
    to the account for the test (the script's own path walk, the
    kernel's answer; the interpreter behind its shebang shares
    the traversal chain, and --clear-groups reads stricter than
    the unit's runtime groups), and the refusal names the cause
    at install time (the /opt/msks venv is the durable
    answer)."""
    argv = [
        "setpriv",
        f"--reuid={DAEMON_USER}",
        f"--regid={DAEMON_USER}",
        "--clear-groups",
        "test",
        "-r",
        msksd,
        "-a",
        "-x",
        msksd,
    ]
    rc, _out, err = sh(argv)
    if rc != 0:
        raise HostSetupError(
            f"the {DAEMON_USER} account cannot read and execute "
            f"{msksd} ({err.strip()}) — a venv behind a 0700 home "
            "is the known cause; install the daemon where every "
            "account reaches it"
        )


def write_unit(
    msksd: str,
    uplink_name: str,
    profile: HostProfile = DEBIAN_PROFILE,
) -> bool:
    """Build the system unit and land it at its path. True when a
    different unit was replaced — a re-run that changes the
    daemon's line or its uplink — so the caller can restart a
    running daemon onto the new unit. A missing unit file is a
    first landing; any other read failure is the caller's named
    refusal, not a silent rebuild."""
    try:
        existing = read_file(UNIT_PATH)
    except FileNotFoundError:
        existing = ""
    text = unit_text(msksd, uplink_name, profile)
    write_file(UNIT_PATH, text)
    return existing not in ("", text)


def systemctl(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """One systemctl call against the system manager, as the
    root the tool already holds."""
    rc, _out, err = sh(["systemctl", *args], timeout=timeout)
    return rc, err


def load_unit() -> None:
    """daemon-reload, so the manager reads the unit; a manager
    that cannot answer is a named refusal."""
    rc, err = systemctl(["daemon-reload"])
    if rc != 0:
        raise HostSetupError(f"systemctl daemon-reload failed: {err}")


def start_unit() -> None:
    """Enable and start the service — a first landing's only
    start; the failure names the journal that holds the cause."""
    rc, err = systemctl(["enable", "--now", UNIT_NAME])
    if rc != 0:
        raise HostSetupError(
            f"systemctl enable --now {UNIT_NAME} failed: {err} "
            "(journalctl -u msksd names the cause)"
        )


def restart_running_unit() -> None:
    """Restart the daemon, waited out: a stop stops workspaces
    first (the unit allows 180s), so this call carries a matching
    timeout, and the restart command's own failure is named."""
    rc, err = systemctl(["restart", UNIT_NAME], timeout=240.0)
    if rc != 0:
        raise HostSetupError(
            f"systemctl restart {UNIT_NAME} failed: {err} "
            "(journalctl -u msksd names the cause)"
        )


def activate_unit(changed: bool) -> None:
    """Load the unit, enable and start it, and restart a running
    daemon when the unit changed under it — a fresh landing
    starts through enable alone."""
    load_unit()
    start_unit()
    if not changed:
        return
    rc, _err = systemctl(["is-active", "--quiet", UNIT_NAME])
    if rc == 0:
        restart_running_unit()


def service_skip_cause(msksd: str | None) -> str | None:
    """Why the service phase stands down, as the summary's line
    states it: no msksd beside the tool, no systemd manager, or
    no kvm group for the unit's SupplementaryGroups to name.
    None means the phase proceeds."""
    if msksd is None:
        return (
            "skipped (no msksd beside this tool; the deployment's "
            "daemon is elsewhere — bare msksd unchanged)"
        )
    if not systemd_manages():
        return (
            "skipped (no systemd manager on this host — bare msksd unchanged)"
        )
    if not group_present("kvm"):
        return (
            "skipped (the kvm group is absent — the unit's "
            "SupplementaryGroups names it; install the KVM stack "
            "(qemu-kvm) first, then re-run)"
        )
    return None


def service_note() -> str:
    """The summary's service line."""
    return (
        f"system unit {UNIT_NAME} (enabled, started, running as {DAEMON_USER})"
    )


def install_service(
    uplink_name: str,
    profile: HostProfile = DEBIAN_PROFILE,
) -> str:
    """Land the daemon's systemd system unit (#1080): the
    account phase's msksd account under it, the caps native, the
    shared socket through its Environment lines — and a restart
    when a re-run rewrote a unit that was already running, so a
    refreshed ExecStart or uplink takes effect. The binary the
    unit execs is proven readable and executable by the msksd
    account before anything lands. Returns the summary's service
    line; every stand-down names its cause instead of refusing
    (the host pieces above landed, and a container or an
    elsewhere daemon keeps a working host)."""
    msksd = msksd_sibling()
    cause = service_skip_cause(msksd)
    if cause is not None:
        return cause
    verify_daemon_binary(msksd)
    try:
        changed = write_unit(msksd, uplink_name, profile)
    except OSError as exc:
        raise HostSetupError(f"landing the system unit failed: {exc}") from exc
    activate_unit(changed)
    return service_note()


def stop_unit() -> None:
    """Disable and stop the service, waited out: a stop stops
    workspaces first (the unit allows 180s), so this call carries
    a matching timeout, and the failure names the journal that
    holds the cause — start_unit's refusal, mirrored."""
    rc, err = systemctl(["disable", "--now", UNIT_NAME], timeout=240.0)
    if rc != 0:
        raise HostSetupError(
            f"systemctl disable --now {UNIT_NAME} failed: {err} "
            "(journalctl -u msksd names the cause)"
        )


def forget_unit() -> None:
    """daemon-reload once the unit file is gone, so the manager
    forgets it — load_unit's refusal, mirrored."""
    rc, err = systemctl(["daemon-reload"])
    if rc != 0:
        raise HostSetupError(f"systemctl daemon-reload failed: {err}")


def teardown_unit_files() -> None:
    """Both files leave by path — the unit and the enablement
    symlink `disable --now` would otherwise be the only piece
    that removes."""
    remove_file(UNIT_PATH)
    remove_file(SYSTEM_WANTS_PATH)


def uninstall_service(
    profile: HostProfile = DEBIAN_PROFILE,
) -> str:
    """The service phase's mirror (#604): the stop when the unit
    file is there (a unit file that is gone runs no daemon, so
    no stop call is involved), both files by path, and the reload
    that forgets the unit. Returns the summary's service line."""
    if not systemd_manages():
        return "skipped (no systemd manager on this host)"
    unit_present = file_exists(UNIT_PATH)
    if not unit_present and not file_exists(SYSTEM_WANTS_PATH):
        return f"no {UNIT_NAME} — nothing to remove"
    if unit_present:
        stop_unit()
    teardown_unit_files()
    forget_unit()
    return uninstall_note(unit_present)


def uninstall_note(unit_present: bool) -> str:
    """The summary's service line for one teardown: the full stop
    when the unit file was there, the by-path note when it was
    already gone."""
    if unit_present:
        return f"system unit {UNIT_NAME} stopped, disabled, and removed"
    return (
        f"the {UNIT_NAME} unit file was already gone; its "
        "enablement symlink is removed"
    )
