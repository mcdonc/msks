"""Placeholder and audit ORM: the interceptor's DB half (#198).

A placeholder row binds one sentinel to one **coverage set** — the
whole daemon (a daemon-wide row, #339) or an explicit list of
workspaces (a scoped row) — plus one destination allowlist and one
backend reference into the secret store. The sentinel is stored
**plaintext by design**: the proxy matches it on the wire, and
without the daemon it swaps nothing — its only power is naming the
row that carried it.

The real secret never touches this database under the SecretSpec
store: it lives behind ``backend_ref`` and in the daemon's memory
cache. The #513 half begins the move to per-recipient ciphertext
(#504): ``SecretBlob`` rows hold the value encrypted to the
workspace identity's ssh public half, one row per recipient — the
daemon that stores them holds no key that decrypts them, and the
client (#515) does the unwrapping.

Audit rows are the operator's record of placeholder lifecycle:
mint, revoke, expiry, and unlock carry the placeholder's
identity, coverage, destinations, and timestamp — everything
except the secret and the sentinel, which have no business in an
audit trail.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

#: The lifecycle kinds an audit row can carry — mint, revoke,
#: expiry, and the unlock that re-encrypts a row's blobs (#514).
AUDIT_KINDS = ("mint", "revoke", "expiry", "unlock")

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


class SecretBlob(Base):
    """One placeholder value encrypted to one recipient (#513,
    #504).

    The ciphertext is a whole age file — the format the client
    (#515) decrypts with the workspace identity's private half,
    wrapped to the workspace's ssh public key line at mint.
    ``recipient`` is the workspace id that line belonged to, and
    ``key_hash`` records the line itself (SHA-256 hex): the
    staleness rule reads it back — a workspace whose key changed,
    or a coverage set that grew past its blobs, is derivable from
    rows alone, so the next unlock (#514) knows what to refresh
    without the daemon ever holding plaintext.
    """

    __tablename__ = "secret_blobs"
    __table_args__ = (
        UniqueConstraint(
            "placeholder_id", "recipient", name="uq_secret_blobs_placeholder"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    placeholder_id: Mapped[int] = mapped_column(Integer, index=True)
    recipient: Mapped[str] = mapped_column(String)
    key_hash: Mapped[str] = mapped_column(String)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
