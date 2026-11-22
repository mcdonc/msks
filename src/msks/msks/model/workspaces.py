"""Workspace ORM: the backend-neutral workspace record (#8).

Rows describe *what* a workspace is (its VM spec) and the lifecycle
state the daemon last observed. They never name pods, nodes, sockets,
or volumes — backend specifics live behind the microvm seam only.
"""

import os
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

WORKSPACE_STATUSES = (
    "created",
    "starting",
    "running",
    "paused",
    "stopping",
    "stopped",
    "absent",
    "unknown",
)


def daemon_uid() -> int:
    """The uid the daemon process itself runs as (#1082) — the
    owner the migration backfills existing rows with and the uid
    the TCP surface maps every caller onto."""
    return os.geteuid()


def sees_all_rows(viewer) -> bool:
    """The owner filter's bypass rule (#1082): the daemon's own
    internal reads (``None``), root (uid 0), and the daemon's own
    uid see every row; every other uid sees the rows it owns
    alone. An explicit :class:`OwnerScope` never bypasses (#1084)
    — the scope's whole point is "resolve as this one owner's
    rows", and an owner the rule would bypass (root's own rows,
    the migrated daemon-uid rows) is exactly one an unscoped
    plural name cannot address."""
    if isinstance(viewer, OwnerScope):
        return False
    return viewer is None or viewer == 0 or viewer == daemon_uid()


class OwnerScope(int):
    """An explicitly scoped viewer (#1084): numerically its uid —
    every comparison and bind the owner filter already makes
    (``row.owner_uid == viewer``, the SQLAlchemy ``where``) reads
    it as the plain uid — but :func:`sees_all_rows` answers False
    for it, so the scope always filters. The scope dependency
    (:func:`msks.server.api.deps.scoped_caller`) is the only
    thing that builds one."""

    __slots__ = ()


class AmbiguousRef(ValueError):
    """A name a bypass viewer addressed that more than one owner
    holds (#1082): per-owner uniqueness makes the plural legal, so
    the resolution refuses to pick — the message names the owners
    and the by-id remedy. #1084 formalizes the error kind and the
    client's ``--user`` disambiguator."""

    def __init__(self, ref: str, owners: list[int]) -> None:
        #: The ref the caller addressed — the bare name two or
        #: more owners hold.
        self.ref = ref
        #: The holding uids, sorted — the payload the ambiguity
        #: surfaces answer with names account-side.
        self.owners = owners
        listed = ", ".join(str(uid) for uid in owners)
        super().__init__(
            f"workspace name {ref!r} is held by more than one owner "
            f"(uids: {listed}); address the workspace by its id"
        )


class Workspace(Base):
    """One workspace: identity + VM spec + observed lifecycle state.

    Identity is three fields: ``id`` is the daemon-minted,
    immutable instance id (10 fresh random hex digits per create,
    never reused while the workspace lives — every artifact path,
    cache, and keyed surface derives from it), ``name`` is the
    operator-chosen label the CLI addresses workspaces by, and
    ``owner_uid`` (#1082) is the peer credential that created the
    row. A row minted before #246 keeps its operator-chosen id as
    the id and a NULL name: its label is its id, so ref resolution
    addresses it unchanged and no path or cache moves. ``name`` is
    unique per owner and may be NULL (a workspace created through
    the API without a label, addressable by id only).

    Three #14 columns record the workspace's persistent half:
    ``image_hash`` binds it to the catalog image its overlay backs
    (an image with live workspaces cannot be removed), ``host`` is
    the machine that owns the overlay and home volume (the placement
    a start must honor), and ``root_mib``/``home_mib`` are the
    artifacts' sizes, fixed at create.
    """

    __tablename__ = "workspaces"
    # Names are unique per owner (#1082): the daemon-wide unique
    # constraint became this pair, so two owners may each hold a
    # ``build`` and a nameless create (NULL) never collides —
    # SQLite's unique index treats NULLs as distinct.
    __table_args__ = (
        UniqueConstraint("owner_uid", "name", name="uq_workspaces_owner_name"),
    )

    # The owning uid (#1082): the peer credential that created the
    # row. Root and the daemon's own uid bypass the owner filter;
    # every other uid sees and addresses its own rows alone.
    owner_uid: Mapped[int] = mapped_column(Integer)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    kernel: Mapped[str] = mapped_column(String)
    initrd: Mapped[str | None] = mapped_column(String, nullable=True)
    rootfs: Mapped[str] = mapped_column(String)
    cmdline: Mapped[str] = mapped_column(Text)
    cpus: Mapped[int] = mapped_column(Integer)
    mem_mib: Mapped[int] = mapped_column(Integer)
    image_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    host: Mapped[str | None] = mapped_column(String, nullable=True)
    root_mib: Mapped[int] = mapped_column(Integer, default=20480)
    home_mib: Mapped[int] = mapped_column(Integer, default=20480)
    # Egress networking (#52): boots the VM with a virtio-net NIC
    # onto a per-VM host tap. The default keeps the
    # no-NIC posture.
    egress: Mapped[bool] = mapped_column(Boolean, default=True)
    # First-boot provisioning payload (#41): the operator's
    # user_data, delivered on the workspace's cidata seed disk
    # beside the identity's seeding script when a key was supplied
    # (#111). Create-time and immutable — NULL boots with the
    # identity script alone (or without a seed, a keyless create
    # under #486).
    user_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The identity (#111, #486): the public half is one
    # authorized_keys line (algorithm name, key, and a provenance
    # comment). NULL on a keyless create or a pre-#111 row — no
    # key was seeded, no key fetch serves it. The private half
    # lives nowhere on the daemon (#497): the operator's own key
    # holds it.
    ssh_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The workspace's login user (#248): create-time and immutable,
    # like the specs. The seed provisions the account at first boot
    # when the image does not ship it; the key endpoint serves it as
    # the client's default login. NULL on a pre-#248 row (or an API
    # create that names none) — the key endpoint answers null and
    # the client dials root (#500).
    login_user: Mapped[str | None] = mapped_column(String, nullable=True)
    # The pool slice the workspace's /30 derives from (#70 review):
    # recorded at first attach so stop/start cycles and daemon
    # restarts keep the same address, even past digest collisions
    # between workspace ids.
    egress_slice: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The egress consent mode (#69): ``allow`` (the default — new
    # flows pass, off-list names are recorded), ``static`` (the
    # allowlist only; off-list names never resolve), or
    # ``interactive`` (each new flow's first packet holds for a
    # decider verdict). Create-time and immutable, like the specs.
    egress_mode: Mapped[str] = mapped_column(
        String, default="allow", server_default="allow"
    )
    # The owner-wide placeholder posture (#339, #1083): ``all``
    # (the default — the owner's owner-wide placeholders arm this
    # workspace and their sentinels swap on its tap) or ``scoped``
    # (the workspace takes only placeholders minted directly at
    # it; an owner-wide sentinel used from it reads as an
    # off-allowlist sighting).
    # Set at create and changeable later; a change re-evaluates the
    # workspace's armed state.
    secret_coverage: Mapped[str] = mapped_column(
        String, default="all", server_default="all"
    )
    # The static allowlist as a JSON array of specs (#69):
    # ``host``/``host:port``/``.host``/``*.host`` names gate at the
    # daemon's resolver; CIDR and IP-literal specs accept in the
    # per-VM chain. NULL is an empty allowlist.
    egress_allowlist: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The workspace's host mounts (#620, part #621): a JSON array
    # of ``{share, guest, mode}`` mappings — each names a
    # configured share, the guest path it lands at, and ``ro`` or
    # ``rw`` (default ``rw``). Set at create or later; applied at
    # the next start (resize semantics). NULL is the mountless
    # row — a pre-#620 row and a workspace created without mounts
    # read identically.
    mounts: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The mounts the workspace's last start attached (#756): the
    # healthy subset of the configured set at the moment the
    # launch took them — a snapshot, written only at a successful
    # start. NULL is a workspace that never started (no record),
    # and the pair with ``mounts`` is the row's pending story: the
    # two agree until an edit or a skip splits them, and the next
    # start writes the record that reconciles them.
    attached_mounts: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The sizes the workspace's last start booted (#898): JSON
    # ``{"cpus", "mem_mib", "root_mib"}`` — the half a resize
    # defers to a boot (root growth and the vcpu/memory
    # topology; home bytes move at once and ride nothing). NULL
    # is a workspace that never started, and the pair with the
    # row's own sizes is the resize pending story: the two agree
    # until an edit splits them, and the next start writes the
    # record that reconciles them — the attached_mounts shape
    # (#756), carried to sizes.
    booted_sizes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The host's timezone captured at create (#633): the zone
    # name the seed's first-boot block sets the guest's clock to.
    # NULL is a UTC-or-unreadable host — nothing rides and the
    # guest keeps the image's default. Create-time and immutable
    # like user_data: a host timezone change later does not drift
    # a row already minted.
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    # The workspace's disk set epoch (#697): 0 from create, bumped
    # each time the daemon recreates the root overlay under a live
    # row — a boot's heal of a lost overlay, a factory reset. The
    # overlay is where the guest's ssh host keys live (#110: the
    # image ships none, the first boot writes them there), so a
    # recreated overlay means regenerated host keys: the identity
    # endpoint serves the epoch and the client rotates its
    # host-key cache on a change, re-pinning instead of refusing.
    disk_epoch: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    # The seed's epoch (#803): a monotonic counter, one bump per
    # seed bake or renewal — 0 from create. A new instance-id
    # makes the guest re-provision, and the stock cloud-init ssh
    # module on Debian and Fedora regenerates the ssh host keys
    # there, so the identity endpoint serves the counter and the
    # client rotates its host-key cache on a change, re-pinning
    # instead of refusing. The counter (not the bake's content
    # hash) is what rotates: a mount set leaving and returning
    # bakes the same content twice, and the second bake still
    # re-keys the guest. NixOS omits the ssh module from its
    # cloud_init_modules; the rotation is correct and inert there
    # (accept-new re-pins the unchanged key).
    seed_epoch: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String, default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
