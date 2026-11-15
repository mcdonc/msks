"""The Model state object: every database operation msksd performs."""

import json
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from ..agecrypto import secret_key_hash  # noqa: F401 — the suite reads it here
from ..spec.tokens import validate_token_plaintext
from ..spec.vm import VmSpec
from .db import Base, engine_for, sessionmaker_for, tighten_db_mode
from .egress_consent import EgressConsentModel
from .keys import hash_token, new_token
from .proxies import WorkspaceProxy
from .secret_ops import SecretOpsMixin
from .serialize import (
    proxy_dict,
    resolve_workspace,
    workspace_dict,
    workspace_fields,
)
from .tokens import Token
from .workspaces import WORKSPACE_STATUSES, Workspace

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def alembic_config(db_path: Path) -> AlembicConfig:
    """The programmatic Alembic config for one database path."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


class Model(SecretOpsMixin):
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
        deployed forever. Only a torn *head* is stamped past: the
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

        The plaintext is shown once. A caller-supplied plaintext
        (the bootstrap seed) passes the same grammar guard a minted
        one does (#116): a token the daemon cannot authenticate on
        the websocket handshake is refused at insertion, not
        discovered at first use.
        """
        token = (
            validate_token_plaintext(plaintext)
            if plaintext is not None
            else new_token()
        )
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
        name: str | None = None,
    ) -> dict:
        """Insert a workspace row from its VM spec and artifact facts.

        ``name`` is the operator-chosen label (#246) — the spec's
        ``workspace_id`` is the daemon-minted immutable id, so the
        label rides beside it. The row carries the public half
        alone (#486, #497): msks never holds a private half.
        """
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = Workspace(**workspace_fields(spec, image_hash, host, name))
            session.add(row)
            await session.commit()
            return workspace_dict(row)

    async def get_workspace(self, ref: str) -> dict | None:
        """One workspace row by reference — its immutable id or its
        unique name (#246) — None when neither matches.

        Id first: the create route keeps live ids and live names in
        one disjoint namespace, so the order is a canonical
        preference, not a tiebreak — and a pre-#246 row's label IS
        its id, so its operators' muscle memory lands here too.
        """
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Workspace, ref)
            if row is None:
                row = await session.scalar(
                    select(Workspace).where(Workspace.name == ref)
                )
            if row is None:
                return None
            return workspace_dict(row)

    async def list_workspaces(self) -> list[dict]:
        """All workspace rows, oldest first."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            rows = await session.scalars(
                select(Workspace).order_by(Workspace.created_at, Workspace.id)
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

    async def set_topology(
        self, workspace_id: str, cpus: int | None, mem_mib: int | None
    ) -> bool:
        """Record a resized workspace's cpus and memory (#277);
        False when the row is absent.

        A ``None`` keeps the column — the route passes only the
        side the request named. The row is the whole change: the
        boot builds its VmSpec from it, so the values apply at the
        next start."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(
                    **{
                        column: value
                        for column, value in (
                            ("cpus", cpus),
                            ("mem_mib", mem_mib),
                        )
                        if value is not None
                    }
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def set_mounts(
        self, workspace_id: str, mounts: list[dict] | None
    ) -> bool:
        """Record a workspace's mount list (#620, part #621);
        False when the row is absent.

        The whole list writes at once (set/replace semantics, the
        resize pattern): the caller has validated it against the
        row rules and the daemon's gates. None clears the column
        — the mountless row. The next start reads the row; a
        running VM is untouched."""
        maker = sessionmaker_for(self.engine())
        encoded = json.dumps(mounts) if mounts is not None else None
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(mounts=encoded)
            )
            await session.commit()
            return result.rowcount > 0

    async def set_attached_mounts(
        self, workspace_id: str, mounts: list[dict] | None
    ) -> bool:
        """Record the mounts a start attached (#756); False when
        the row is absent.

        Only a successful start writes here — the snapshot of the
        set the launch actually took (the configured set minus
        its skips), the same wire shape ``set_mounts`` stores. An
        empty list records a start that attached nothing (the
        column's NULL, the same as never-started: both leave the
        flag pending while the row mounts anything). The row read
        beside ``mounts`` is the pending story; the next start is
        the only writer that reconciles the pair."""
        maker = sessionmaker_for(self.engine())
        encoded = json.dumps(mounts) if mounts else None
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(attached_mounts=encoded)
            )
            await session.commit()
            return result.rowcount > 0

    async def set_booted_sizes(
        self,
        workspace_id: str,
        cpus: int,
        mem_mib: int,
        root_mib: int,
    ) -> bool:
        """Record the sizes a start booted (#898); False when the
        row is absent.

        Only a successful start writes here — the snapshot of the
        row's sizes at the launch (the deferred half a resize
        moves: root growth and the topology; home bytes apply at
        once and ride nothing). The row read beside its own sizes
        is the pending story; the next start is the only writer
        that reconciles the pair — :meth:`set_attached_mounts`
        own shape (#756), carried to sizes."""
        maker = sessionmaker_for(self.engine())
        encoded = json.dumps(
            {"cpus": cpus, "mem_mib": mem_mib, "root_mib": root_mib}
        )
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(booted_sizes=encoded)
            )
            await session.commit()
            return result.rowcount > 0

    async def set_egress_policy(
        self, workspace_id: str, mode: str, allowlist: list[str] | None
    ) -> bool:
        """Record a workspace's egress posture (#280); False when
        the row is absent.

        ``mode`` always writes. ``allowlist`` writes when given —
        a None keeps the column, so a mode-only switch leaves the
        static allowlist the workspace already carries. The next
        boot builds from the row; a live swap reads it too."""
        maker = sessionmaker_for(self.engine())
        encoded = (
            json.dumps(list(allowlist)) if allowlist is not None else None
        )
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(
                    **{
                        column: value
                        for column, value in (
                            ("egress_mode", mode),
                            ("egress_allowlist", encoded),
                        )
                        if value is not None
                    }
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def set_secret_coverage(
        self, workspace_id: str, coverage: str
    ) -> bool:
        """Record a workspace's daemon-wide placeholder posture
        (#339); False when the row is absent. The interceptor's
        next refresh reads the new setting — a live workspace's
        armed state re-evaluates with it."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(secret_coverage=coverage)
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

    async def get_ssh_key(self, ref: str) -> dict | None:
        """The workspace's ssh identity halves (#111, #486) and
        login user (#248), or None when the workspace does not
        exist. The
        workspace is resolved by id or name (#246) like every
        route-facing lookup, and the answer carries the row's
        immutable id and name so a client keys its caches on
        instance identity, not the reference it typed. A row
        without an identity (a pre-#111 workspace) returns halves of
        None — the caller distinguishes row-missing from
        identity-missing — and a row without a login user (a
        pre-#248 one) returns None for it the same way."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await resolve_workspace(session, ref)
            if row is None:
                return None
            return {
                "id": row.id,
                "name": row.name,
                "public_key": row.ssh_pubkey,
                # The workspace instance's stamp (#245): the client
                # keys its host-key cache on it, so a recreated
                # workspace (the same name, a new instance) starts
                # with a fresh cache instead of refusing its own
                # first-boot host keys.
                "created_at": str(row.created_at),
                # The workspace's recorded login user (#248); None on
                # a row created before per-workspace users.
                "login_user": row.login_user,
                # The disk set epoch (#697): 0 from create, bumped
                # whenever the daemon recreated the root overlay
                # under the live row (a boot's heal, a factory
                # reset). A bumped epoch means the guest's ssh host
                # keys regenerated with the fresh overlay, and the
                # client rotates its host-key cache on the change.
                "disk_epoch": row.disk_epoch,
                # The seed's epoch (#803): a monotonic counter,
                # one bump per seed bake or renewal. A renewal
                # re-provisions the guest and the stock cloud-init
                # ssh module regenerates its host keys, so the
                # client rotates its host-key cache on a change
                # here too.
                "seed_epoch": row.seed_epoch,
            }

    async def bump_disk_epoch(self, workspace_id: str) -> bool:
        """Advance the workspace's disk epoch by one (#697); False
        when the row is absent — a heal under a row the daemon
        does not know (the driver's fixture launches, a boot before
        the row lands mid-create) has no client cache to rotate,
        so the bump is simply not theirs to record."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(disk_epoch=Workspace.disk_epoch + 1)
            )
            await session.commit()
            return result.rowcount > 0

    async def bump_seed_epoch(self, workspace_id: str) -> bool:
        """Advance the workspace's seed epoch by one (#803); False
        when the row is absent — a bake under a row the daemon
        does not know (the create flow's prepare, a fixture
        launch) has no client cache to rotate, so the bump is
        simply not theirs to record."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            result = await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(seed_epoch=Workspace.seed_epoch + 1)
            )
            await session.commit()
            return result.rowcount > 0

    async def delete_workspace(self, workspace_id: str) -> bool:
        """Remove a workspace row, its consent rows, and its proxy
        rows; False when absent."""
        await self.egress_consent.delete_for_workspace(workspace_id)
        await self.delete_proxies_for_workspace(workspace_id)
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(Workspace, workspace_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # --- workspace proxies (#807, #808) -------------------------------

    async def create_proxy(
        self,
        workspace_id: str,
        direction: str,
        listen_host: str | None,
        listen_port: int,
        target_host: str | None,
        target_port: int,
        label: str | None = None,
    ) -> dict:
        """Insert one proxy row for a workspace (#808). The caller
        has validated the fields against the direction's rules; the
        row is the whole definition the daemon reads at every
        workspace start."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = WorkspaceProxy(
                workspace_id=workspace_id,
                direction=direction,
                listen_host=listen_host,
                listen_port=listen_port,
                target_host=target_host,
                target_port=target_port,
                label=label,
            )
            session.add(row)
            await session.commit()
            return proxy_dict(row)

    async def list_proxies(
        self, workspace_id: str | None = None
    ) -> list[dict]:
        """Proxy rows, insertion order — one workspace's when its id
        is given, every workspace's when None (#809's TUI page reads
        the daemon-wide form)."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            query = select(WorkspaceProxy).order_by(WorkspaceProxy.id)
            if workspace_id is not None:
                query = query.where(
                    WorkspaceProxy.workspace_id == workspace_id
                )
            rows = await session.scalars(query)
            return [proxy_dict(row) for row in rows]

    async def get_proxy(self, workspace_id: str, proxy_id: int) -> dict | None:
        """One workspace's proxy row, None when absent — a proxy id
        from another workspace answers None, not its row."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(WorkspaceProxy, proxy_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return proxy_dict(row)

    async def update_proxy(
        self,
        workspace_id: str,
        proxy_id: int,
        direction: str,
        listen_host: str | None,
        listen_port: int,
        target_host: str | None,
        target_port: int,
        label: str | None = None,
    ) -> dict | None:
        """Rewrite one proxy row (#890); None when the workspace
        does not own it. The caller has validated the fields
        against the direction's rules; the row is replaced whole,
        the listener swap is the caller's."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(WorkspaceProxy, proxy_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            row.direction = direction
            row.listen_host = listen_host
            row.listen_port = listen_port
            row.target_host = target_host
            row.target_port = target_port
            row.label = label
            await session.commit()
            return proxy_dict(row)

    async def delete_proxy(self, workspace_id: str, proxy_id: int) -> bool:
        """Remove one proxy row; False when the workspace does not
        own it."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            row = await session.get(WorkspaceProxy, proxy_id)
            if row is None or row.workspace_id != workspace_id:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_proxies_for_workspace(self, workspace_id: str) -> None:
        """Remove every proxy row a workspace owns (workspace
        delete's cascade)."""
        maker = sessionmaker_for(self.engine())
        async with maker() as session:
            await session.execute(
                delete(WorkspaceProxy).where(
                    WorkspaceProxy.workspace_id == workspace_id
                )
            )
            await session.commit()
