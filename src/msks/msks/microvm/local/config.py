"""The cloud-hypervisor config builders: memory, net,
disks, and the state/status vocabulary mapping."""

from __future__ import annotations

from pathlib import Path

from ...persist import (
    home_volume_path,
    overlay_path,
    seed_path,
)
from ...spec.vm import VmSpec, VmStatus
from ..errors import MicrovmError

CH_STATE_TO_STATUS = {
    "Created": VmStatus.STARTING,
    "Running": VmStatus.RUNNING,
    "Paused": VmStatus.PAUSED,
    "Shutdown": VmStatus.STOPPED,
}

GUEST_DOWN_STATES = ("Created", "Shutdown")

POLL_INTERVAL_S = 0.05
# How long to wait before pressing the power button again: an early-boot
# press lands before the guest's logind listens and is silently dropped.
# The re-press delivers on x86_64 only while cloud-hypervisor's arm64
# button emulation stays one-way (#826).
POWER_REPRESS_S = 5.0


def vm_config(
    spec: VmSpec,
    disks: list[dict],
    console_socket: Path | None = None,
    net: dict | None = None,
    fs: list[dict] | None = None,
) -> dict:
    """The ``PUT /api/v1/vm.create`` body for one spec (v52 schema).

    Memory is bytes (``mem_mib`` is converted), the payload nests
    kernel/cmdline/initramfs, the serial file is a plain path string,
    and the disks arrive as their own entries (#14): the root overlay
    first, the home volume second — position makes the root device.

    ``console_socket`` puts the serial device in socket mode
    (#481): cloud-hypervisor LISTENS on that unix path, and the
    guest's console device — ttyS0 on x86_64, ttyAMA0 on
    aarch64 (#833) — carries the kernel console and the
    autologin root getty through it, bidirectionally. Without it
    the serial device stays off.

    ``net`` adds the virtio-net device (#52): ``tap`` names the
    per-VM interface the daemon already created and addressed (a
    plain dict ``{"tap": ..., "mac": ...}`` from the net manager's
    attachment; v52 takes a one-element sequence), and ``mac`` pins
    the workspace's deterministic MAC.

    ``fs`` adds the vhost-user-fs devices (#620, part #622): one
    per attached share, ``tag`` naming the virtiofs tag the guest
    mounts by — the daemons behind the sockets are already
    serving when the VMM opens them.
    """
    payload: dict = {
        "kernel": str(spec.kernel),
        "cmdline": spec.cmdline,
    }
    if spec.initrd is not None:
        payload["initramfs"] = str(spec.initrd)
    vm: dict = {
        "cpus": {"boot_vcpus": spec.cpus, "max_vcpus": spec.cpus},
        "memory": vm_memory(spec, fs),
        "payload": payload,
        "disks": disks,
        "serial": (
            {"mode": "Socket", "socket": str(console_socket)}
            if console_socket is not None
            else {"mode": "Off"}
        ),
        "console": {"mode": "Off"},
    }
    if net is not None:
        vm["net"] = [net]
    if fs:
        vm["fs"] = fs
    return vm


def vm_memory(spec: VmSpec, fs: list[dict] | None) -> dict:
    """The v52 memory block: the size, and shared mapping when a
    vhost-user device rides the VM (the fs devices map guest RAM
    shared with the virtiofs daemons; CH refuses the boot without
    it — "Using vhost-user requires using shared memory or huge
    pages" — and the v52 field is ``shared``: an unknown key like
    shared_memory is silently ignored, so the spelling is pinned
    live against vm.info). Set only when a device needs it:
    shared memory changes the memory backend's accounting shape."""
    memory: dict = {"size": spec.mem_mib * 1024 * 1024}
    if fs:
        memory["shared"] = True
    return memory


def vm_net(attachment) -> dict | None:
    """The v52 net device for an attachment, or None without egress."""
    if attachment is None:
        return None
    return {"tap": attachment.tap, "mac": attachment.mac}


def disk_entries(state_dir: Path, workspace_id: str) -> list[dict]:
    """The VM's persistent disks (#14): root overlay, home volume.

    Both are writable — the overlay absorbs root writes over the
    pristine base (copy-on-write protects it; ``readonly`` on the old
    single raw disk is obsolete) — and carry an explicit
    ``image_type``: v52's autodetection otherwise disables sector-0
    writes on untyped disks. The overlay additionally opts into
    ``backing_files``: v51 loads a qcow2 backing file only when the
    disk says so (landlock hardening, GHSA advisory follow-up).

    A workspace whose artifacts include the ``cidata`` seed adds a
    third disk: the seed, read-only raw. The seed is built for a
    payload (#41), a supplied key (#111, #486), or a tap that will
    serve an LLM listener or the interception CA (#259, #424) — a
    keyless create seeds too, so the attach follows the artifact
    itself: the caller has run ``ensure_artifacts`` first, and the
    seed's presence at its path is the whole of the decision (the
    boot-time heal rebuilds it the same way, so a healed boot
    re-attaches it the same way).
    """
    disks = [
        {
            "path": str(overlay_path(state_dir, workspace_id)),
            "readonly": False,
            "image_type": "Qcow2",
            "backing_files": True,
        },
        {
            "path": str(home_volume_path(state_dir, workspace_id)),
            "readonly": False,
            "image_type": "Raw",
        },
    ]
    if seed_path(state_dir, workspace_id).is_file():
        disks.append(
            {
                "path": str(seed_path(state_dir, workspace_id)),
                "readonly": True,
                "image_type": "Raw",
            }
        )
    return disks


#: The fs daemons' one grace, three roles: how long a terminated
#: daemon gets to exit before SIGKILL, the budget a freshly
#: spawned daemon gets to serve its socket, and the post-KILL
#: death-poll window.


def map_ch_state(state: str | None) -> VmStatus:
    """Translate a CH ``vm.info`` state string."""
    if state is None:
        return VmStatus.UNKNOWN
    return CH_STATE_TO_STATUS.get(state, VmStatus.UNKNOWN)


def console_retry_error(workspace_id: str, retry: Exception) -> MicrovmError:
    """The named error for a console retry whose deadline passed."""
    return MicrovmError(
        f"console unavailable for {workspace_id} (is the VM running?): {retry}"
    )


def check_id(workspace_id: str) -> None:
    """Reject ids that would escape the vms/ directory.

    The API already enforces the charset; this is the driver-side
    backstop so no future caller can turn ``../..`` into an rmtree of
    the state directory or plant artifacts at absolute paths.
    """
    unsafe = (
        workspace_id in ("", ".", "..")
        or "/" in workspace_id
        or "\\" in workspace_id
        or workspace_id != workspace_id.strip()
    )
    if unsafe:
        raise MicrovmError(f"unsafe workspace id: {workspace_id!r}")
