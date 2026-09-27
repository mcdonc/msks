"""The VM spec and status types shared by every driver backend
(#1), plus the failure type the driver boundary and the egress
data plane raise (#401 — both surfaces fail a workspace boot with
one operator-shaped error, so the class lives in the vocabulary
below both and neither imports the other)."""

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
    is derived from the workspace id. ``name`` is the operator-
    chosen creation label: it owns no path or row key — the seed
    turns it into the guest's hostname (#370).
    """

    workspace_id: str
    kernel: Path
    rootfs: Path
    cmdline: str = "console=hvc0 root=/dev/vda rw"
    cpus: int = 2
    mem_mib: int = 8192
    initrd: Path | None = None
    # Persistent-artifact sizes (#14): the root overlay's virtual
    # size and the home volume's size, both fixed at create.
    root_mib: int = 10240
    home_mib: int = 20480
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
    # The daemon-wide placeholder posture (#339): ``all`` (the
    # default) takes daemon-wide placeholder coverage — one
    # sentinel minted for the whole daemon arms this workspace and
    # swaps on its tap; ``scoped`` takes only placeholders minted
    # directly at it. Recorded on the workspace row and changeable
    # there; the microvm seam itself never reads it.
    secret_coverage: str = "all"
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
    # The creation name (#246): the seed's ``local-hostname``
    # (#370) — the minted id stands in for a nameless workspace.
    # None is a spec built outside the row path (conformance
    # checkers, direct launches); the seed falls back to the id.
    name: str | None = None


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


class MicrovmError(Exception):
    """A workspace-surface failure (klangk PodmanError analogue).

    Raised by the driver boundary (a CH API transport failure, a
    lifecycle refusal) and by the egress data plane (a tap, chain,
    queue, or service that could not arm or operate). ``status``
    carries the transport status when one exists (a CH API HTTP
    status), ``None`` otherwise.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
