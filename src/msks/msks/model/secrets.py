"""Placeholder and audit ORM: the interceptor's DB half (#198).

A placeholder row binds one sentinel to one **coverage set** — the
whole daemon (a daemon-wide row, #339) or an explicit list of
workspaces (a scoped row) — plus one destination allowlist and one
backend reference into the secret store. The sentinel is stored
**plaintext by design**: the proxy matches it on the wire, and
without the daemon it swaps nothing — its only power is naming the
row that carried it.

The real secret never touches this database. It lives in the
secret store behind ``backend_ref`` (a SecretSpec declaration name)
and in the daemon's memory cache; a restart re-fetches by ref.

Audit rows are the operator's record of placeholder lifecycle:
mint, revoke, and expiry carry the placeholder's identity,
coverage, destinations, and timestamp — everything except the
secret and the sentinel, which have no business in an audit trail.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

#: The mint/revoke/expiry kinds an audit row can carry.
AUDIT_KINDS = ("mint", "revoke", "expiry")

#: The workspace settings a coverage flip accepts (#339): ``all``
#: takes daemon-wide placeholders, ``scoped`` exempts the workspace
#: from them (only placeholders minted directly at it arm it).
SECRET_COVERAGES = ("all", "scoped")


def coverage_label(workspaces: list[str]) -> str:
    """The operator-facing name of a coverage set (#339):
    ``*`` for the daemon-wide row, a comma-joined id list scoped.
    Display-only — rows and refs key on the JSON form."""
    return ",".join(sorted(set(workspaces))) if workspaces else "*"


class Placeholder(Base):
    """One minted placeholder: sentinel ↔ coverage binding (#198,
    #339).

    ``workspaces`` is the row's coverage as a JSON array of
    workspace ids: an empty array is the daemon-wide row (valid on
    every workspace whose ``secret_coverage`` accepts daemon-wide
    placeholders), a non-empty array is scoped to exactly those
    ids. The mint normalizes the list (sorted, de-duplicated), so
    the (name, workspaces) unique constraint pins one row per
    label per coverage set.
    """

    __tablename__ = "placeholders"
    __table_args__ = (
        UniqueConstraint("name", "workspaces", name="uq_placeholders_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The coverage set as a JSON array; [] = daemon-wide (#339).
    workspaces: Mapped[str] = mapped_column(Text)
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
    """One placeholder lifecycle event (#198).

    ``workspaces`` records the placeholder's coverage at the event
    (the same JSON-array shape the placeholder row carries), so an
    audit reader can tell a daemon-wide mint from a scoped one.
    """

    __tablename__ = "secret_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    # The placeholder's coverage as a JSON array; [] = daemon-wide
    # (#339). The decider replay's per-workspace scan matches rows
    # whose coverage includes the workspace — daemon-wide rows
    # replay onto every workspace's screen.
    workspaces: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String)
    dests: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
