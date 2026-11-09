"""Workspace proxy ORM: persisted TCP proxies (#807, #808).

A proxy row is one daemon-owned listener tied to a workspace. Two
directions exist:

- ``forward`` (the ssh ``-L`` shape): the daemon binds
  ``listen_host:listen_port`` on its own host and every accepted
  connection opens a guest TCP connection to ``target_port`` —
  the same dial the forward websocket rides (#109).
- ``remote`` (the ssh ``-R`` shape): the daemon binds
  ``listen_port`` on the workspace's tap address, and every
  connection a guest process makes to it is bridged to
  ``target_host:target_port`` on the daemon's side.

The row is the whole definition: the daemon reads it at every
workspace start and brings each listener up, so a proxy survives
stops, starts, and daemon restarts. Nothing here describes the
listener's live state — that is process-local to the proxy
manager (:mod:`msks.proxies`).
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

#: The two directions a proxy row can carry.
PROXY_DIRECTIONS = ("forward", "remote")


class WorkspaceProxy(Base):
    """One persisted proxy definition, owned by one workspace."""

    __tablename__ = "workspace_proxies"
    __table_args__ = (
        Index("ix_workspace_proxies_workspace_id", "workspace_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    listen_host: Mapped[str | None] = mapped_column(Text, nullable=True)
    listen_port: Mapped[int] = mapped_column(Integer, nullable=False)
    target_host: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_port: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
