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
   tool it execs. The grant surface is the wrapper's execute
   permission: any local user able to run it holds the two
   capabilities through it — the same grant the NixOS dev host's
   wrapper makes.
2. The routing kernel settings egress verifies or needs
   (docs/networking.md): net.ipv4.ip_forward=1 as a boot-time
   sysctl.d setting — the daemon reads the sysctl at startup and
   refuses every egress workspace with the cause named when it
   reads 0 — and the netfilter/tun modules at boot, mirroring
   nix/module.nix's boot.kernelModules plus the REJECT and NFQUEUE
   pieces the consent chain's rulesets reference.
3. The systemd user service (#560), on a host systemd manages:
   the tool writes ``~/.config/systemd/user/msksd.service`` for
   the executing user — the operator who invoked the tool
   (``SUDO_USER`` under sudo), whose own state dir, image
   catalog, and #555 socket identity the service inherits —
   execs the daemon through the wrapper (the wrapper's file
   capabilities grant any executor the two caps, so a user unit
   holds the same posture a root unit would), creates the #555
   socket's runtime directory under the user's own
   ``$XDG_RUNTIME_DIR``, and ``loginctl enable-linger`` keeps
   the user manager — and the daemon — running across logouts
   and boots. A host without systemd (a container, a non-systemd
   init), a deployment whose msksd lives elsewhere, or
   ``--no-service`` keeps today's behavior: every host piece
   above, no unit.

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
arrives through the kvm
group on the daemon's user, the same supplementary-group grant the
NixOS unit makes — the tool adds the executing user to the group
when missing (#560; bare runs and new logins need it on the
account). A user manager that was already running forked before
the group write reaches it — the daemon holds the old groups
until that manager restarts, and the service line says so.
Idempotent: re-run any time. Re-run after a util-linux
upgrade — the setpriv copy under /usr/local keeps running the old
binary until refreshed.

``--uninstall`` (#604) is the mirror, in teardown order: the
service stops first (workspaces included, inside the unit's
180s stop grace — a user manager that is down runs no stop at
all, because no manager means no daemon), then the unit file
and its enablement symlink, the sysctl and
modules files leave (``sysctl --system`` re-applies every
remaining sysctl.d file, so a second owner's setting survives),
the wrapper, the client links, the kvm membership (the tool
keeps no state, so any member leaves — the line names the
restore), the ``~/.profile`` block, and the cloud-hypervisor
binary — while
the apt packages stay (they are shared Debian packages; the
summary names the ones present with the ``apt-get`` line that
removes them). Idempotent the same way: re-run any time; a
piece already gone is a line in the summary, not a refusal,
and the first real refusal stops the run, the setup run's rule.

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
    RETRY_DELAY,
    RUNTIME_DIR_FMT,
    SYSTEMD_MANAGER,
    USER_UNIT_RELATIVE,
    activate_unit,
    disable_linger,
    ensure_unit_parents,
    forget_unit,
    install_service,
    linger,
    load_unit,
    manager_running,
    restart_running_unit,
    service_note,
    service_skip_cause,
    start_unit,
    stop_unit,
    systemd_manages,
    teardown_busless_service,
    teardown_running_service,
    uninstall_service,
    unit_path,
    unit_text,
    user_manager_call,
    user_systemctl,
    wants_path,
    write_unit,
)
from .users import (
    ensure_kvm_membership,
    executing_user,
    group_present,
    in_group,
    pwd_getpwnam,
    pwd_getpwuid,
    remove_kvm_membership,
    uplink,
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
    "executing_user",
    "group_present",
    "in_group",
    "pwd_getpwnam",
    "pwd_getpwuid",
    "remove_kvm_membership",
    "uplink",
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
    "RETRY_DELAY",
    "RUNTIME_DIR_FMT",
    "SYSTEMD_MANAGER",
    "USER_UNIT_RELATIVE",
    "activate_unit",
    "disable_linger",
    "ensure_unit_parents",
    "forget_unit",
    "install_service",
    "linger",
    "load_unit",
    "manager_running",
    "restart_running_unit",
    "service_note",
    "service_skip_cause",
    "start_unit",
    "stop_unit",
    "systemd_manages",
    "teardown_busless_service",
    "teardown_running_service",
    "uninstall_service",
    "unit_path",
    "unit_text",
    "user_manager_call",
    "user_systemctl",
    "wants_path",
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
