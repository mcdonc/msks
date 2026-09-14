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

    Three #14 columns record the workspace's persistent half:
    ``image_hash`` binds it to the catalog image its overlay backs
    (an image with live workspaces cannot be removed), ``host`` is
    the machine that owns the overlay and home volume (the placement
    a start must honor), and ``root_mib``/``home_mib`` are the
    artifacts' sizes, fixed at create.
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kernel: Mapped[str] = mapped_column(String)
    initrd: Mapped[str | None] = mapped_column(String, nullable=True)
    rootfs: Mapped[str] = mapped_column(String)
    cmdline: Mapped[str] = mapped_column(Text)
    cpus: Mapped[int] = mapped_column(Integer)
    mem_mib: Mapped[int] = mapped_column(Integer)
    image_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    host: Mapped[str | None] = mapped_column(String, nullable=True)
    root_mib: Mapped[int] = mapped_column(Integer, default=10240)
    home_mib: Mapped[int] = mapped_column(Integer, default=2048)
    # Egress networking (#52): boots the VM with a virtio-net NIC
    # onto a per-VM tap in the appliance — the default; ``egress:
    # false`` opts back into the no-NIC posture.
    egress: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String, default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
