"""The Model state object: every database operation msksd performs."""

import hashlib
import json
import secrets
from datetime import UTC
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from ..microvm.spec import VmSpec
from .db import Base, engine_for, sessionmaker_for, tighten_db_mode, utcnow
from .egress_consent import EgressConsentModel
from .secrets import AUDIT_KINDS, Placeholder, SecretAudit
from .tokens import Token
from .workspaces import WORKSPACE_STATUSES, Workspace

TOKEN_ENTROPY_BYTES = 32

# Inside the package, so the wheel ships it: a pip-installed msksd
# can run its migrations (the appliance build inherits this).
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def alembic_config(db_path: Path) -> AlembicConfig:
    """The programmatic Alembic config for one database path."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def hash_token(plaintext: str) -> str:
    """The stored form of a bearer token (sha256 hex)."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def new_token() -> str:
    """A fresh bearer token plaintext (shown once, stored hashed)."""
    return secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


class Model:
    """Owns the engine and every query; caches only ``self.app``."""

    def __init__(self, app) -> None:
        self.app = app
        self._engine: AsyncEngine | None = None
        # The consent submodel (#69): same ownership rule (caches
        # only app), reached as ``app.state.model.egress_consent``.
        self.egress_consent = EgressConsentModel(app)

    def _db_path(self) -> Path:
        return self.app.state.settings.server.db_path

    def engine(self) -> AsyncEngine:
        """The lazily-created engine for the live database path."""
        if self._engine is None:
            self._engine = engine_for(self._db_path())
        return self._engine

    async def create_all(self) -> None:
        """Create tables for a fresh database (tests; Alembic owns changes)."""
        async with self.engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    def migrate(self) -> None:
        """Run Alembic migrations to head for the live database path."""
        db_path = self._db_path()
        # Same 0600 rule as engine_for: this path often creates the
        # file first (lifespan migrates before anything opens the
        # engine), and the rows carry user_data payloads (#41).
        tighten_db_mode(db_path)
        config = alembic_config(db_path)
        try:
            command.upgrade(config, "head")
        except OperationalError as exc:
            self.heal_torn_migration(config, exc)

    def heal_torn_migration(self, config, exc: OperationalError) -> None:
        """Recover a migration torn by a hard power cut.

        The cut can land between a migration's committed DDL and its
        alembic_version stamp (separate transactions): the next boot
        fails with "table already exists" (or, for an add_column
        re-run, "duplicate column name") and, unfixed, wedges the
        appliance forever. Only a torn *head* is stamped past: the
        version row must be absent (the observed #10 shape: DDL done,
        stamp lost) or sit at head's parent, meaning the torn step is
        the last pending one. Any earlier gap — or a legitimate
        future failure worded the same — needs an operator, not a
        guess, and is re-raised.
        """
        torn = "already exists" in str(exc) or "duplicate column name" in str(
            exc
        )
        if not torn or not self.torn_head_is_next(config):
            raise
        command.stamp(config, "head")
        command.upgrade(config, "head")

    def torn_head_is_next(self, config) -> bool:
        """Whether head is the next step for the stamped version."""
        script = ScriptDirectory.from_config(config)
        revisions = [rev.revision for rev in script.walk_revisions()]
        engine = create_engine(f"sqlite:///{self._db_path()}")
        try:
            with engine.connect() as connection:
                current = MigrationContext.configure(
                    connection
                ).get_current_revision()
        finally:
            engine.dispose()
        return current is None or current == revisions[1]

    async def close(self) -> None:
        """Dispose the engine (daemon shutdown; tests swap database
        paths). Idempotent; the engine recreates lazily on next use."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    # --- tokens ------------------------------------------------------------

    async def create_token(
        self, name: str, plaintext: str | None = None
    ) -> tuple[int, str]:
        """Insert a token; returns ``(id, plaintext)``.

        The plaintext is shown once."""
        token = plaintext if plaintext is not None else new_token()
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = Token(name=name, token_hash=hash_token(token))
            session.add(row)
            await session.commit()
            return row.id, token

    async def list_tokens(self) -> list[dict]:
        """All tokens, insertion order, without hashes (operator view)."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(select(Token).order_by(Token.id))
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "created_at": row.created_at.isoformat(),
                    "revoked": row.revoked,
                }
                for row in rows
            ]

    async def token_valid(self, plaintext: str) -> bool:
        """Whether a bearer token authenticates (present, unrevoked)."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.scalar(
                select(Token).where(Token.token_hash == hash_token(plaintext))
            )
            return row is not None and not row.revoked

    async def revoke_token(self, token_id: int) -> bool:
        """Revoke by id; False when the id is unknown."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Token).where(Token.id == token_id).values(revoked=True)
            )
            await session.commit()
            return result.rowcount > 0

    async def bootstrap_token(self) -> str | None:
        """The configured bootstrap token, inserted when absent.

        A token that exists in any state — valid or revoked — is left
        alone: re-inserting a revoked row would trip the unique index
        on ``token_hash`` and crash startup.
        """
        plaintext = self.app.state.settings.server.bootstrap_token
        if plaintext is None:
            return None
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.scalar(
                select(Token).where(Token.token_hash == hash_token(plaintext))
            )
        if row is not None:
            return None
        await self.create_token("bootstrap", plaintext)
        return plaintext

    # --- workspaces ---------------------------------------------------------

    async def create_workspace(
        self,
        spec: VmSpec,
        image_hash: str | None = None,
        host: str | None = None,
        ssh_privkey: str | None = None,
    ) -> dict:
        """Insert a workspace row from its VM spec and artifact facts.

        ``ssh_privkey`` carries the minted identity's private half
        (#111): the spec holds the public half (the seed needs it at
        artifact-build time), the private half goes from mint to row
        without ever riding a spec.
        """
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = Workspace(
                **workspace_fields(spec, image_hash, host, ssh_privkey)
            )
            session.add(row)
            await session.commit()
            return workspace_dict(row)

    async def get_workspace(self, workspace_id: str) -> dict | None:
        """One workspace row as a dict, None when absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Workspace, workspace_id)
            return None if row is None else workspace_dict(row)

    async def list_workspaces(self) -> list[dict]:
        """All workspace rows."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(
                select(Workspace).order_by(Workspace.id)
            )
            return [workspace_dict(row) for row in rows]

    async def set_status(self, workspace_id: str, status: str) -> bool:
        """Record an observed lifecycle status; False when absent."""
        if status not in WORKSPACE_STATUSES:
            raise ValueError(f"unknown workspace status: {status!r}")
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(status=status)
            )
            await session.commit()
            return result.rowcount > 0

    async def set_sizes(
        self, workspace_id: str, root_mib: int | None, home_mib: int | None
    ) -> bool:
        """Record resized artifact sizes (#184); False when absent.

        A ``None`` keeps the column — the route passes only the side
        the request named."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(
                    **{
                        column: value
                        for column, value in (
                            ("root_mib", root_mib),
                            ("home_mib", home_mib),
                        )
                        if value is not None
                    }
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def egress_slice(self, workspace_id: str) -> int | None:
        """The workspace's recorded egress pool slice, None when never
        attached (#70 review)."""
        row = await self.get_workspace(workspace_id)
        return None if row is None else row.get("egress_slice")

    async def set_egress_slice(self, workspace_id: str, slice_: int) -> bool:
        """Record the workspace's pool slice; False when the row is
        absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(egress_slice=slice_)
            )
            await session.commit()
            return result.rowcount > 0

    async def get_ssh_key(self, workspace_id: str) -> dict | None:
        """The workspace's minted identity halves (#111), or None
        when the workspace does not exist. A row without an identity
        (a pre-#111 workspace) returns halves of None — the caller
        distinguishes row-missing from identity-missing."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Workspace, workspace_id)
            if row is None:
                return None
            return {
                "public_key": row.ssh_pubkey,
                "private_key": row.ssh_privkey,
            }

    async def delete_workspace(self, workspace_id: str) -> bool:
        """Remove a workspace row and its consent rows; False when
        absent."""
        await self.egress_consent.delete_for_workspace(workspace_id)
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Workspace, workspace_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # --- placeholders (#198) ------------------------------------------

    async def create_placeholder(
        self,
        workspace_id: str,
        name: str,
        sentinel: str,
        dests: list[str],
        backend_ref: str,
        expires_at=None,
    ) -> dict:
        """Insert a placeholder row; raises IntegrityError on a
        (workspace, name) or backend-ref collision."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = Placeholder(
                workspace_id=workspace_id,
                name=name,
                sentinel=sentinel,
                dests=json.dumps(dests),
                backend_ref=backend_ref,
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()
            return placeholder_dict(row)

    async def list_placeholders(self) -> list[dict]:
        """All placeholder rows, insertion order."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(
                select(Placeholder).order_by(Placeholder.id)
            )
            return [placeholder_dict(row) for row in rows]

    async def get_placeholder(self, placeholder_id: int) -> dict | None:
        """One placeholder row as a dict, None when absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Placeholder, placeholder_id)
            return None if row is None else placeholder_dict(row)

    async def placeholder_for(
        self, workspace_id: str, name: str
    ) -> dict | None:
        """The workspace's placeholder by label, None when absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.scalar(
                select(Placeholder).where(
                    Placeholder.workspace_id == workspace_id,
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

    async def placeholder_refs(self) -> list[tuple[str, str]]:
        """Every (backend_ref, description) pair — the manifest body."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(
                select(Placeholder).order_by(Placeholder.id)
            )
            return [
                (row.backend_ref, f"{row.workspace_id}/{row.name}")
                for row in rows
            ]

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
        """Remove a placeholder row; False when absent."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Placeholder, placeholder_id)
            if row is None:
                return False
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

    # --- secret audit (#198) -----------------------------------------

    async def record_audit(self, kind: str, row: dict) -> None:
        """Append one lifecycle event for a placeholder row."""
        if kind not in AUDIT_KINDS:
            raise ValueError(f"unknown audit kind: {kind!r}")
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            session.add(
                SecretAudit(
                    kind=kind,
                    workspace_id=row["workspace_id"],
                    name=row["name"],
                    dests=(
                        json.dumps(row["dests"])
                        if isinstance(row["dests"], list)
                        else row["dests"]
                    ),
                )
            )
            await session.commit()

    async def list_audit(self, limit: int = 100) -> list[dict]:
        """The newest audit events first (operator view)."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(
                select(SecretAudit)
                .order_by(SecretAudit.id.desc())
                .limit(limit)
            )
            return [audit_dict(row) for row in rows]


def workspace_fields(
    spec: VmSpec,
    image_hash: str | None,
    host: str | None,
    ssh_privkey: str | None,
) -> dict:
    """The ORM column values a VmSpec maps to."""
    return {
        "id": spec.workspace_id,
        "kernel": str(spec.kernel),
        "initrd": None if spec.initrd is None else str(spec.initrd),
        "rootfs": str(spec.rootfs),
        "cmdline": spec.cmdline,
        "cpus": spec.cpus,
        "mem_mib": spec.mem_mib,
        "image_hash": image_hash,
        "host": host,
        "root_mib": spec.root_mib,
        "home_mib": spec.home_mib,
        "egress": spec.egress,
        "egress_mode": spec.egress_mode,
        "egress_allowlist": json.dumps(spec.egress_allowlist)
        if spec.egress_allowlist
        else None,
        "user_data": spec.user_data,
        "ssh_pubkey": spec.ssh_pubkey,
        "ssh_privkey": ssh_privkey,
        "status": "created",
    }


def placeholder_dict(row: Placeholder) -> dict:
    """The API-facing dict for a placeholder row.

    The sentinel is included — mint's response prints it once — but
    list views built from these dicts drop it ("never shown again").
    """
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "sentinel": row.sentinel,
        "dests": json.loads(row.dests),
        "backend_ref": row.backend_ref,
        "created_at": row.created_at.isoformat(),
        "expires_at": (
            None if row.expires_at is None else row.expires_at.isoformat()
        ),
    }


def audit_dict(row: SecretAudit) -> dict:
    """The API-facing dict for an audit row."""
    return {
        "id": row.id,
        "kind": row.kind,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "dests": json.loads(row.dests),
        "created_at": row.created_at.isoformat(),
    }


def workspace_dict(row: Workspace) -> dict:
    """The API-facing dict for a workspace row."""
    return {
        "id": row.id,
        "kernel": row.kernel,
        "initrd": row.initrd,
        "rootfs": row.rootfs,
        "cmdline": row.cmdline,
        "cpus": row.cpus,
        "mem_mib": row.mem_mib,
        "image_hash": row.image_hash,
        "host": row.host,
        "root_mib": row.root_mib,
        "home_mib": row.home_mib,
        "egress": row.egress,
        "egress_slice": row.egress_slice,
        "egress_mode": row.egress_mode or "allow",
        "egress_allowlist": json.loads(row.egress_allowlist)
        if row.egress_allowlist
        else [],
        "user_data": row.user_data,
        "ssh_pubkey": row.ssh_pubkey,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }
