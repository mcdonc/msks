"""The VM spec and status types shared by every driver backend
(#1). The failure class the surfaces below share lives one leaf
over, in :mod:`msks.spec.failures` (#416), so a module that only
raises ``MicrovmError`` pulls none of this vocabulary."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .mounts import MountSpec


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
    cmdline: str = "console=ttyS0 root=/dev/vda rw"
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
    # disk labeled ``cidata`` — composed beside the identity's
    # seeding script when one is staged (#111). Create-time only;
    # None boots the workspace without an operator payload (the
    # identity alone still builds a seed).
    user_data: str | None = None
    # The identity's public half, one authorized_keys line (#111,
    # #486 — the operator's own key, supplied at create): composed
    # into the seed's user-data beside any operator payload. None
    # is a workspace without a seeded key (a deliberate keyless
    # create, or a pre-#111 row).
    ssh_pubkey: str | None = None
    # The workspace's login user (#248): recorded at create and
    # seeded into the guest at first boot (the account, its home,
    # authorized_keys, and the workspace-user sudo grant) when it
    # names an account the image does not ship. None is a workspace
    # created before per-workspace users (#248) or an API create
    # that names none — the guest carries the key for root and its
    # own msks account either way.
    login_user: str | None = None
    # The host's timezone captured at create (#633), the zone
    # name the guest's clock follows (the seed's first-boot block
    # sets it). None is a UTC-or-unreadable host: nothing rides,
    # and the guest keeps the image's own default. Immutable like
    # the rest of the create-time facts — a host timezone change
    # later does not drift a row already minted.
    timezone: str | None = None
    # The workspace's host mounts (#620, part #622): each names a
    # configured share or — the #667 host form (#672) — an
    # operator-typed host path through the policy gates, plus the
    # guest path it lands at and its mode. The start path derives
    # the attachable subset (shares gone from the config, denied
    # modes, rw conflicts, and host paths a reload ungated skip
    # with events — #621) and the seam attaches exactly what it
    # is handed: one virtiofsd per mount, the tag the guest
    # mounts by. Empty is the mountless workspace.
    mounts: tuple[MountSpec, ...] = ()
    # The workspace's LLM proxy credential (#259): minted at create,
    # stored on the row, and seeded into the guest (the token file
    # plus the profile.d exports that point OpenAI-shaped clients
    # at the daemon's proxy on this workspace's tap). None is a
    # workspace created before the proxy existed.
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
