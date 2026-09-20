"""The ``egress_consent`` ORM and its CRUD (#69), ported from
klangk's ``model/egress_consent.py``.

One row per consented destination: a pending request a held SYN
created, the verdict a decider (or policy) gave it, or the expiry
of a hold nobody answered. The lifecycle is klangk's:

``pending → allowed | denied | expired | revoked``

- ``allowed``/``denied`` — a decider's verdict (``decided_by`` set,
  with a duration) or a policy record (``decided_by`` NULL: static
  denials and allow-mode records).
- ``expired`` — a hold that timed out; distinct from a deny so the
  audit trail separates an unattended timeout from a refusal.
- ``revoked`` — an in-effect verdict undone by a decider.

Durations (how long enforcement honors a verdict): ``once`` (this
connection), ``5m``, ``15m``, ``tilrestart`` (until the workspace
VM stops — the flow rules die with the per-VM table), ``forever``
(the workspace's lifetime — replayed at every attach).
"""

import time
import uuid

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from .audit_hmac import compute_egress_consent_hmac
from .db import Base, sessionmaker_for

# --- lifecycle -------------------------------------------------------------

DECISION_PENDING = "pending"
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
DECISION_EXPIRED = "expired"
DECISION_REVOKED = "revoked"
DECISIONS = (
    DECISION_PENDING,
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_EXPIRED,
    DECISION_REVOKED,
)

# --- durations (#69's five) --------------------------------------------------

DURATION_ONCE = "once"
DURATION_5M = "5m"
DURATION_15M = "15m"
DURATION_TILRESTART = "tilrestart"
DURATION_FOREVER = "forever"
DURATIONS = (
    DURATION_ONCE,
    DURATION_5M,
    DURATION_15M,
    DURATION_TILRESTART,
    DURATION_FOREVER,
)
DURATION_DEFAULT = DURATION_TILRESTART

#: Timed durations in seconds; ``once``/``tilrestart``/``forever``
#: are not time-bounded (they are governed by connection, table
#: lifetime, and the row itself).
DURATION_SECONDS = {DURATION_5M: 300, DURATION_15M: 900}


class EgressConsent(Base):
    """One consented destination's audit + enforcement row."""

    __tablename__ = "egress_consent"
    __table_args__ = (
        # Dedup: at most one pending hold per (workspace, host, port)
        # — a SYN retransmit or a racing decider cannot pile up
        # duplicate prompts.
        sa.Index(
            "uq_egress_consent_pending",
            "workspace_id",
            "dest_host",
            "dest_port",
            unique=True,
            sqlite_where=sa.text("decision = 'pending'"),
        ),
        # Dedup: at most one policy record per destination per
        # decision — a flooding workspace cannot spam denial rows.
        sa.Index(
            "uq_egress_consent_static_denied",
            "workspace_id",
            "dest_host",
            "dest_port",
            unique=True,
            sqlite_where=sa.text("decision = 'denied' AND decided_by IS NULL"),
        ),
        sa.Index(
            "uq_egress_consent_static_allowed",
            "workspace_id",
            "dest_host",
            "dest_port",
            unique=True,
            sqlite_where=sa.text(
                "decision = 'allowed' AND decided_by IS NULL"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(sa.String, index=True)
    dest_host: Mapped[str] = mapped_column(sa.String)
    # 0 = portless (a non-TCP/UDP flow); never NULL, so the dedup
    # indexes see equal ports as equal.
    dest_port: Mapped[int] = mapped_column(sa.Integer)
    decision: Mapped[str] = mapped_column(sa.String)
    duration: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    requested_at: Mapped[float] = mapped_column(sa.Float)
    decided_at: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    revoked_at: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    hmac: Mapped[str | None] = mapped_column(sa.String, nullable=True)


def row_dict(row: EgressConsent) -> dict:
    """The API-facing dict for one row (the live and replayed frames
    share this shape, so the two cannot drift)."""
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "dest_host": row.dest_host,
        "dest_port": row.dest_port,
        "decision": row.decision,
        "duration": row.duration,
        "requested_at": row.requested_at,
        "decided_at": row.decided_at,
        "decided_by": row.decided_by,
        "revoked_at": row.revoked_at,
        "revoked_by": row.revoked_by,
        "hmac": row.hmac,
    }


def public_row(row: dict) -> dict:
    """A row dict without the verification-internal ``hmac`` tag —
    the shape that crosses to deciders."""
    return {k: v for k, v in row.items() if k != "hmac"}


def duration_in_effect(
    duration: str | None, decided_at: float | None, now: float
) -> bool:
    """Whether a verdict is still in effect at ``now``.

    ``tilrestart``/``forever`` are in effect until their own
    lifecycle event (detach clears the first; revoke or workspace
    delete clears both). A NULL duration is not in effect — every
    verdict sets one, so NULL only means a future shape we cannot
    bound, and the safe answer is no."""
    if decided_at is None:
        return False
    if duration in (DURATION_TILRESTART, DURATION_FOREVER):
        return True
    if duration == DURATION_ONCE:
        return False
    secs = DURATION_SECONDS.get(duration)
    if secs is None:
        return False
    return decided_at + secs > now


class EgressConsentModel:
    """CRUD for the consent table; ``app.state.model.egress_consent``."""

    def __init__(self, app) -> None:
        self.app = app

    def maker(self):
        """The session factory for the live engine."""
        return sessionmaker_for(self.app.state.model.engine())

    # --- creation -----------------------------------------------------------

    async def create_request(
        self, workspace_id: str, dest_host: str, dest_port: int
    ) -> dict | None:
        """Insert a pending hold, or None when one already exists
        for this destination (the dedup index answers atomically).

        The new row is re-read and HMAC-stamped inside the same
        session, so the returned dict carries the full column set —
        the same shape ``list_requests`` rows have."""
        request_id = str(uuid.uuid4())
        async with self.maker()() as session:
            stmt = (
                sqlite_insert(EgressConsent)
                .values(
                    id=request_id,
                    workspace_id=workspace_id,
                    dest_host=dest_host,
                    dest_port=dest_port,
                    decision=DECISION_PENDING,
                    requested_at=time.time(),
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "workspace_id",
                        "dest_host",
                        "dest_port",
                    ],
                    index_where=sa.text("decision = 'pending'"),
                )
            )
            await session.execute(stmt)
            row = await pending_row(
                session, workspace_id, dest_host, dest_port
            )
            if row is None or row.id != request_id:
                return None
            await stamp_hmac(session, self.app.state.settings, row)
            await session.commit()
            return row_dict(row)

    async def record_policy(
        self,
        decision: str,
        workspace_id: str,
        dest_host: str,
        dest_port: int,
    ) -> dict | None:
        """Record a policy row (no human): a static denial or an
        allow-mode record, decided at once — no pending state, no
        timeout. At most one per destination per decision (the
        static dedup index), so a flooding workspace cannot spam
        rows."""
        request_id = str(uuid.uuid4())
        now = time.time()
        async with self.maker()() as session:
            stmt = (
                sqlite_insert(EgressConsent)
                .values(
                    id=request_id,
                    workspace_id=workspace_id,
                    dest_host=dest_host,
                    dest_port=dest_port,
                    decision=decision,
                    requested_at=now,
                    decided_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "workspace_id",
                        "dest_host",
                        "dest_port",
                    ],
                    index_where=sa.text(
                        f"decision = '{decision}' AND decided_by IS NULL"
                    ),
                )
            )
            await session.execute(stmt)
            row = await session.scalar(
                select(EgressConsent).where(
                    EgressConsent.workspace_id == workspace_id,
                    EgressConsent.dest_host == dest_host,
                    EgressConsent.dest_port == dest_port,
                    EgressConsent.decision == decision,
                    EgressConsent.decided_by.is_(None),
                )
            )
            if row is None or row.id != request_id:
                return None
            await stamp_hmac(session, self.app.state.settings, row)
            await session.commit()
            return row_dict(row)

    # --- reads ---------------------------------------------------------------

    async def get_request(self, request_id: str) -> dict | None:
        async with self.maker()() as session:
            row = await session.get(EgressConsent, request_id)
            return None if row is None else row_dict(row)

    async def list_requests(
        self,
        workspace_id: str,
        decision: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        query = select(EgressConsent).where(
            EgressConsent.workspace_id == workspace_id
        )
        if decision is not None:
            query = query.where(EgressConsent.decision == decision)
        query = query.order_by(EgressConsent.requested_at.desc()).limit(limit)
        async with self.maker()() as session:
            rows = await session.scalars(query)
            return [row_dict(row) for row in rows]

    async def count_pending(self, workspace_id: str) -> int:
        """Pending holds for a workspace — gates the prompt-spam
        cap."""
        async with self.maker()() as session:
            count = await session.scalar(
                select(sa.func.count())
                .select_from(EgressConsent)
                .where(
                    EgressConsent.workspace_id == workspace_id,
                    EgressConsent.decision == DECISION_PENDING,
                )
            )
            return int(count or 0)

    async def active_verdict_for(
        self,
        workspace_id: str,
        dest_host: str,
        dest_port: int,
    ) -> dict | None:
        """The newest in-effect verdict for one exact destination,
        or None. Newest-first with elapsed verdicts skipped, so an
        expired newer allow cannot mask an older in-effect deny
        (klangk's pause-path semantics, reused by the resolver
        gate's forever check)."""
        async with self.maker()() as session:
            rows = await session.scalars(
                select(EgressConsent)
                .where(
                    EgressConsent.workspace_id == workspace_id,
                    EgressConsent.dest_host == dest_host,
                    EgressConsent.dest_port == dest_port,
                    EgressConsent.decision.in_(
                        [DECISION_ALLOWED, DECISION_DENIED]
                    ),
                    EgressConsent.decided_by.is_not(None),
                )
                .order_by(EgressConsent.decided_at.desc())
            )
            now = time.time()
            for row in rows:
                if duration_in_effect(row.duration, row.decided_at, now):
                    return row_dict(row)
        return None

    async def forever_verdict_for(
        self, workspace_id: str, dest_host: str
    ) -> dict | None:
        """The newest in-effect ``forever`` verdict for a host (any
        port), or None — the resolver gate's durable-allow/deny
        check. Only human verdicts: policy records are the
        allowlist's complement, not name rules."""
        async with self.maker()() as session:
            row = await session.scalar(
                select(EgressConsent)
                .where(
                    EgressConsent.workspace_id == workspace_id,
                    EgressConsent.dest_host == dest_host,
                    EgressConsent.decision.in_(
                        [DECISION_ALLOWED, DECISION_DENIED]
                    ),
                    EgressConsent.decided_by.is_not(None),
                    EgressConsent.duration == DURATION_FOREVER,
                )
                .order_by(EgressConsent.decided_at.desc())
                .limit(1)
            )
            return None if row is None else row_dict(row)

    async def list_active(self, workspace_id: str) -> list[dict]:
        """In-effect verdicts for a workspace — the rules view.

        Only human verdicts (``decided_by`` set): policy records are
        the complement of the allowlist, not actionable rules.
        ``once`` verdicts are consumed by their connection;
        ``expired``/``revoked`` rows are never in effect."""
        async with self.maker()() as session:
            rows = await session.scalars(
                select(EgressConsent)
                .where(
                    EgressConsent.workspace_id == workspace_id,
                    EgressConsent.decision.in_(
                        [DECISION_ALLOWED, DECISION_DENIED]
                    ),
                    EgressConsent.decided_by.is_not(None),
                )
                .order_by(EgressConsent.decided_at.desc())
            )
            now = time.time()
            return [
                row_dict(row)
                for row in rows
                if duration_in_effect(row.duration, row.decided_at, now)
            ]

    # --- lifecycle moves ---------------------------------------------------

    async def decide(
        self,
        request_id: str,
        decision: str,
        decided_by: str,
        duration: str = DURATION_DEFAULT,
    ) -> dict | None:
        """Record a decider's verdict on a pending hold; None when
        the row is absent or no longer pending."""
        if decision not in (DECISION_ALLOWED, DECISION_DENIED):
            raise ValueError(f"invalid decision: {decision!r}")
        if duration not in DURATIONS:
            raise ValueError(f"invalid duration: {duration!r}")
        now = time.time()
        async with self.maker()() as session:
            result = await session.execute(
                update(EgressConsent)
                .where(
                    EgressConsent.id == request_id,
                    EgressConsent.decision == DECISION_PENDING,
                )
                .values(
                    decision=decision,
                    duration=duration,
                    decided_at=now,
                    decided_by=decided_by,
                )
            )
            if result.rowcount == 0:
                return None
            row = await session.get(EgressConsent, request_id)
            await stamp_hmac(session, self.app.state.settings, row)
            await session.commit()
            return row_dict(row)

    async def revoke(self, request_id: str, revoked_by: str) -> dict | None:
        """Mark an in-effect verdict revoked; None when the row is
        not one (pending, already revoked, or a policy record)."""
        now = time.time()
        async with self.maker()() as session:
            result = await session.execute(
                update(EgressConsent)
                .where(
                    EgressConsent.id == request_id,
                    EgressConsent.decision.in_(
                        [DECISION_ALLOWED, DECISION_DENIED]
                    ),
                )
                .values(
                    decision=DECISION_REVOKED,
                    revoked_at=now,
                    revoked_by=revoked_by,
                )
            )
            if result.rowcount == 0:
                return None
            row = await session.get(EgressConsent, request_id)
            await stamp_hmac(session, self.app.state.settings, row)
            await session.commit()
            return row_dict(row)

    async def expire_pending(self, request_id: str) -> bool:
        """Auto-expire one hold (timeout); True when it moved. The
        duration is ``once`` — a timeout is a non-persistent deny
        for this connection, not a standing refusal."""
        async with self.maker()() as session:
            result = await session.execute(
                update(EgressConsent)
                .where(
                    EgressConsent.id == request_id,
                    EgressConsent.decision == DECISION_PENDING,
                )
                .values(
                    decision=DECISION_EXPIRED,
                    duration=DURATION_ONCE,
                    decided_at=time.time(),
                )
            )
            moved = result.rowcount > 0
            if moved:
                row = await session.get(EgressConsent, request_id)
                await stamp_hmac(session, self.app.state.settings, row)
                await session.commit()
            return moved

    async def expire_all_pending(self) -> int:
        """Expire every pending row (startup reaping): the previous
        run's in-memory holds are gone, so a row still pending is an
        orphan no decider can resolve. Returns the count reaped."""
        async with self.maker()() as session:
            rows = list(
                await session.scalars(
                    select(EgressConsent).where(
                        EgressConsent.decision == DECISION_PENDING
                    )
                )
            )
            now = time.time()
            for row in rows:
                row.decision = DECISION_EXPIRED
                row.decided_at = now
            for row in rows:
                await stamp_hmac(session, self.app.state.settings, row)
            await session.commit()
            return len(rows)

    async def clear_tilrestart(self, workspace_id: str) -> int:
        """Delete decided ``tilrestart`` verdicts (workspace stop).

        The flow rules died with the per-VM table; without this the
        rules view would keep listing verdicts nothing enforces.
        ``forever`` rows stay (replayed at the next attach); timed
        and pending rows keep their own clocks."""
        async with self.maker()() as session:
            rows = list(
                await session.scalars(
                    select(EgressConsent).where(
                        EgressConsent.workspace_id == workspace_id,
                        EgressConsent.duration == DURATION_TILRESTART,
                        EgressConsent.decision.in_(
                            [DECISION_ALLOWED, DECISION_DENIED]
                        ),
                    )
                )
            )
            for row in rows:
                await session.delete(row)
            await session.commit()
            return len(rows)

    async def forever_rows(self, workspace_id: str) -> list[dict]:
        """In-effect ``forever`` verdicts — the attach-time replay
        that pins a workspace's durable consents into the fresh
        chain and resolver gate."""
        return [
            row
            for row in await self.list_active(workspace_id)
            if row["duration"] == DURATION_FOREVER
        ]

    async def delete_for_workspace(self, workspace_id: str) -> int:
        """Drop every consent row for a workspace (workspace
        delete)."""
        async with self.maker()() as session:
            rows = list(
                await session.scalars(
                    select(EgressConsent).where(
                        EgressConsent.workspace_id == workspace_id
                    )
                )
            )
            for row in rows:
                await session.delete(row)
            await session.commit()
            return len(rows)

    # --- retention ---------------------------------------------------------

    def prune_eligible(self, row: dict, now: float) -> bool:
        """Whether a row may be pruned. Rows still in effect are
        enforcement state, not history — deleting a forever or
        un-elapsed verdict would silently stop it working. They
        leave via their own lifecycle (revoke, expiry, workspace
        delete) instead."""
        decision = row["decision"]
        if decision not in (DECISION_ALLOWED, DECISION_DENIED):
            # pending (stale by age), expired, revoked — never in
            # effect.
            return True
        if row["decided_by"] is None:
            # A policy record: audit-only, re-recorded on the next
            # hit by its dedup index.
            return True
        return not duration_in_effect(row["duration"], row["decided_at"], now)

    async def prune(self, now: float | None = None) -> int:
        """Bound the table: delete rows past retention or over the
        per-workspace row cap. Returns rows deleted. Both passes
        skip rows still in effect (see :meth:`prune_eligible`)."""
        retention_days, row_cap = self.bounds()
        if retention_days <= 0 and row_cap <= 0:
            return 0
        when = now if now is not None else time.time()
        return await self.run_prune_passes(retention_days, row_cap, when)

    async def run_prune_passes(
        self, retention_days: int, row_cap: int, when: float
    ) -> int:
        """The two prune passes in order (retention, then cap)."""
        deleted = 0
        if retention_days > 0:
            deleted += await self.prune_retention(
                when - retention_days * 86400.0, when
            )
        if row_cap > 0:
            deleted += await self.prune_row_cap(row_cap, when)
        return deleted

    def bounds(self) -> tuple[int, int]:
        """The retention-days and row-cap settings pair."""
        settings = self.app.state.settings.net
        return settings.consent_retention_days, settings.consent_row_cap

    async def prune_retention(self, cutoff: float, now: float) -> int:
        """Delete rows whose terminal timestamp predates the
        cutoff."""
        async with self.maker()() as session:
            rows = list(
                await session.scalars(
                    select(EgressConsent).where(
                        sa.func.coalesce(
                            EgressConsent.revoked_at,
                            EgressConsent.decided_at,
                            EgressConsent.requested_at,
                        )
                        < cutoff
                    )
                )
            )
            doomed = [
                row for row in rows if self.prune_eligible(row_dict(row), now)
            ]
            for row in doomed:
                await session.delete(row)
            await session.commit()
            return len(doomed)

    async def prune_row_cap(self, row_cap: int, now: float) -> int:
        """Per workspace over the cap, delete the oldest eligible
        rows down to it — the bound against decided-request floods
        outpacing age-based pruning. A live pending row is never
        deleted here."""
        async with self.maker()() as session:
            over = list(
                await session.execute(
                    select(
                        EgressConsent.workspace_id,
                        sa.func.count(),
                    )
                    .group_by(EgressConsent.workspace_id)
                    .having(sa.func.count() > row_cap)
                )
            )
            deleted = 0
            for workspace_id, count in over:
                # HAVING guarantees count > row_cap, so the excess
                # is always at least one row.
                excess = int(count) - row_cap
                deleted += await self.trim_workspace(
                    session, workspace_id, excess, now
                )
            await session.commit()
            return deleted

    async def trim_workspace(self, session, workspace_id, excess, now):
        """Delete the oldest eligible rows of one workspace down
        to its excess; returns how many went."""
        rows = list(
            await session.scalars(
                select(EgressConsent)
                .where(
                    EgressConsent.workspace_id == workspace_id,
                    EgressConsent.decision != DECISION_PENDING,
                )
                .order_by(
                    sa.func.coalesce(
                        EgressConsent.revoked_at,
                        EgressConsent.decided_at,
                        EgressConsent.requested_at,
                    )
                )
            )
        )
        deleted = 0
        for row in rows:
            if excess <= 0:
                break
            if not self.prune_eligible(row_dict(row), now):
                continue
            await session.delete(row)
            excess -= 1
            deleted += 1
        return deleted


async def pending_row(session, workspace_id, dest_host, dest_port):
    """The one pending row for a destination, inside the caller's
    session (the dedup-check read of the insert paths)."""
    return await session.scalar(
        select(EgressConsent).where(
            EgressConsent.workspace_id == workspace_id,
            EgressConsent.dest_host == dest_host,
            EgressConsent.dest_port == dest_port,
            EgressConsent.decision == DECISION_PENDING,
        )
    )


async def stamp_hmac(session, settings, row: EgressConsent) -> None:
    """Compute and persist the row's HMAC tag (a no-op when no key
    is configured — the row keeps its NULL tag)."""
    tag = compute_egress_consent_hmac(settings, row_dict(row))
    if tag is None:
        return
    row.hmac = tag
