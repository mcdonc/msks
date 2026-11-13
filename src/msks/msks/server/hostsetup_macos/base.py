"""The shared base: constants and the command helpers.

The VM's names, ports, floors, and URLs; the wheel-side paths
the in-VM bootstrap writes; and ``run``/``vm_shell`` — every
phase's way to run a command on the host or inside the VM —
beside :class:`MacSetupError`, the named refusal.
"""

from __future__ import annotations

import subprocess
import sys
import time

TOOL = "msks-setup-macos-host"

#: The Lima instance name. The course VM (#649) is ``msks``; the
#: daemon VM keeps its own name so the two never collide.

#: The Lima instance name. The course VM (#649) is ``msks``; the
#: daemon VM keeps its own name so the two never collide.
VM_NAME = "msksd"

#: The daemon's port inside the VM (its own default) and on the
#: Mac — the forward binds both loopbacks (the daemon binds the
#: guest's 127.0.0.1, Lima forwards the host's 127.0.0.1; the
#: course VM verified the shape live). The host port is a
#: ``--port`` choice: anything else on the Mac already answering
#: there — another VM's forward, a local daemon — makes the
#: verify pass name the collision and the operator picks
#: another. A run without ``--port`` adopts the forward the
#: existing VM carries (#699); a fresh creation renders this
#: default.

#: The daemon's port inside the VM (its own default) and on the
#: Mac — the forward binds both loopbacks (the daemon binds the
#: guest's 127.0.0.1, Lima forwards the host's 127.0.0.1; the
#: course VM verified the shape live). The host port is a
#: ``--port`` choice: anything else on the Mac already answering
#: there — another VM's forward, a local daemon — makes the
#: verify pass name the collision and the operator picks
#: another. A run without ``--port`` adopts the forward the
#: existing VM carries (#699); a fresh creation renders this
#: default.
VM_PORT = 8660

PORT = 8660

#: The template's Lima floor (#671) — the version whose mount
#: expansions (``~`` host-side, ``{{.User}}`` guest-side) were
#: verified. Older Limas render the mountPoint literally.

#: The template's Lima floor (#671) — the version whose mount
#: expansions (``~`` host-side, ``{{.User}}`` guest-side) were
#: verified. Older Limas render the mountPoint literally.
LIMA_FLOOR = (2, 2, 1)

#: The sizing floors (#721): below these the VM cannot hold the
#: daemon and a workspace (the state dir, the image store, and a
#: workspace's two sparse disks — 40 GiB at the create defaults,
#: filled on demand; the daemon and a workspace's guest under
#: 4 GiB of memory; two vCPUs).

#: The sizing floors (#721): below these the VM cannot hold the
#: daemon and a workspace (the state dir, the image store, and a
#: workspace's two sparse disks — 40 GiB at the create defaults,
#: filled on demand; the daemon and a workspace's guest under
#: 4 GiB of memory; two vCPUs).
SHAPE_FLOORS = {"cpus": 2, "memory": 4, "disk": 20}

#: Where the wheel installs inside the VM and the paths the unit
#: and the config read. The venv is uv-managed Python: the wheel
#: targets the project's Python floor (3.14) and Debian trixie
#: ships 3.13, so the bootstrap provisions the standalone 3.14
#: through uv rather than fighting the distro's interpreter.

#: Where the wheel installs inside the VM and the paths the unit
#: and the config read. The venv is uv-managed Python: the wheel
#: targets the project's Python floor (3.14) and Debian trixie
#: ships 3.13, so the bootstrap provisions the standalone 3.14
#: through uv rather than fighting the distro's interpreter.
VM_PREFIX = "/opt/msks"

#: The daemon's own tool set — the packages a Debian deployment
#: host installs (hostsetup's map): the egress plumbing (nft,
#: ip, conntrack), the volume trio, the seed's iso tooling, the
#: client workflows' pair, and the console's multiplexer. The
#: daemon execs them by name; a missing one is a workspace-start
#: refusal naming it.

#: The daemon's own tool set — the packages a Debian deployment
#: host installs (hostsetup's map): the egress plumbing (nft,
#: ip, conntrack), the volume trio, the seed's iso tooling, the
#: client workflows' pair, and the console's multiplexer. The
#: daemon execs them by name; a missing one is a workspace-start
#: refusal naming it.
VM_PACKAGES = (
    "python3-venv virtiofsd nftables iproute2 conntrack "
    "e2fsprogs qemu-utils genisoimage openssh-client rsync tmux"
)

#: The VMM the daemon boots workspaces with: upstream's static
#: aarch64 release (the tool is Apple-Silicon-only, one arch),
#: pinned by sha256 the way the repro suites pin theirs — Debian
#: carries cloud-hypervisor in no suite.

#: The VMM the daemon boots workspaces with: upstream's static
#: aarch64 release (the tool is Apple-Silicon-only, one arch),
#: pinned by sha256 the way the repro suites pin theirs — Debian
#: carries cloud-hypervisor in no suite.
CH_URL = (
    "https://github.com/cloud-hypervisor/cloud-hypervisor/"
    "releases/download/v54.0/cloud-hypervisor-static-aarch64"
)

CH_SHA256 = "e8a6d9786ea678e739f1a386682846e93742dcb9c7b6e3e0c62e3bda6cff0ef3"

#: The uv release the bootstrap installs from PyPI — pinned like
#: every fetched artifact; uv provisions the VM's Python 3.14
#: from its own standalone builds.

#: The uv release the bootstrap installs from PyPI — pinned like
#: every fetched artifact; uv provisions the VM's Python 3.14
#: from its own standalone builds.
UV_PIN = "0.9.7"

VM_CONF = "/etc/msksd.yaml"

VM_UNIT = "/etc/systemd/system/msksd.service"

VM_STATE = "/var/lib/msksd"

#: Where the CA lands on the Mac — beside the client config the
#: preset's CAFILE names.


class MacSetupError(Exception):
    """A named refusal: the message prints with the tool's name."""


def say(message: str) -> None:
    """One progress line on stderr, the hostsetup tools' shape."""
    print(f"{TOOL}: {message}", file=sys.stderr)


def run(command: list[str], *, capture: bool = False) -> str:
    """One subprocess; a nonzero exit is a named refusal carrying
    the command's tail. A binary the host does not carry raises
    FileNotFoundError unchanged — the caller decides what it
    means: lima_version_output reads it as the stock-Mac answer,
    delete_vm maps it to the install remedy."""
    done = subprocess.run(
        command,
        capture_output=capture,
        text=True,
    )
    if done.returncode != 0:
        tail = ""
        if capture and done.stderr:
            tail = ": " + done.stderr.strip().splitlines()[-1]
        raise MacSetupError(f"`{' '.join(command[:3])}...` failed{tail}")
    return done.stdout if capture else ""


def vm_shell(script: str) -> str:
    """Run one bash snippet inside the VM as root, stdin to
    stdout — the one in-VM seam every phase drives. A nonzero exit
    is a named refusal carrying the stderr tail."""
    done = subprocess.run(
        ["limactl", "shell", VM_NAME, "--", "sudo", "bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1] if done.stderr else ""
        raise MacSetupError(
            f"in-VM command failed{': ' + tail if tail else ''}"
        )
    return done.stdout


# --- phase 1: the host check -----------------------------------------


def fetch_vm_file(path: str, timeout_s: float = 20.0) -> str:
    """One file out of the VM, retried while the daemon's first
    startup may not have written it yet (the restart returns
    before the CA lands)."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = vm_shell(f"cat {path} 2>/dev/null || true")
        if last.strip():
            return last
        time.sleep(2.0)
    return last
