"""``msks-setup-debian-host`` (#546): the Debian deployment-host
setup, moved from scripts/setup-debian-host.sh into the wheel —
the host-side pieces msksd needs to run first-level the way the
NixOS deployment host (nix/module.nix) runs it. The daemon itself
(the venv or package install, msksd.yaml, the state dir) stays
whatever deployment brought it; this tool owns the host only:

1. The capability wrapper, the Debian equivalent of the NixOS
   host's security.wrappers.msks-caps (#231): a root-owned copy of
   util-linux setpriv at ``/usr/local/bin/msks-caps`` carrying
   file capabilities CAP_NET_ADMIN and CAP_NET_BIND_SERVICE —
   net_admin for per-VM taps and nftables chains, net_bind_service
   for the egress stack's DHCP 67 and DNS 53 (#101). The wrapper
   keeps setpriv's own CLI, so the dev daemon's exec line runs
   unchanged (``DEV_CAPS=/usr/local/bin/msks-caps`` relocates it).
   The caps arrive ambient, so they flow to the daemon and every
   tool it execs. The system unit grants its caps natively
   (#1080), so the wrapper is the dev daemon's path alone:
   root:msks 0750, executable by root and the msks group's
   members, and no other account — the 0755 shape this replaces
   handed both caps to every local user.
2. The routing kernel settings egress verifies or needs
   (docs/networking.md): net.ipv4.ip_forward=1 as a boot-time
   sysctl.d setting — the daemon reads the sysctl at startup and
   refuses every egress workspace with the cause named when it
   reads 0 — and the netfilter/tun modules at boot, mirroring
   nix/module.nix's boot.kernelModules plus the REJECT and NFQUEUE
   pieces the consent chain's rulesets reference.
3. The system deployment's accounts (#1080): the msksd system
   account (nologin, /var/lib/msksd — the unit's User= and
   StateDirectory= pair), the msks access group the shared
   socket's connect permission gates on (#1093), and the
   executing user's grant — the deliberate, auditable membership
   that keeps the single-user workstation at zero new friction.
   The daemon reaches kvm through the unit's SupplementaryGroups,
   so the human's kvm membership is never the daemon's path in
   (the account keeps its own kvm grant for bare runs).
4. The systemd system service (#560, reshaped by #1080), on a
   host systemd manages: the tool writes
   ``/etc/systemd/system/msksd.service`` running the daemon under
   the msksd account — the shape the NixOS module already ships:
   User=/Group= msksd, kvm through SupplementaryGroups, the two
   capabilities ambient and bounded natively, the shared socket's
   path and group through its Environment lines (#1093), egress
   enabled against the detected uplink. The binary the unit
   execs is proven readable and executable by the msksd account
   before anything lands (a venv behind a 0700 home refuses by
   name; the /opt/msks venv is the durable answer). A host
   without systemd (a container, a non-systemd init), a
   deployment whose msksd lives elsewhere, or ``--no-service``
   keeps today's behavior: every host piece above, no unit. The
   developer's own daemon runs through devenv (development.md),
   not a landed unit.

The plumbing tools the daemon execs (ip, nft, conntrack) install
through apt when missing — and with #598, so does every other
package `msksd doctor` reads: the volume trio's e2fsprogs, the
image pair's qemu-utils and genisoimage, the client workflows'
openssh-client, rsync, and tmux, and the consent stack's two
NFQUEUE soname packages (probed through the linker cache, the
same evidence doctor's library rows read). The VMM lands from
the upstream static release — Debian's archive carries
cloud-hypervisor in no suite — when the host has none, for the
host's own architecture, and the landed binary answers
``--version`` before the run calls it landed; a present binary
stays untouched. The phase runs last, so a host whose egress
passes the Debian mirror but not github.com still finishes
every other piece. The client's console scripts link at
``/usr/local/bin`` (#719, #755) — ``msks`` and, beside it,
``msks-term-popup``, the consent-terminal launcher a
GUI-opened window resolves on its own PATH — each pointing at
the bin sibling this tool's own venv ships, on every login
PATH; a path already holding a live link or a regular file
refuses by name, and a dangling link re-points. /dev/kvm access
for the executing user's own bare runs arrives through the kvm
group on the account — the tool adds the executing user to the
group when missing (#560; the system unit's daemon reaches kvm
through its SupplementaryGroups instead).
Idempotent: re-run any time. Re-run after a util-linux
upgrade — the setpriv copy under /usr/local keeps running the old
binary until refreshed.

``--uninstall`` (#604) is the mirror, in teardown order: the
service stops first (workspaces included, inside the unit's
180s stop grace — a unit file that is gone runs no daemon, so
no stop call is involved), then the unit file and its
enablement symlink, the accounts the deployment created (the
executing user's msks grant, the msksd account, the msks group —
/var/lib/msksd stays: the VM overlays, images, and secrets
there survive by design), the sysctl and modules files leave
(``sysctl --system`` re-applies every remaining sysctl.d file,
so a second owner's setting survives), the wrapper, the client
links, the kvm membership (the tool keeps no state, so any
member leaves — the line names the restore), the ``~/.profile``
block, and the cloud-hypervisor binary — while the apt packages
stay (they are shared Debian packages; the summary names the
ones present with the ``apt-get`` line that removes them).
Idempotent the same way: re-run any time; a piece already gone
is a line in the summary, not a refusal, and the first real
refusal stops the run, the setup run's rule.

The phases are distro-neutral machinery; the per-distro surface
— the tool's name, the package maps and probe names, the
installer verbs, the group commands, the texts that name the
tool — is a HostProfile. DEBIAN_PROFILE below is this entry's
own; the Fedora sibling entry (#635, msks.server.hostsetup_fedora,
``msks-setup-fedora-host``) carries its own into the same run.
"""

import os as os
import subprocess as subprocess
import sys as sys
from pathlib import Path as Path

from . import kernel, links, packages, profile, run, unit, users, util, wrapper
from .kernel import (
    ABSENT,
    CONSENT_MODULES,
    IP_FORWARD_PROC,
    LOAD_FAILED,
    LOADED,
    MODULES,
    MODULES_FILE,
    MODULES_ROOT,
    SYSCTL_FILE,
    SYSCTL_TEXT,
    drop_modules_file,
    drop_sysctl,
    land_modules,
    land_sysctl,
    load_module,
    module_states,
    module_tree_present,
    report_module_states,
    tree_cause,
    warn_absent_modules,
    warn_load_failures,
)
from .links import (
    CLIENT_LINK,
    CLIENT_LINKS,
    POPUP_LINK,
    bin_sibling,
    claim_link,
    drop_client_link,
    link_client,
    link_is_live,
    msksd_sibling,
    place_link,
    relink_or_refuse,
)
from .packages import (
    CLOUD_HYPERVISOR_ASSETS,
    CLOUD_HYPERVISOR_BIN,
    CLOUD_HYPERVISOR_RELEASE,
    cloud_hypervisor_assets,
    cloud_hypervisor_url,
    drop_cloud_hypervisor,
    fetch_upstream,
    install_cloud_hypervisor,
    install_packages,
    missing_packages,
    missing_soname_packages,
    present_packages,
    tool_present,
)
from .profile import (
    DEBIAN_PATH,
    DEBIAN_PROFILE,
    MODULES_HEADER,
    PROFILE_MARKER,
    PROFILE_PATH_BLOCK,
    SONAME_PACKAGES,
    TOOL,
    TOOL_PACKAGES,
    HostProfile,
    drop_user_path,
    kernel_package,
    land_user_path,
    profile_text,
    write_profile,
)
from .run import (
    check_root_and_platform,
    consent_line,
    main,
    parse_args,
    refuse_below_floor,
    run_setup,
    run_uninstall,
    summary,
    uninstall_summary,
)
from .unit import (
    SYSTEM_WANTS_PATH,
    SYSTEMD_MANAGER,
    UNIT_CAPS,
    UNIT_NAME,
    UNIT_PATH,
    activate_unit,
    forget_unit,
    install_service,
    load_unit,
    restart_running_unit,
    service_note,
    service_skip_cause,
    start_unit,
    stop_unit,
    systemctl,
    systemd_manages,
    teardown_unit_files,
    uninstall_service,
    unit_text,
    verify_daemon_binary,
    write_unit,
)
from .users import (
    ACCESS_GROUP,
    DAEMON_HOME,
    DAEMON_USER,
    NOLOGIN,
    ensure_accounts,
    ensure_kvm_membership,
    executing_user,
    group_present,
    in_group,
    pwd_getpwnam,
    pwd_getpwuid,
    remove_accounts,
    remove_kvm_membership,
    uplink,
    user_present,
)
from .util import (
    OS_RELEASE,
    PATH_EXTRA_DIRS,
    HostSetupError,
    am_root,
    chown_path,
    file_exists,
    invoked_as,
    os_release_id,
    os_release_version_id,
    read_file,
    remove_file,
    replace_file,
    say,
    sh,
    silent_unlink,
    version_tuple,
    which,
    write_file,
)
from .wrapper import (
    CAPS,
    WRAPPER,
    drop_wrapper,
    granted_caps,
    install_wrapper,
    verify_wrapper_caps,
    wrapper_ambient_proof,
)

# The submodules a test's patch-all helper walks: patching a name
# on the package root rebinds nothing a phase sees, so the suites
# patch each defining and importing submodule instead.
SUBMODULES = (
    kernel,
    links,
    packages,
    profile,
    run,
    unit,
    users,
    util,
    wrapper,
)

__all__ = [
    "HostSetupError",
    "OS_RELEASE",
    "PATH_EXTRA_DIRS",
    "am_root",
    "chown_path",
    "file_exists",
    "invoked_as",
    "os_release_id",
    "os_release_version_id",
    "read_file",
    "remove_file",
    "replace_file",
    "say",
    "sh",
    "silent_unlink",
    "version_tuple",
    "which",
    "write_file",
    "DEBIAN_PATH",
    "DEBIAN_PROFILE",
    "HostProfile",
    "MODULES_HEADER",
    "PROFILE_MARKER",
    "PROFILE_PATH_BLOCK",
    "SONAME_PACKAGES",
    "TOOL",
    "TOOL_PACKAGES",
    "drop_user_path",
    "kernel_package",
    "land_user_path",
    "profile_text",
    "write_profile",
    "CLOUD_HYPERVISOR_ASSETS",
    "CLOUD_HYPERVISOR_BIN",
    "CLOUD_HYPERVISOR_RELEASE",
    "cloud_hypervisor_assets",
    "cloud_hypervisor_url",
    "drop_cloud_hypervisor",
    "fetch_upstream",
    "install_cloud_hypervisor",
    "install_packages",
    "missing_packages",
    "missing_soname_packages",
    "present_packages",
    "tool_present",
    "CAPS",
    "WRAPPER",
    "drop_wrapper",
    "granted_caps",
    "install_wrapper",
    "verify_wrapper_caps",
    "wrapper_ambient_proof",
    "ABSENT",
    "CONSENT_MODULES",
    "IP_FORWARD_PROC",
    "LOADED",
    "LOAD_FAILED",
    "MODULES",
    "MODULES_FILE",
    "MODULES_ROOT",
    "SYSCTL_FILE",
    "SYSCTL_TEXT",
    "drop_modules_file",
    "drop_sysctl",
    "land_modules",
    "land_sysctl",
    "load_module",
    "module_states",
    "module_tree_present",
    "report_module_states",
    "tree_cause",
    "warn_absent_modules",
    "warn_load_failures",
    "ensure_kvm_membership",
    "ACCESS_GROUP",
    "DAEMON_HOME",
    "DAEMON_USER",
    "NOLOGIN",
    "ensure_accounts",
    "executing_user",
    "group_present",
    "in_group",
    "pwd_getpwnam",
    "pwd_getpwuid",
    "remove_accounts",
    "remove_kvm_membership",
    "uplink",
    "user_present",
    "CLIENT_LINK",
    "CLIENT_LINKS",
    "POPUP_LINK",
    "bin_sibling",
    "claim_link",
    "drop_client_link",
    "link_client",
    "link_is_live",
    "msksd_sibling",
    "place_link",
    "relink_or_refuse",
    "SYSTEMD_MANAGER",
    "SYSTEM_WANTS_PATH",
    "UNIT_CAPS",
    "UNIT_NAME",
    "UNIT_PATH",
    "activate_unit",
    "forget_unit",
    "install_service",
    "load_unit",
    "restart_running_unit",
    "service_note",
    "service_skip_cause",
    "start_unit",
    "stop_unit",
    "systemctl",
    "systemd_manages",
    "teardown_unit_files",
    "uninstall_service",
    "unit_text",
    "verify_daemon_binary",
    "write_unit",
    "check_root_and_platform",
    "consent_line",
    "main",
    "parse_args",
    "refuse_below_floor",
    "run_setup",
    "run_uninstall",
    "summary",
    "uninstall_summary",
]
