"""The VM spec and status types shared by every driver backend (#1)."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True)
class VmSpec:
    """The backend-neutral description of one workspace VM.

    Kernel/initrd/rootfs are host paths today (local backend) and are
    passed through to the runner pod verbatim tomorrow (k8s backend),
    so the spec carries no placement knowledge.
    """

    workspace_id: str
    kernel: Path
    rootfs: Path
    cmdline: str = "console=hvc0 root=/dev/vda rw"
    cpus: int = 2
    mem_mib: int = 1024
    initrd: Path | None = None


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
