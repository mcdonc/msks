"""The VM spec and status types shared by every driver backend (#1)."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True)
class VmSpec:
    """The backend-neutral description of one workspace VM.

    Kernel/initrd/rootfs are host paths today (local backend), so
    the spec carries no placement knowledge. ``workspace_id`` is
    the workspace's immutable, daemon-minted instance id (#246);
    ``rootfs`` names the *base* image: the VM boots the
    per-workspace overlay backed by it (#14), and the overlay path
    is derived from the workspace id. The operator-chosen name
    never reaches this layer.
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
    # NIC onto a per-VM host tap — the default, so a
    # plain create is networked. ``egress: false`` opts back into the
    # no-NIC posture (the one every backend serves with zero net
    # machinery).
    egress: bool = True
    # The consent mode and static allowlist (#69), fixed at create:
    # the mode picks the chain shape and the resolver gate; the
    # specs are name specs (resolver) and address specs (chain).
    egress_mode: str = "allow"
    egress_allowlist: tuple[str, ...] = ()
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
    # The workspace's login user (#248): recorded at create and
    # seeded into the guest at first boot (the account, its home,
    # authorized_keys, and the workspace-user sudo grant) when it
    # names an account the image does not ship. None is a workspace
    # created before per-workspace users — its login user is the
    # image's own (LEGACY_LOGIN_USER).
    login_user: str | None = None
    # The workspace's LLM proxy credential (#259): minted at create,
    # stored on the row, and seeded into the guest (the token file
    # plus the profile.d exports that point OpenAI-shaped clients
    # at the daemon's proxy on this workspace's tap). None is a
    # workspace created before the proxy existed.
    llm_token: str | None = None


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
