"""Placeholder and audit ORM: the interceptor's DB half (#198).

A placeholder row binds one sentinel to one workspace, one
destination allowlist, and one backend reference into the secret
store. The sentinel is stored **plaintext by design**: the proxy
matches it on the wire, and without the daemon it swaps nothing —
its only power is naming the workspace that carried it.

The real secret never touches this database. It lives in the
secret store behind ``backend_ref`` (a SecretSpec declaration name)
and in the daemon's memory cache; a restart re-fetches by ref.

Audit rows are the operator's record of placeholder lifecycle:
mint, revoke, and expiry carry the placeholder's identity,
destinations, and timestamp — everything except the secret and the
sentinel, which have no business in an audit trail.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

#: The mint/revoke/expiry kinds an audit row can carry.
AUDIT_KINDS = ("mint", "revoke", "expiry")


class Placeholder(Base):
    """One minted placeholder: sentinel ↔ workspace binding (#198)."""

    __tablename__ = "placeholders"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_placeholders_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    # The operator-facing label; also the tail of backend_ref.
    name: Mapped[str] = mapped_column(String)
    # Plaintext by design (see the module docstring).
    sentinel: Mapped[str] = mapped_column(String, unique=True, index=True)
    # JSON array of destination allowlist entries: exact hosts and
    # label-anchored suffixes (".example.com").
    dests: Mapped[str] = mapped_column(Text)
    # The SecretSpec declaration name the real secret is stored under.
    backend_ref: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # None means unbounded lifetime; the per-request predicate treats
    # a past expiry the same as a revoked row.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class SecretAudit(Base):
    """One placeholder lifecycle event (#198)."""

    __tablename__ = "secret_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    workspace_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    dests: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
