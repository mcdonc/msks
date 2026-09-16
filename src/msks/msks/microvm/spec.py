"""The VM spec and status types shared by every driver backend (#1)."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True)
class VmSpec:
    """The backend-neutral description of one workspace VM.

    Kernel/initrd/rootfs are host paths today (local backend) and are
    passed through to the runner pod verbatim tomorrow (k8s backend),
    so the spec carries no placement knowledge. ``rootfs`` names the
    *base* image: the VM boots the per-workspace overlay backed by it
    (#14), and the overlay path is derived from the workspace id.
    """

    workspace_id: str
    kernel: Path
    rootfs: Path
    cmdline: str = "console=hvc0 root=/dev/vda rw"
    cpus: int = 2
    mem_mib: int = 1024
    initrd: Path | None = None
    # Persistent-artifact sizes (#14): the root overlay's virtual
    # size and the home volume's size, both fixed at create.
    root_mib: int = 10240
    home_mib: int = 2048
    # Egress networking (#52): the workspace boots with a virtio-net
    # NIC onto a per-VM tap inside the appliance — the default, so a
    # plain create is networked. ``egress: false`` opts back into the
    # no-NIC posture (the one every backend serves with zero net
    # machinery).
    egress: bool = True
    # First-boot provisioning payload (#41): a shell script (leading
    # ``#!``) or cloud-config YAML, delivered on a per-workspace seed
    # disk labeled ``cidata`` — composed beside the minted identity's
    # seeding script when one was minted (#111). Create-time only;
    # None boots the workspace without an operator payload (the
    # identity alone still builds a seed).
    user_data: str | None = None
    # The minted identity's public half, one authorized_keys line
    # (#111): composed into the seed's user-data beside any operator
    # payload. None is a workspace without a minted identity (a
    # pre-#111 row).
    ssh_pubkey: str | None = None


class VmStatus(StrEnum):
    """Lifecycle states across both drivers."""

    ABSENT = "absent"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VmInfo:
    """What ``info()`` reports for one workspace."""

    workspace_id: str
    status: VmStatus
    pid: int | None = None
