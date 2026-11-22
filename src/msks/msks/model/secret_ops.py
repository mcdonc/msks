"""The secret-store half of the Model (#898's split of
:mod:`msks.model.model`): the placeholder rows, their secret
blobs, and the audit trail — every method speaks the Model's own
attributes, composed in as a mixin the way the net manager's
halves compose.
"""

import json
from datetime import UTC

from sqlalchemy import delete, or_, select, update

from .db import sessionmaker_for, utcnow
from .keys import classify_blobs, coverage_key
from .secrets import AUDIT_KINDS, Placeholder, SecretAudit, SecretBlob
from .serialize import audit_dict, placeholder_dict
from .workspaces import Workspace, daemon_uid, sees_all_rows


class SecretOpsMixin:
    """The placeholder, blob, and audit operations; every method
    speaks the composing Model's own attributes."""

    # --- placeholders (#198) ------------------------------------------

    async def create_placeholder(
        self,
        workspaces: list[str],
        name: str,
        sentinel: str,
        dests: list[str],
        backend_ref: str,
        expires_at=None,
        owner_uid: int | None = None,
    ) -> dict:
        """Insert a placeholder row (#339: *workspaces* is the
        coverage set — ``[]`` mints the owner-wide row; #1083:
        *owner_uid* is the minting owner — None records the
        daemon's own uid, the TCP surface's mapping); raises
        IntegrityError on an (owner, coverage, name) or
        backend-ref collision."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = Placeholder(
                owner_uid=daemon_uid() if owner_uid is None else owner_uid,
                workspaces=coverage_key(workspaces),
                name=name,
                sentinel=sentinel,
                dests=json.dumps(dests),
                backend_ref=backend_ref,
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()
            return placeholder_dict(row)

    async def list_placeholders(self, viewer: int | None = None) -> list[dict]:
        """All placeholder rows, insertion order. ``viewer`` is the
        owner filter (#1083): a uid the filter does not bypass is
        served its own mints alone (the ``secret ls`` read);
        ``None`` (the daemon's own reads — the interceptor's
        resolution, the watcher's sweep) sees all."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            stmt = select(Placeholder).order_by(Placeholder.id)
            if not sees_all_rows(viewer):
                stmt = stmt.where(Placeholder.owner_uid == viewer)
            rows = await session.scalars(stmt)
            return [placeholder_dict(row) for row in rows]

    async def get_placeholder(self, placeholder_id: int) -> dict | None:
        """One placeholder row as a dict, None when absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Placeholder, placeholder_id)
            return None if row is None else placeholder_dict(row)

    async def placeholder_for(
        self, workspaces: list[str], name: str, owner_uid: int | None = None
    ) -> dict | None:
        """The placeholder with this exact owner, coverage set, and
        label, None when absent — the mint's collision read (#339:
        one row per label per coverage set, so the same label can
        live on the owner-wide row and on a scoped row beside it;
        #1083: the namespace is per owner — two owners may each
        hold the same label on the same coverage shape). None for
        *owner_uid* reads the daemon's own uid (the probe row's
        reads)."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.scalar(
                select(Placeholder).where(
                    Placeholder.owner_uid
                    == (daemon_uid() if owner_uid is None else owner_uid),
                    Placeholder.workspaces == coverage_key(workspaces),
                    Placeholder.name == name,
                )
            )
            return None if row is None else placeholder_dict(row)

    async def placeholder_by_ref(self, ref: str) -> dict | None:
        """The placeholder owning a backend ref, None when none does.

        Distinct (workspace, name) pairs can sanitize to the same
        ref (``foo``/``FOO`` on one workspace; ``a-b``/``a``+``b_c``
        across two) — a mint answers 409 here instead of corrupting
        the shared store entry.
        """
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.scalar(
                select(Placeholder).where(Placeholder.backend_ref == ref)
            )
            return None if row is None else placeholder_dict(row)

    async def rename_placeholder_ref(
        self, placeholder_id: int, new_ref: str
    ) -> bool:
        """Point one placeholder row at a renamed backend ref (#335).

        False when the row vanished mid-pass. A ref collision raises
        IntegrityError — impossible from a consistent legacy database
        (the rename is injective and (workspace, name) is unique), so
        it reaches the caller's per-row guard rather than being
        silently dropped.
        """
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Placeholder, placeholder_id)
            if row is None:
                return False
            row.backend_ref = new_ref
            await session.commit()
            return True

    async def renew_placeholder(self, placeholder_id: int, expires_at) -> bool:
        """Set a new expiry deadline; False when absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Placeholder)
                .where(Placeholder.id == placeholder_id)
                .values(expires_at=expires_at)
            )
            await session.commit()
            return result.rowcount > 0

    async def delete_placeholder(self, placeholder_id: int) -> bool:
        """Remove a placeholder row and its ciphertext blobs, in
        one transaction; False when absent (#514: revoke and the
        expiry sweep keep their row semantics and additionally
        drop the row's blobs — a mint rolled back after arming
        leaves no orphan ciphertext behind either)."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Placeholder, placeholder_id)
            if row is None:
                return False
            await session.execute(
                delete(SecretBlob).where(
                    SecretBlob.placeholder_id == placeholder_id
                )
            )
            await session.delete(row)
            await session.commit()
            return True

    async def placeholder_valid(self, sentinel: str) -> bool:
        """The per-request predicate (#199's swap gate): a row exists
        for the sentinel and its expiry is in the future (or unset).
        The stored deadline is naive UTC on the sqlite round-trip —
        normalized here before the comparison.
        """
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.scalar(
                select(Placeholder).where(Placeholder.sentinel == sentinel)
            )
            if row is None:
                return False
            if row.expires_at is None:
                return True
            # Stored deadlines are naive UTC (the sqlite dialect
            # strips tzinfo at bind); replace() unconditionally
            # normalizes without a branch.
            return row.expires_at.replace(tzinfo=UTC) > utcnow()

    # --- secret blobs (#513) ----------------------------------------

    async def write_secret_blobs(
        self, placeholder_id: int, blobs: dict[str, tuple[str, bytes]]
    ) -> None:
        """Replace one placeholder's blob set atomically: every
        recipient's ``(key_hash, ciphertext)`` pair lands in one
        transaction, the previous set — if any — goes with it.
        Replace-all is the lazy re-encryption rule's shape (#514:
        an unlock refreshes the whole set to the current
        recipients); a crash mid-write leaves either set whole."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            await session.execute(
                delete(SecretBlob).where(
                    SecretBlob.placeholder_id == placeholder_id
                )
            )
            for recipient, (key_hash, ciphertext) in sorted(blobs.items()):
                session.add(
                    SecretBlob(
                        placeholder_id=placeholder_id,
                        recipient=recipient,
                        key_hash=key_hash,
                        ciphertext=ciphertext,
                    )
                )
            await session.commit()

    async def secret_blobs(self, placeholder_id: int) -> list[dict]:
        """One placeholder's blob rows — recipient order — as
        dicts: recipient, key_hash, ciphertext, created_at."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(
                select(SecretBlob)
                .where(SecretBlob.placeholder_id == placeholder_id)
                .order_by(SecretBlob.recipient)
            )
            return [
                {
                    "recipient": row.recipient,
                    "key_hash": row.key_hash,
                    "ciphertext": row.ciphertext,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    async def drop_secret_blob(
        self, placeholder_id: int, recipient: str
    ) -> bool:
        """Remove one recipient's blob; False when absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                delete(SecretBlob).where(
                    SecretBlob.placeholder_id == placeholder_id,
                    SecretBlob.recipient == recipient,
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def secret_blob_staleness(self, placeholder_id: int) -> dict | None:
        """Which of a placeholder's recipients need a refresh
        (#513's staleness rule): ``{"missing": ids, "stale": ids}``,
        or None for an absent placeholder.

        The expected set is the row's coverage — a scoped row's
        workspaces, or every accepting workspace the row's owner
        holds (``all`` coverage, #339, #1083) for an owner-wide
        row — narrowed to the workspaces that carry a public key:
        a keyless workspace can hold no recipient, and #514's mint
        names that refusal at write time. A covered workspace
        whose current key has no blob is missing; one whose blob
        records a different key hash is stale (the key rotated).
        Rows alone decide — the rule survives a crash mid-write
        because each blob carries the hash it was minted against."""
        row = await self.get_placeholder(placeholder_id)
        if row is None:
            return None
        expected = await self.secret_recipient_keys(
            row["workspaces"], row["owner_uid"]
        )
        blobs = {
            blob["recipient"]: blob["key_hash"]
            for blob in await self.secret_blobs(placeholder_id)
        }
        return classify_blobs(expected, blobs)

    async def secret_recipient_keys(
        self, coverage: list[str], owner_uid: int | None = None
    ) -> dict[str, str]:
        """The workspaces a placeholder's blobs belong to, as an
        ``{id: ssh public key line}`` map: the explicit coverage
        for a scoped row, every key-carrying workspace the row's
        owner holds with ``all`` coverage for an owner-wide one
        (#339, #1083). None for *owner_uid* reads the daemon's own
        uid — the seeded probe row's shape. A covered workspace
        without a key is absent from the map — the mint (#514)
        refuses a keyless recipient by name, so the staleness rule
        never meets one."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            if coverage:
                rows = await session.scalars(
                    select(Workspace).where(Workspace.id.in_(coverage))
                )
            else:
                rows = await session.scalars(
                    select(Workspace).where(
                        Workspace.secret_coverage == "all",
                        Workspace.owner_uid
                        == (daemon_uid() if owner_uid is None else owner_uid),
                    )
                )
            return {row.id: row.ssh_pubkey for row in rows if row.ssh_pubkey}

    # --- secret audit (#198) -----------------------------------------

    async def record_audit(self, kind: str, row: dict) -> int:
        """Append one lifecycle event for a placeholder row; the
        new row's id — the identity the live publish and the
        decider replay share, so a frame that already landed is
        not sent or counted twice (#305). #1083: the event records
        the row's owner beside its coverage."""
        if kind not in AUDIT_KINDS:
            raise ValueError(f"unknown audit kind: {kind!r}")
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            entry = SecretAudit(
                kind=kind,
                owner_uid=row["owner_uid"],
                workspaces=coverage_key(row["workspaces"]),
                name=row["name"],
                dests=(
                    json.dumps(row["dests"])
                    if isinstance(row["dests"], list)
                    else row["dests"]
                ),
            )
            session.add(entry)
            await session.flush()
            # The flush populates the generated id into the instance
            # before commit; reading it into a local keeps the
            # return correct whatever the session factory's
            # expire_on_commit setting (#305 review).
            row_id = entry.id
            await session.commit()
            return row_id

    async def list_audit(
        self, limit: int = 100, viewer: int | None = None
    ) -> list[dict]:
        """The newest audit events first (operator view). ``viewer``
        is the owner filter (#1083): a uid the filter does not
        bypass reads its own rows' events alone; ``None`` (the
        daemon's own reads) sees all."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            stmt = select(SecretAudit).order_by(SecretAudit.id.desc())
            if not sees_all_rows(viewer):
                stmt = stmt.where(SecretAudit.owner_uid == viewer)
            rows = await session.scalars(stmt.limit(limit))
            return [audit_dict(row) for row in rows]

    async def list_workspace_audit(
        self, workspace_id: str, limit: int = 100
    ) -> list[dict]:
        """The audit events whose coverage includes one workspace
        AND whose owner owns it, oldest first (#305, #339, #1083):
        the decider registration replays them so the connection
        opens on the recorded lifecycle — an owner-wide row's
        events land on its own owner's workspaces alone, a scoped
        row's on its members'. The newest ``limit`` rows arrive in
        recording order — replay sends oldest first, so the
        client log lands newest last."""
        workspace = await self.get_workspace(workspace_id)
        if workspace is None:
            return []
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(
                select(SecretAudit)
                .where(
                    SecretAudit.owner_uid == workspace["owner_uid"],
                    or_(
                        SecretAudit.workspaces == "[]",
                        SecretAudit.workspaces.like(f'%"{workspace_id}"%'),
                    ),
                )
                .order_by(SecretAudit.id.desc())
                .limit(limit)
            )
            return [audit_dict(row) for row in reversed(rows.all())]
