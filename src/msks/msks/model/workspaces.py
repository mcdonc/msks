"""Workspace ORM: the backend-neutral workspace record (#8).

Rows describe *what* a workspace is (its VM spec) and the lifecycle
state the daemon last observed. They never name pods, nodes, sockets,
or volumes — backend specifics live behind the microvm seam only.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

WORKSPACE_STATUSES = (
    "created",
    "starting",
    "running",
    "paused",
    "stopped",
    "absent",
    "unknown",
)


class Workspace(Base):
    """One workspace: identity + VM spec + observed lifecycle state.

    Identity is two fields (#246): ``id`` is the daemon-minted,
    immutable instance id (10 fresh random hex digits per create,
    never reused while the workspace lives — every artifact path,
    cache, and keyed surface derives from it), and ``name`` is the
    operator-chosen label the CLI addresses workspaces by. A row
    minted before #246 keeps its operator-chosen id as the id and a
    NULL name: its label is its id, so ref resolution addresses it
    unchanged and no path or cache moves. ``name`` is unique per
    daemon and may be NULL (a workspace created through the API
    without a label, addressable by id only).

    Three #14 columns record the workspace's persistent half:
    ``image_hash`` binds it to the catalog image its overlay backs
    (an image with live workspaces cannot be removed), ``host`` is
    the machine that owns the overlay and home volume (the placement
    a start must honor), and ``root_mib``/``home_mib`` are the
    artifacts' sizes, fixed at create.
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(
        String, nullable=True, unique=True
    )
    kernel: Mapped[str] = mapped_column(String)
    initrd: Mapped[str | None] = mapped_column(String, nullable=True)
    rootfs: Mapped[str] = mapped_column(String)
    cmdline: Mapped[str] = mapped_column(Text)
    cpus: Mapped[int] = mapped_column(Integer)
    mem_mib: Mapped[int] = mapped_column(Integer)
    image_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    host: Mapped[str | None] = mapped_column(String, nullable=True)
    root_mib: Mapped[int] = mapped_column(Integer, default=10240)
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
    # key was seeded, no key fetch serves it.
    ssh_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The private half a pre-#486 daemon minted and escrowed
    # (#111): NULL for every row created now (#486 — the daemon
    # never holds a private half), kept readable for the rows that
    # carry one so they stay reachable.
    ssh_privkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The workspace's login user (#248): create-time and immutable,
    # like the specs. The seed provisions the account at first boot
    # when the image does not ship it; the key endpoint serves it as
    # the client's default login. NULL on a pre-#248 row — the
    # workspace's login user is the image's own msks account.
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
    # The daemon-wide placeholder posture (#339): ``all`` (the
    # default — daemon-wide placeholders arm this workspace and
    # their sentinels swap on its tap) or ``scoped`` (the workspace
    # takes only placeholders minted directly at it; a daemon-wide
    # sentinel used from it reads as an off-allowlist sighting).
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
    status: Mapped[str] = mapped_column(String, default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
