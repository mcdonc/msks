"""The package and VMM phases.

Missing plumbing packages and NFQUEUE sonames install through
the distro manager; cloud-hypervisor lands from the upstream
static release when the archive carries none.
"""

from __future__ import annotations

import os
import shutil
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profile import HostProfile

from ...settings import resolve_virtiofsd
from .profile import DEBIAN_PROFILE, HostProfile
from .util import (
    HostSetupError,
    file_exists,
    remove_file,
    say,
    sh,
    silent_unlink,
    which,
)

# The VMM (#598): Debian's archive carries cloud-hypervisor in
# no suite, so the tool lands the documented upstream static
# release itself (docs/installation.md names the same source).
# The release carries one static build per architecture — the
# unsuffixed assets are x86-64, aarch64's carry -aarch64 — and
# each entry names the (VMM, ch-remote) pair that architecture
# ships; doctor's fix hint prints the same pair (#831).
CLOUD_HYPERVISOR_RELEASE = (
    "https://github.com/cloud-hypervisor/cloud-hypervisor/releases"
    "/latest/download"
)

CLOUD_HYPERVISOR_ASSETS = {
    "x86_64": ("cloud-hypervisor-static", "ch-remote-static"),
    "aarch64": (
        "cloud-hypervisor-static-aarch64",
        "ch-remote-static-aarch64",
    ),
}

CLOUD_HYPERVISOR_BIN = "/usr/local/bin/cloud-hypervisor"


def cloud_hypervisor_assets(machine: str) -> tuple[str, str]:
    """The release's (cloud-hypervisor, ch-remote) static asset
    names for one machine — a machine the release carries no
    build for falls back to the x86-64 pair, the unsuffixed
    assets."""
    return CLOUD_HYPERVISOR_ASSETS.get(
        machine, CLOUD_HYPERVISOR_ASSETS["x86_64"]
    )


def cloud_hypervisor_url() -> str:
    """The upstream static-release URL for this host's machine —
    kernel_package()'s uname() probe is the in-file precedent."""
    asset, _remote = cloud_hypervisor_assets(os.uname().machine)
    return f"{CLOUD_HYPERVISOR_RELEASE}/{asset}"


def tool_present(tool: str) -> bool:
    """Whether one needed tool answers on this host: PATH names
    it, or — the virtiofsd package's shape alone (#643) — one of
    the archive locations it installs the binary at off PATH
    carries it."""
    if which(tool) is not None:
        return True
    if tool != "virtiofsd":
        return False

    def executable(path: str) -> bool:
        return file_exists(path) and os.access(path, os.X_OK)

    return resolve_virtiofsd(tool, which, executable) != tool


def missing_packages(
    profile: HostProfile = DEBIAN_PROFILE,
) -> list[str]:
    """The packages whose tools are absent, in check order."""
    missing = []
    for tool, package in profile.tool_packages.items():
        if not tool_present(tool):
            missing.append(package)
    return missing


def missing_soname_packages(
    profile: HostProfile = DEBIAN_PROFILE,
) -> list[str]:
    """The NFQUEUE soname packages the linker cache does not show.

    A probe that cannot run counts as absent: the install is
    idempotent, and a host doctor cannot read stays honest when
    the packages land anyway.
    """
    rc, out, _err = sh(["ldconfig", "-p"])
    if rc != 0:
        return list(profile.soname_packages.values())
    return [
        p for soname, p in profile.soname_packages.items() if soname not in out
    ]


def install_packages(
    packages: list[str],
    profile: HostProfile = DEBIAN_PROFILE,
) -> None:
    """Install the missing packages through the profile's
    package manager — the refresh step apt-get wants and dnf
    runs as part of install, the noninteractive environment
    debconf needs, and the timeout a cold mirror fill takes.
    The error lines name the manager's own verbs."""
    say(profile.tool, f"installing {' '.join(packages)}")
    if profile.refresh_command is not None:
        rc, _out, err = sh(list(profile.refresh_command), timeout=120)
        if rc != 0:
            raise HostSetupError(
                f"{profile.refresh_command[0]} update failed: {err}"
            )
    env = (
        {**os.environ, **profile.install_env} if profile.install_env else None
    )
    rc, _out, err = sh(
        [*profile.install_command, *packages],
        timeout=profile.install_timeout,
        env=env,
    )
    if rc != 0:
        raise HostSetupError(
            f"{profile.package_manager} install failed: {err}"
        )


def fetch_upstream(url: str, dest: str) -> None:
    """Stream one upstream artifact to dest — the network seam
    the tests rig; no shell, no pipeline."""
    with (
        urllib.request.urlopen(url, timeout=60) as response,
        open(dest, "wb") as sink,
    ):
        shutil.copyfileobj(response, sink)


def install_cloud_hypervisor() -> str | None:
    """The VMM from the upstream static release (#598): Debian's
    archive carries it in no suite, so the tool lands the binary
    the Installation chapter documents, for this host's
    architecture. Returns the binary's path when it landed, None
    when the host already had one — a present binary stays
    untouched (an operator's own install, a pinned release,
    wins). The landed binary answers `--version` before the run
    calls it landed: a truncated download, an architecture
    mismatch, or a dead redirect is a refusal that removes what
    it downloaded, not a broken VMM the next run defends."""
    if which("cloud-hypervisor") is not None:
        return None
    url = cloud_hypervisor_url()
    partial = CLOUD_HYPERVISOR_BIN + ".part"
    try:
        fetch_upstream(url, partial)
    except Exception as exc:  # any network failure, named for the fix
        silent_unlink(partial)
        raise HostSetupError(
            f"cloud-hypervisor download failed ({exc}); install the "
            f"upstream static release from {url}"
        ) from exc
    os.chmod(partial, 0o755)
    os.replace(partial, CLOUD_HYPERVISOR_BIN)
    rc, out, err = sh([CLOUD_HYPERVISOR_BIN, "--version"])
    if rc != 0:
        silent_unlink(CLOUD_HYPERVISOR_BIN)
        raise HostSetupError(
            f"the downloaded cloud-hypervisor does not run ({err.strip()} "
            f"or {out.strip()}); install the upstream static release "
            f"for {os.uname().machine} from {url}"
        )
    return CLOUD_HYPERVISOR_BIN


def drop_cloud_hypervisor() -> str:
    """The VMM's mirror: the binary the tool lands at
    /usr/local/bin goes (#598's own documented location). The
    `.part` sibling a killed download can leave goes with it,
    best-effort the install's own refusal paths made it — residue
    cleanup never refuses the phase."""
    silent_unlink(CLOUD_HYPERVISOR_BIN + ".part")
    if not remove_file(CLOUD_HYPERVISOR_BIN):
        return f"{CLOUD_HYPERVISOR_BIN} already absent"
    return f"{CLOUD_HYPERVISOR_BIN} removed"


def present_packages(
    profile: HostProfile = DEBIAN_PROFILE,
) -> list[str]:
    """The dependency map's packages whose probes answer present
    on this host — the ones the uninstall summary names for the
    operator's own package-manager run. The probes are the
    install's own (which() and the linker cache), so the list on
    the way out reads the same evidence the list on the way in
    did."""
    missing_sonames = missing_soname_packages(profile)
    packages = [
        package
        for tool, package in profile.tool_packages.items()
        if which(tool) is not None
    ]
    packages += [
        package
        for soname, package in profile.soname_packages.items()
        if package not in missing_sonames
    ]
    return list(dict.fromkeys(packages))
