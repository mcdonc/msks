"""Engine/session plumbing and the ORM base for msksd."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Timezone-aware UTC now (stored on every row msksd writes)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """The single declarative base; Alembic autogenerates from it."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def engine_for(db_path: Path) -> AsyncEngine:
    """The async engine for one database file (parent dirs created).

    The file is created mode 0600 when absent: the database records
    workspace `user_data` (#41), which can embed tokens — the same
    protection the seed disk itself gets. sqlite happily uses an
    existing file, so the mode is fixed before the first connect.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch(mode=0o600, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}")


def sessionmaker_for(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """The session factory bound to one engine."""
    return async_sessionmaker(engine, expire_on_commit=False)
