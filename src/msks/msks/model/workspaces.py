"""Workspace ORM: the backend-neutral workspace record (#8).

Rows describe *what* a workspace is (its VM spec) and the lifecycle
state the daemon last observed. They never name pods, nodes, sockets,
or volumes — backend specifics live behind the microvm seam only.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
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
    """One workspace: identity + VM spec + observed lifecycle state."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kernel: Mapped[str] = mapped_column(String)
    initrd: Mapped[str | None] = mapped_column(String, nullable=True)
    rootfs: Mapped[str] = mapped_column(String)
    cmdline: Mapped[str] = mapped_column(Text)
    cpus: Mapped[int] = mapped_column(Integer)
    mem_mib: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
