"""The orchestration: the setup and uninstall runs, the CLI.

The phase order the docstring's numbered pieces describe, the
summaries both runs end with, and the argument parser and
``main`` the entry points call.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import HostProfile

from .kernel import (
    CONSENT_MODULES,
    LOADED,
    MODULES,
    MODULES_FILE,
    SYSCTL_FILE,
    drop_modules_file,
    drop_sysctl,
    land_modules,
    land_sysctl,
)
from .links import drop_client_link, link_client
from .packages import (
    drop_cloud_hypervisor,
    install_cloud_hypervisor,
    install_packages,
    missing_packages,
    missing_soname_packages,
    present_packages,
)
from .profile import (
    DEBIAN_PROFILE,
    HostProfile,
    drop_user_path,
    land_user_path,
)
from .unit import install_service, uninstall_service
from .users import (
    ensure_kvm_membership,
    executing_user,
    remove_kvm_membership,
    uplink,
)
from .util import (
    HostSetupError,
    am_root,
    invoked_as,
    os_release_id,
    os_release_version_id,
    say,
    version_tuple,
    which,
)
from .wrapper import (
    CAPS,
    WRAPPER,
    drop_wrapper,
    install_wrapper,
    wrapper_ambient_proof,
)


def refuse_below_floor(profile: HostProfile, release: str) -> None:
    """The archive floor (#639): a floored ID under its minimum
    VERSION_ID is refused with the floor named — the release's
    archive lacks packages the setup installs."""
    floor = profile.version_floors.get(release)
    if floor is None:
        return
    version = version_tuple(os_release_version_id())
    if version is None or version < version_tuple(floor):
        raise HostSetupError(
            f"this tool targets {release} {floor} or newer hosts — "
            f"older releases lack packages the setup installs "
            f"(os-release says "
            f"'{os_release_version_id() or 'unknown'}')"
        )


def check_root_and_platform(
    profile: HostProfile = DEBIAN_PROFILE,
) -> None:
    """The refusals that precede every side effect."""
    if not am_root():
        raise HostSetupError(f"run me as root (sudo {invoked_as()})")
    release = os_release_id()
    if release not in profile.os_ids:
        raise HostSetupError(
            f"this tool targets {profile.label} hosts "
            f"(os-release says '{release or 'unknown'}')"
        )
    refuse_below_floor(profile, release)


def consent_line(states: dict[str, str]) -> str:
    """The summary's consent verdict: interactive egress consent
    needs every consent module loaded (#550)."""
    missing = [
        module for module in CONSENT_MODULES if states.get(module) != LOADED
    ]
    if not missing:
        return "interactive egress consent available"
    return (
        "interactive egress consent UNAVAILABLE on this kernel — "
        f"missing: {', '.join(missing)}"
    )


def summary(
    ambient: str,
    states: dict[str, str],
    kvm: str,
    service: str,
    uplink_name: str,
    login_path: str = "",
    client_link: str = "",
    profile: HostProfile = DEBIAN_PROFILE,
) -> None:
    say(profile.tool, "done —")
    print(f"  wrapper:    {WRAPPER} (setpriv, {CAPS}+ep)")
    print(f"               dev-daemon use: DEV_CAPS={WRAPPER} msks-dev")
    print(f"  forwarding: net.ipv4.ip_forward=1 ({SYSCTL_FILE}, active now)")
    print(f"  modules:    {MODULES_FILE} ({','.join(MODULES)})")
    print(
        f"  tools:      ip={which('ip')} nft={which('nft')} "
        f"conntrack={which('conntrack')}"
    )
    print(f"  /dev/kvm:   {kvm}")
    print(f"  login PATH: {login_path}")
    print(f"  client:     {client_link}")
    print(f"  consent:    {consent_line(states)}")
    print(
        "  uplink:     point the daemon at this host's default-route "
        "interface (MSKSD_EGRESS_UPLINK, or DEV_UPLINK for "
        f"msks-dev): {uplink_name or 'none found'}"
    )
    print(f"  service:    {service}")
    if "started" in service:
        print(
            "               same-user clients connect token-free "
            "(#555): $XDG_RUNTIME_DIR/msks/msksd.sock"
        )


def run_setup(
    service: bool = True, profile: HostProfile = DEBIAN_PROFILE
) -> None:
    """Every phase in order; the first refusal stops the run. The
    profile's admin PATH (sudo can strip /usr/sbin, where setcap
    and friends live) holds for the run and is restored after it."""
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = profile.admin_path + ":" + saved
    try:
        check_root_and_platform(profile)
        missing = list(
            dict.fromkeys(
                missing_packages(profile) + missing_soname_packages(profile)
            )
        )
        if missing:
            install_packages(missing, profile)
        install_wrapper()
        ambient = wrapper_ambient_proof()
        say(
            profile.tool,
            f"wrapper {WRAPPER} carries {CAPS}+ep (CapAmb {ambient})",
        )
        land_sysctl()
        states = land_modules(profile)
        user, uid, gid, home = executing_user()
        the_uplink = uplink()
        kvm_note, _kvm_added = ensure_kvm_membership(user, profile)
        path_note = land_user_path(home, uid, gid, profile)
        service_note = (
            install_service(user, uid, gid, home, the_uplink, profile)
            if service
            else "skipped (--no-service)"
        )
        client_link = link_client()
        summary(
            ambient,
            states,
            kvm_note,
            service_note,
            the_uplink,
            login_path=path_note,
            client_link=client_link,
            profile=profile,
        )
        # The VMM lands last and after the summary: it is the one
        # phase that needs the open internet (a host behind an
        # egress policy that passes the Debian mirror but not
        # github.com still gets every other piece), and its
        # refusal names the manual install the Installation
        # chapter documents.
        vmm = install_cloud_hypervisor()
        if vmm is not None:
            say(
                profile.tool,
                f"cloud-hypervisor: landed {vmm} (upstream static release)",
            )
    finally:
        os.environ["PATH"] = saved


def uninstall_summary(
    service: str,
    forwarding: str,
    modules: str,
    wrapper: str,
    kvm: str,
    login_path: str,
    vmm: str,
    packages: list[str],
    client_link: str = "",
    profile: HostProfile = DEBIAN_PROFILE,
) -> None:
    """The uninstall summary: every piece the run removed, in the
    install summary's shape, plus the removal line — the
    dependency packages stay, and the operator removes the ones
    present himself when nothing else uses them."""
    say(profile.tool, "done —")
    print(f"  service:    {service}")
    print(f"  forwarding: {forwarding}")
    print(f"  modules:    {modules}")
    print(f"  wrapper:    {wrapper}")
    print(f"  /dev/kvm:   {kvm}")
    print(f"  login PATH: {login_path}")
    print(f"  client:     {client_link}")
    print(f"  VMM:        {vmm}")
    if not packages:
        print("  packages:   none of the msks dependency packages are present")
        return
    print(
        "  packages:   the msks dependency packages stay installed; "
        "remove the ones present yourself when nothing else uses them:"
    )
    print(f"               {' '.join([*profile.remove_command, *packages])}")


def run_uninstall(
    service: bool = True, profile: HostProfile = DEBIAN_PROFILE
) -> None:
    """The setup's mirror (#604), in teardown order: the service
    stops first — its wrapper and caps vanish next — then every
    host piece leaves, and the summary names the dependency
    packages for the operator's own package-manager call. The
    first refusal stops the run, the install run's rule."""
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = profile.admin_path + ":" + saved
    try:
        check_root_and_platform(profile)
        user, uid, gid, home = executing_user()
        service_note = (
            uninstall_service(user, uid, home)
            if service
            else "skipped (--no-service)"
        )
        forwarding = drop_sysctl(profile)
        modules = drop_modules_file()
        wrapper = drop_wrapper()
        kvm = remove_kvm_membership(user, profile)
        login_path = drop_user_path(home, uid, gid, profile)
        client_link = drop_client_link()
        vmm = drop_cloud_hypervisor()
        uninstall_summary(
            service_note,
            forwarding,
            modules,
            wrapper,
            kvm,
            login_path,
            vmm,
            present_packages(profile),
            client_link=client_link,
            profile=profile,
        )
    finally:
        os.environ["PATH"] = saved


def parse_args(
    argv: list[str] | None, profile: HostProfile = DEBIAN_PROFILE
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=profile.tool,
        description=profile.description,
    )
    parser.add_argument(
        "--no-service",
        action="store_true",
        help="land every host piece but the systemd unit (a "
        "container, a non-systemd init, a deployment that runs "
        "msksd its own way)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="undo the setup (#604): stop and remove the msksd "
        "service, remove the wrapper, the client links, the routing "
        "kernel files, "
        "the cloud-hypervisor binary, the kvm membership, and the "
        "profile block — the apt packages stay, and the summary "
        "names them",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None, profile: HostProfile = DEBIAN_PROFILE
) -> int:
    """The console entry: 0 on success, 1 on a named refusal."""
    args = parse_args(argv, profile)
    try:
        if args.uninstall:
            run_uninstall(service=not args.no_service, profile=profile)
        else:
            run_setup(service=not args.no_service, profile=profile)
    except HostSetupError as exc:
        print(f"{profile.tool}: {exc}", file=sys.stderr)
        return 1
    return 0
