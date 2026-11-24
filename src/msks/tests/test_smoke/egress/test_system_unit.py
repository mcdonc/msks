"""The system unit's privileged proofs (#1094): the account
phase creates the msksd account and the msks group against a
real account database, idempotently, and the uninstall mirror
takes both down again with /var/lib/msksd named as kept. The
unit text the phase lands parses under the host's own systemd
(``systemd-analyze verify``) with the ExecStart pointed at a
real binary — and on a host whose systemd manages, the unit
boots for real: enabled under the msksd account, its ExecStart
reads a nonzero ambient-capability mask out of its own
/proc, the native grant (decision 7) working. Root-only:
useradd/groupadd/userdel need it. No guest, no egress, no
/dev/kvm — the egress lane runs this beside the #1093
shared-socket proof.
"""

import grp
import os
import pwd
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from msks.server import hostsetup

root_only = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="root only: creating accounts and groups needs it",
)


def sh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


@pytest.fixture()
def deployment_host():
    """A real account database brought to the deployed state by
    the phase's own commands: the msksd account, the msks group,
    the executing user's grant — and back down in the mirror
    (the home directory stays: useradd -r never creates it, and
    the mirror's contract names it kept). The Debian profile's
    grant verb is adduser: a NixOS dev host ships none, and the
    egress lane's Debian/Fedora runner is the audience."""
    if os.geteuid() != 0:
        pytest.skip("root only: creating accounts and groups needs it")
    if shutil.which("adduser") is None:
        pytest.skip("the Debian group commands (adduser) are absent")
    note = hostsetup.ensure_accounts("root", hostsetup.DEBIAN_PROFILE)
    try:
        yield note
    finally:
        teardown = hostsetup.remove_accounts("root", hostsetup.DEBIAN_PROFILE)
        assert "the msksd account removed" in teardown
        assert "the msks group removed" in teardown


@root_only
def test_the_account_phase_lands_and_mirrors(deployment_host) -> None:
    entry = pwd.getpwnam(hostsetup.DAEMON_USER)
    assert entry.pw_dir == hostsetup.DAEMON_HOME
    assert entry.pw_shell == hostsetup.NOLOGIN
    # A system account: the uid sits inside the system range.
    assert entry.pw_uid < 1000
    assert group_exists(hostsetup.ACCESS_GROUP)
    assert "system account" in deployment_host
    # The re-run is idempotent against the real database: every
    # piece already present, no command, and the same note shape
    # (the first note may read either phrase when a prior run
    # left the accounts behind).
    again = hostsetup.ensure_accounts("root", hostsetup.DEBIAN_PROFILE)
    assert "already present" in again
    assert f"root holds {hostsetup.ACCESS_GROUP}" in again


def test_the_unit_text_parses_under_systemd(tmp_path: Path) -> None:
    """The landed unit's own parser accepts the text: every
    directive the phase writes — the account lines, the caps, the
    Environment spellings — is systemd-shaped before any boot
    reads it. verify needs an executable ExecStart, so the binary
    is the host's own `true`; an unrelated unit's warning on the
    host (verify loads the manager whole) is not this unit's
    verdict."""
    verify = shutil.which("systemd-analyze")
    true = shutil.which("true")
    if verify is None or true is None:
        pytest.skip("systemd-analyze not present on this host")
    unit = tmp_path / "msksd.service"
    unit.write_text(hostsetup.unit_text(true, "lo"))
    done = sh([verify, "verify", str(unit)])
    complaints = [
        line
        for line in (done.stdout + done.stderr).splitlines()
        if unit.name in line
    ]
    assert not complaints, complaints


def systemd_manages() -> bool:
    """The same probe the phase gates on: a manager that owns the
    host and the tool to reach it."""
    return (
        Path(hostsetup.SYSTEMD_MANAGER).is_dir()
        and shutil.which("systemctl") is not None
    )


@pytest.fixture()
def running_unit(deployment_host):
    """The unit booted for real through the phase's own calls:
    the binary is a probe script the msksd account can exec (a
    world-traversable directory — a pytest tmp dir is 0700), the
    landing proof runs against it, and activate_unit enables and
    starts the service. The probe records its own CapAmb and
    sleeps; teardown takes the service down through the mirror's
    own calls."""
    if not systemd_manages():
        pytest.skip("no systemd manager on this host")
    if shutil.which("adduser") is None:
        pytest.skip("the Debian group commands (adduser) are absent")
    if Path(hostsetup.UNIT_PATH).exists():
        pytest.skip(
            f"a unit is already installed at {hostsetup.UNIT_PATH} — "
            "this proof writes that path"
        )
    run_dir = Path(tempfile.mkdtemp(prefix="msks-unit-", dir="/tmp"))
    run_dir.chmod(0o755)
    ambient = run_dir / "capamb"
    probe = run_dir / "msksd-probe"
    probe.write_text(
        "#!/bin/sh\n"
        "sed -n 's/^CapAmb:[[:space:]]*//p' /proc/self/status "
        f"> {ambient}\n"
        "exec sleep 300\n"
    )
    probe.chmod(0o755)
    drop_in = Path("/etc/systemd/system/msksd.service.d")
    drop_in.mkdir(parents=True, exist_ok=True)
    (drop_in / "smoke.conf").write_text(
        # The deployed unit orders itself after network-online.target
        # (the daemon binds a listener and NATs through the uplink);
        # the CI runner's network-online job can take minutes, and
        # this proof is about the privilege shape, not the ordering.
        # Empty assignments reset the lists.
        "[Unit]\nAfter=\nWants=\n"
    )
    started = False
    try:
        hostsetup.verify_daemon_binary(str(probe))
        hostsetup.write_unit(str(probe), "lo")
        hostsetup.activate_unit(False)
        started = True
        yield ambient
    finally:
        # The stop is only the running service's; a landing that
        # failed partway leaves files no daemon serves, and the
        # by-path teardown plus reload still cleans them.
        if started:
            hostsetup.stop_unit()
        hostsetup.teardown_unit_files()
        hostsetup.forget_unit()
        shutil.rmtree(drop_in, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)


@root_only
def test_the_unit_boots_under_the_account_with_native_caps(
    running_unit,
) -> None:
    """The fresh-boot shape (#1094's acceptance): enable --now
    starts the service under the msksd account, and the ExecStart
    process holds the unit's two caps ambient — the native grant
    the wrapper used to carry, read from the process's own
    /proc."""
    ambient = running_unit
    mask = ""
    for _ in range(150):
        active = sh(["systemctl", "is-active", "msksd.service"])
        if ambient.exists():
            mask = ambient.read_text().strip()
            break
        assert active.stdout.strip() != "failed", active.stdout
        time.sleep(0.2)
    assert mask not in ("", "0000000000000000")
    assert sh(["systemctl", "is-active", "msksd.service"]).returncode == 0
