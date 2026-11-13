from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import HostProfile

from .links import msksd_sibling
from .profile import DEBIAN_PROFILE, HostProfile
from .users import group_present
from .util import (
    HostSetupError,
    chown_path,
    file_exists,
    read_file,
    remove_file,
    sh,
    which,
    write_file,
)
from .wrapper import WRAPPER

# The service phase's names (#560): the user unit's location
# under the executing user's home, and the directory whose
# presence says systemd is the manager (the same probe systemctl
# itself gates on; a container or another init leaves it absent).
# The service is a user service of the executing user — their
# state dir, image catalog, and #555 socket identity carry over —
# so no system unit, no User=/Group= lines, and no state
# relocation are involved.
USER_UNIT_RELATIVE = ".config/systemd/user/msksd.service"

SYSTEMD_MANAGER = "/run/systemd/system"

# Root's login PATH carries /usr/sbin on Debian; a sudo invocation
# can arrive with a stripped PATH, and setcap/nft/conntrack/modprobe
# live there.


def systemd_manages() -> bool:
    """Whether a systemd manager owns this host and systemctl is
    there to reach it — the unit is dead text without both."""
    return Path(SYSTEMD_MANAGER).is_dir() and which("systemctl") is not None


def unit_path(home: str) -> str:
    """Where the user unit lands: under the executing user's own
    config, the location the user manager loads. The file itself
    is root-owned mode 0644 (write_file's shape); the directory
    chain below the home belongs to the user, so an operator can
    replace the file — the same grant the world-executable
    wrapper already makes."""
    return str(Path(home) / USER_UNIT_RELATIVE)


def user_systemctl(user: str, uid: int, args: list[str]) -> list[str]:
    """One systemctl --user call made as the executing user: the
    tool runs as root, so runuser (util-linux, a package the tool
    already installs) drops to the account and names its runtime
    dir — the one the user manager's socket lives under."""
    return [
        "runuser",
        "-u",
        user,
        "--",
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        "systemctl",
        "--user",
        *args,
    ]


def unit_text(
    msksd: str,
    uplink_name: str,
    profile: HostProfile = DEBIAN_PROFILE,
) -> str:
    """The msksd.service user unit: the wrapper raises the two
    caps ambient (dev-daemon.sh's exec line verbatim — the file
    capabilities grant any executor the same posture a root unit
    would), the #555 local socket's runtime directory lands under
    the user's own $XDG_RUNTIME_DIR (the client's default
    candidate path), and egress runs enabled against the uplink
    the tool detected. A user unit carries no User=/Group=/HOME —
    the user manager is the account."""
    lines = [
        profile.unit_header,
        "[Unit]",
        "Description=msks daemon (workspaces as cloud-hypervisor microvms)",
        "Documentation=https://github.com/mcdonc/msks",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "ExecStart="
        + WRAPPER
        + " --inh-caps=+net_admin,+net_bind_service"
        + " --ambient-caps=+net_admin,+net_bind_service"
        + f' -- "{msksd}"',
        "Environment=MSKSD_EGRESS_ENABLED=true",
    ]
    if uplink_name:
        lines.append(f"Environment=MSKSD_EGRESS_UPLINK={uplink_name}")
    lines += [
        "RuntimeDirectory=msks",
        "Restart=always",
        "RestartSec=1",
        "TimeoutStopSec=180s",
        "StandardInput=null",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


RUNTIME_DIR_FMT = "/run/user/{}"

RETRY_DELAY = 1.0


def manager_running(uid: int) -> bool:
    """Whether the user manager for *uid* is up (its runtime dir
    exists). Read before linger is armed: a manager that was
    already running forked before any group this run adds, so its
    services hold the old supplementary groups."""
    return Path(RUNTIME_DIR_FMT.format(uid)).is_dir()


def ensure_unit_parents(home: str, uid: int, gid: int) -> None:
    """Create the unit's parent chain user-owned. A fresh Debian
    account ships no ~/.config, and write_file's mkdir would land
    root-owned parents there — the user manager's enable step
    writes its symlink as the user and a root-owned parent
    refuses it, on this run and every re-run. Each level the
    chain is missing is created and handed to the executing user;
    levels that exist keep their owner."""
    current = Path(home)
    for part in Path(USER_UNIT_RELATIVE).parts[:-1]:
        current = current / part
        if not current.exists():
            current.mkdir()
            chown_path(current, uid, gid)
            current.chmod(0o755)


def user_manager_call(
    user: str, uid: int, args: list[str], timeout: float = 10.0
) -> tuple[int, str]:
    """One systemctl --user call, with the linger-startup retry:
    enable-linger returns after logind enqueues the user manager,
    so the first call can beat the manager's bus into existence —
    a "Failed to connect to bus" answer retries briefly before it
    is treated as a failure."""
    cmd = user_systemctl(user, uid, args)
    rc, _out, err = sh(cmd, timeout=timeout)
    tries = 0
    while rc != 0 and "Failed to connect to bus" in err and tries < 4:
        time.sleep(RETRY_DELAY)
        rc, _out, err = sh(cmd, timeout=timeout)
        tries += 1
    return rc, err


def write_unit(
    home: str,
    msksd: str,
    uplink_name: str,
    profile: HostProfile = DEBIAN_PROFILE,
) -> bool:
    """Build the user unit and land it at its path. True when a
    different unit was replaced — a re-run that changes the
    daemon's line or its uplink — so the caller can restart a
    running daemon onto the new unit. A missing unit file is a
    first landing; any other read failure is the caller's named
    refusal, not a silent rebuild."""
    path = unit_path(home)
    try:
        existing = read_file(path)
    except FileNotFoundError:
        existing = ""
    text = unit_text(msksd, uplink_name, profile)
    write_file(path, text)
    return existing not in ("", text)


def linger(user: str) -> None:
    """Keep the user manager — and the daemon with it — running
    across logouts and boots. Idempotent; enabling it also starts
    the manager now when no session had."""
    rc, _out, err = sh(["loginctl", "enable-linger", user])
    if rc != 0:
        raise HostSetupError(
            f"loginctl enable-linger {user} failed: {err} "
            "(the daemon would stop at the operator's logout)"
        )


def load_unit(user: str, uid: int) -> None:
    """daemon-reload through the bus-race retry; a manager that
    never answers is a named refusal."""
    rc, err = user_manager_call(user, uid, ["daemon-reload"])
    if rc != 0:
        raise HostSetupError(f"systemctl --user daemon-reload failed: {err}")


def start_unit(user: str, uid: int) -> None:
    """Enable and start the service — a first landing's only
    start; the failure names the journal that holds the cause."""
    rc, err = user_manager_call(
        user, uid, ["enable", "--now", "msksd.service"]
    )
    if rc != 0:
        raise HostSetupError(
            f"systemctl --user enable --now msksd.service failed: {err} "
            f"(as {user}: journalctl --user -u msksd names the cause)"
        )


def restart_running_unit(user: str, uid: int) -> None:
    """Restart the daemon, waited out: a stop stops workspaces
    first (the unit allows 180s), so this call carries a matching
    timeout, and the restart command's own failure is named."""
    rc, err = user_manager_call(
        user, uid, ["restart", "msksd.service"], timeout=240.0
    )
    if rc != 0:
        raise HostSetupError(
            f"systemctl --user restart msksd.service failed: {err} "
            f"(as {user}: journalctl --user -u msksd names the cause)"
        )


def activate_unit(user: str, uid: int, changed: bool) -> None:
    """Linger on, load the unit, enable and start it, and restart
    a running daemon when the unit changed under it — a fresh
    landing starts through enable alone."""
    linger(user)
    load_unit(user, uid)
    start_unit(user, uid)
    if not changed:
        return
    rc, _err = user_manager_call(
        user, uid, ["is-active", "--quiet", "msksd.service"]
    )
    if rc == 0:
        restart_running_unit(user, uid)


def service_skip_cause(msksd: str | None) -> str | None:
    """Why the service phase stands down, as the summary's line
    states it: no msksd beside the tool, or no systemd manager.
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
    return None


def service_note(user: str, uid: int, manager_up_first: bool) -> str:
    """The summary's service line. A user manager that was already
    running forked before any kvm membership this run adds — and a
    re-run cannot prove the manager postdates an older membership
    either — so whenever the manager predates the proof, the line
    names the symptom (workspaces failing on /dev/kvm) and the
    manager restart that hands the daemon the group, with what
    that restart really stops (the user's units; login sessions
    survive it)."""
    note = f"user service msksd.service ({user}, enabled, started, lingering)"
    if manager_up_first:
        note += (
            f" — the manager may predate the kvm membership; if "
            f"workspaces fail on /dev/kvm, restart it: "
            f"sudo systemctl restart user@{uid}.service "
            f"(it stops that user's units — the daemon and its "
            f"workspaces; login sessions survive)"
        )
    return note


def install_service(
    user: str,
    uid: int,
    gid: int,
    home: str,
    uplink_name: str,
    profile: HostProfile = DEBIAN_PROFILE,
) -> str:
    """Land the daemon's systemd user service (#560): the unit in
    the executing user's own config, enable-and-start with linger
    — and a restart when a re-run rewrote a unit that was already
    running, so a refreshed ExecStart or uplink takes effect.
    Returns the summary's service line; every stand-down names
    its cause instead of refusing (the host pieces above landed,
    and a container or an elsewhere daemon keeps a working
    host)."""
    msksd = msksd_sibling()
    cause = service_skip_cause(msksd)
    if cause is not None:
        return cause
    manager_up_first = manager_running(uid) and group_present("kvm")
    try:
        ensure_unit_parents(home, uid, gid)
        changed = write_unit(home, msksd, uplink_name, profile)
        activate_unit(user, uid, changed)
    except OSError as exc:
        raise HostSetupError(f"landing the user unit failed: {exc}") from exc
    return service_note(user, uid, manager_up_first)


def stop_unit(user: str, uid: int) -> None:
    """Disable and stop the service, waited out: a stop stops
    workspaces first (the unit allows 180s), so this call carries
    a matching timeout, and the failure names the journal that
    holds the cause — start_unit's refusal, mirrored."""
    rc, err = user_manager_call(
        user, uid, ["disable", "--now", "msksd.service"], timeout=240.0
    )
    if rc != 0:
        raise HostSetupError(
            f"systemctl --user disable --now msksd.service failed: {err} "
            f"(as {user}: journalctl --user -u msksd names the cause)"
        )


def forget_unit(user: str, uid: int) -> None:
    """daemon-reload once the unit file is gone, so the manager
    forgets it — load_unit's refusal, mirrored."""
    rc, err = user_manager_call(user, uid, ["daemon-reload"])
    if rc != 0:
        raise HostSetupError(f"systemctl --user daemon-reload failed: {err}")


def disable_linger(user: str) -> None:
    """The linger the service phase armed leaves with it; the
    call is idempotent, and the failure names it."""
    rc, _out, err = sh(["loginctl", "disable-linger", user])
    if rc != 0:
        raise HostSetupError(
            f"loginctl disable-linger {user} failed: {err} "
            "(the user manager keeps running across logouts)"
        )


def wants_path(home: str) -> str:
    """The enablement symlink `enable --now` lands (the unit's
    WantedBy) — the piece `disable --now` is otherwise the only
    thing that removes, so a teardown without a bus call takes
    it by path. Derived from USER_UNIT_RELATIVE so the basename
    and the unit's own location stay one name."""
    relative = Path(USER_UNIT_RELATIVE)
    return str(
        Path(home) / relative.parent / "default.target.wants" / relative.name
    )


def teardown_running_service(
    user: str, uid: int, unit: str, wants: str
) -> str:
    """The manager-up teardown: stop and disable (the stop waits
    out the unit's shutdown grace), both files, the reload that
    forgets the unit, and the linger — the summary line names
    linger's restore."""
    stop_unit(user, uid)
    remove_file(unit)
    remove_file(wants)
    forget_unit(user, uid)
    disable_linger(user)
    return (
        f"user service msksd.service stopped, disabled, and "
        f"removed ({user}; lingering off — loginctl "
        f"enable-linger {user} restores it)"
    )


def teardown_busless_service(
    user: str, uid: int, home: str, unit: str, wants: str, manager_up: bool
) -> str:
    """The no-stop teardown: a down user manager runs no daemon,
    and a unit file that is already gone left none behind — the
    files and the linger leave by path, no stop call involved, so
    the teardown holds its every-piece-leaves contract anyway. A
    manager that is up still hears the reload, so it forgets the
    unit the files no longer name. Neither piece on disk is the
    stand-down: the linger hint names the by-hand fix, and the
    tool never removes state it cannot see."""
    unit_present = file_exists(unit)
    if not unit_present and not file_exists(wants):
        return (
            f"no msksd.service under {home} — lingering may remain; "
            f"loginctl disable-linger {user} turns it off"
        )
    remove_file(unit)
    remove_file(wants)
    if manager_up:
        forget_unit(user, uid)
    disable_linger(user)
    if unit_present:
        return (
            f"user service msksd.service removed ({user}; the user "
            "manager was down — no daemon was running; lingering off)"
        )
    return (
        "the msksd.service unit file was already gone; the enablement "
        f"symlink is removed and lingering is off ({user})"
    )


def uninstall_service(user: str, uid: int, home: str) -> str:
    """The service phase's mirror (#604): the full stop when a
    manager is up and the unit file is there, the busless path
    otherwise. Returns the summary's service line."""
    if not systemd_manages():
        return "skipped (no systemd manager on this host)"
    unit = unit_path(home)
    wants = wants_path(home)
    manager_up = manager_running(uid)
    if manager_up and file_exists(unit):
        return teardown_running_service(user, uid, unit, wants)
    return teardown_busless_service(user, uid, home, unit, wants, manager_up)
