"""The /api/v1 request models and their validation grammar."""

import re

from pydantic import BaseModel, ConfigDict, Field

from ...identity import LOGIN_NAME_RE


class TokenCreate(BaseModel):
    name: str = "api"


class SecretMint(BaseModel):
    """A mint request (#198, #339, #423): one placeholder row.

    Coverage (#339): an empty or absent ``workspaces`` mints the
    **daemon-wide** placeholder — one row, one sentinel, valid on
    every accepting workspace's tap; a non-empty list scopes the
    row to exactly those workspaces (each entry an id or name,
    #246). ``workspace_id`` is the pre-#339 single-workspace
    spelling and stays accepted; the two spellings cannot mix.
    The value itself is never sent: the daemon mints it (#423)
    and answers with it — beside the sentinel — in exactly one
    response, this request's. Extra fields are refused by name: a
    pre-#423 client sending ``secret`` gets a 422 naming the field
    instead of a mint whose one-time value that client never
    prints.
    """

    # The stale-client catch (#423): pydantic's default silently
    # drops unknown fields, which would let a pre-#423 CLI mint
    # successfully while never showing the value it answered.
    model_config = ConfigDict(extra="forbid")

    workspaces: list[str] = Field(default=None, min_length=1, max_length=32)
    workspace_id: str | None = None
    name: str = Field(min_length=1, max_length=128)
    dests: list[str] = Field(min_length=1, max_length=32)
    ttl_s: int | None = Field(default=None, ge=1)


class SecretCoverageSet(BaseModel):
    """A coverage flip (#339): ``all`` accepts daemon-wide
    placeholders, ``scoped`` exempts the workspace from them."""

    secret_coverage: str


class SecretRenew(BaseModel):
    """A renew request: the new lifetime in seconds from now."""

    ttl_s: int = Field(ge=1)


#: One destination allowlist entry: an exact hostname or a
#: label-anchored suffix (``.example.com`` matches every host under
#: example.com and never ``notexample.com``) — the forms the #194
#: spike's matcher binds.
DEST_PATTERN = re.compile(
    r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)
WORKSPACE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
#: The #41 payload cap, in characters (pydantic max_length): far more
#: script or cloud-config than any first boot needs, while keeping the
#: seed disk's staging small.
USER_DATA_MAX = 65536


class WorkspaceResize(BaseModel):
    """A resize request (#184, #277): the new disk sizes or
    topology, any side optional.

    The bounds match create — a resize is the create-time sizing
    revisited, so the same floors and ceilings hold.
    """

    root_mib: int | None = Field(default=None, ge=256, le=65536)
    home_mib: int | None = Field(default=None, ge=64, le=65536)
    cpus: int | None = Field(default=None, ge=1, le=64)
    mem_mib: int | None = Field(default=None, ge=64, le=1 << 15)


class EgressDecide(BaseModel):
    """A decider's verdict on a held request (#69)."""

    decision: str  # "allow" | "deny"
    duration: str = "tilrestart"  # once | 5m | 15m | tilrestart | forever


class EgressPolicySet(BaseModel):
    """A mode switch (#280): the new posture, and — when given —
    the allowlist that replaces the row's (a None keeps it)."""

    mode: str  # allow | static | interactive
    allow_list: list[str] | None = Field(default=None, max_length=256)
    # The empty-static confirmation: the switch runs past the
    # nothing-effectively-allowed refusal when the operator gave
    # it (the TUI's confirm dialog, the CLI's --offline).
    confirm_empty: bool = False


class ImageImport(BaseModel):
    """An import request: a host-side path to a container-image tar,
    or an ``https://`` URL the daemon downloads itself (#258).

    The daemon's filesystem must reach a path source (a store path
    via the share, or a state-dir path) — the API deliberately
    does not accept uploads yet. A URL source is fetched into the
    catalog's staging area under the import ceiling and deadline
    (``MSKSD_IMAGE_IMPORT_MAX_MIB`` / ``MSKSD_IMAGE_IMPORT_TIMEOUT_S``),
    verified against system TLS roots, and imported from the
    downloaded copy.

    An optional ``name``/``version`` override (#340) registers the
    archive under an operator-chosen pair — either key alone, the
    other from the archive's own manifest, whose pair stays
    recorded as the row's origin.
    """

    source: str
    name: str | None = None
    version: str | None = None


class ImageDefault(BaseModel):
    """A default-designation request (#270): the catalog reference
    whose image a bare workspace create boots.

    Every form the daemon resolves for a create's ``image`` field
    works here — hash, ``name:version``, bare name (newest),
    ``name@hash`` — and a miss is a named 404.
    """

    ref: str


class ImageRename(BaseModel):
    """A rename request (#340): the registered pair a cataloged
    image moves to.

    Either key alone keeps the other at its registered value; the
    composed pair must pass the override checks (non-empty, no
    ``:``/``@``) and must not collide with another row's
    ``name:version``. The archive's own pair stays recorded as the
    row's origin, and the hash — the row's identity — never moves.
    """

    name: str | None = None
    version: str | None = None


class WorkspaceCreate(BaseModel):
    # The workspace's name (#246): the operator-chosen label the CLI
    # addresses the workspace by — the same DNS-label charset the
    # id carried before the split (it still lands in display
    # surfaces, never in artifact paths). Optional: a nameless
    # workspace is addressable by its minted id only.
    name: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=WORKSPACE_ID_PATTERN
    )
    # The pre-#246 spelling of the same field: a client one version
    # behind keeps creating (the daemon treats its ``id`` as the
    # name and mints the id either way). Note the same client's
    # ``msks ssh`` on such a workspace must address it by the minted
    # id: its client-minted identity file lands under the id, and
    # the old client looks it up under whatever reference it typed.
    id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=WORKSPACE_ID_PATTERN
    )
    # Either a catalog reference (image: "name:version", "name", or
    # hash — the default image when omitted) or explicit boot
    # artifacts (kernel/rootfs paths; the pre-catalog shape the
    # tests and dev flows still use).
    image: str | None = None
    kernel: str | None = None
    initrd: str | None = None
    rootfs: str | None = None
    cmdline: str | None = None
    cpus: int = Field(default=2, ge=1, le=64)
    mem_mib: int = Field(default=8192, ge=64, le=1 << 15)
    # Persistent-artifact sizes (#14), fixed at create; unset takes
    # the MSKSD_ROOT_MIB / MSKSD_HOME_MIB defaults.
    root_mib: int | None = Field(default=None, ge=256, le=65536)
    home_mib: int | None = Field(default=None, ge=64, le=65536)
    # Egress networking (#52): boots with a virtio-net NIC onto a
    # per-VM host tap — the default. "egress": false opts
    # into the no-NIC posture.
    egress: bool = True
    # The consent mode and static allowlist (#69), fixed at create.
    # ``egress_mode`` picks the enforcement posture: ``allow``
    # (default — new flows pass, off-list names are recorded),
    # ``static`` (the allowlist only; off-list names never resolve),
    # ``interactive`` (each new flow's first packet holds until a
    # decider allows or denies it). The allowlist is specs:
    # ``host``/``host:port`` names (a leading ``.`` includes
    # subdomains, ``*.`` matches subdomains only) gate at the
    # daemon's resolver; ``cidr[:port]`` address specs accept in the
    # per-VM chain.
    egress_mode: str | None = None
    egress_allowlist: list[str] | None = Field(default=None, max_length=256)
    # The daemon-wide placeholder posture (#339): ``all`` (the
    # default — daemon-wide placeholder coverage arms this
    # workspace and its sentinel swaps on its tap) or ``scoped``
    # (only placeholders minted directly at it arm it; a
    # daemon-wide sentinel used from it reads as an off-allowlist
    # sighting). Set at create and changeable later through
    # /secret-coverage.
    secret_coverage: str | None = None
    # First-boot provisioning (#41): a shell script (leading "#!") or
    # cloud-config YAML — cloud-init runs both — delivered on the
    # workspace's read-only cidata seed disk, composed beside the
    # minted identity's seeding script when one was minted (#111).
    # Create-time and immutable: a workspace keeps its payload until
    # it is deleted and recreated.
    user_data: str | None = Field(default=None, max_length=USER_DATA_MAX)
    # The no-escrow identity mode (#121): a public key line the
    # client minted, or one the operator already owns (#132) — any
    # well-formed key type. Present → the daemon stores and seeds the
    # public half only — no private half ever reaches it. Absent →
    # the daemon mints both halves itself (#111).
    ssh_pubkey: str | None = Field(default=None, max_length=16384)
    # The workspace's login user (#248): recorded on the row, seeded
    # into the guest at first boot (the account and its authorized_keys
    # when the image does not ship it), and served back as the
    # client's default login. Create-time and immutable, like the
    # specs. Absent → the workspace keeps the image's own login user
    # (the pre-#248 posture); the client fills its invoking user's
    # name before the request ever leaves.
    user: str | None = Field(default=None, pattern=LOGIN_NAME_RE.pattern)
