"""Engine/session plumbing and the ORM base for msksd."""

import sqlite3
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

#: How long a sqlite connection waits for a contended lock before
#: raising (#744), in seconds — ``sqlite3.connect``'s ``timeout``.
#: Writes are milliseconds, but two concurrent writers — a create
#: racing the watcher's status transition, consent verdict pins
#: during bulk activity — can stack behind one another longer than
#: sqlite's 5-second default, which surfaces as a client-facing 500.
BUSY_TIMEOUT_S = 30.0

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


def tighten_db_mode(db_path: Path) -> None:
    """Create the database file 0600, or tighten an older one.

    The database records workspace `user_data` (#41), which can
    embed tokens — the same protection the seed disk itself gets. A
    file carried over from a release before #41 keeps whatever mode
    it was created with, so an over-permissive mode is fixed here,
    not only avoided at creation.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch(mode=0o600, exist_ok=True)
    if db_path.stat().st_mode & 0o777 != 0o600:
        db_path.chmod(0o600)


def enable_wal(db_path: Path) -> None:
    """Switch the database file to WAL journaling (one connection,
    opened and closed again at once).

    The mode lives in the file, not the connection: WAL persists
    across connections and restarts, so setting it at engine
    creation covers every pooled connection and every later engine
    on the same file without per-connection pragmas. In WAL,
    readers stop blocking writers and writers queue on the busy
    timeout instead of failing (#744); a clean final close
    checkpoints and removes the ``-wal``/``-shm`` siblings, which
    inherit the database file's mode. The blocking connect (a
    contended switch can wait out the busy timeout) rides the
    serial lifespan startup, where engine creation happens — never
    a live request; an engine is (re)created lazily only at boot
    and after ``close()``, which tests alone do. The pragma answers
    with the new mode or the old one: a filesystem that cannot
    host WAL's shared-memory file (an NFS-style state dir) says
    ``delete`` instead of raising, and that answer is refused —
    the daemon fails the startup loudly rather than running the
    process on the defaults #744 removed.
    """
    raw = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_S)
    try:
        mode = raw.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode != ("wal",):
            raise OSError(
                f"{db_path}: journal_mode stayed {mode!r}; WAL needs "
                "a filesystem that supports its shared-memory file"
            )
    finally:
        raw.close()


def engine_for(db_path: Path) -> AsyncEngine:
    """The async engine for one database file (parent dirs created).

    The database runs in WAL mode with a busy timeout (#744):
    readers stop blocking writers, and a connection that meets a
    locked file waits out the contender instead of raising into a
    client-facing 500.
    """
    tighten_db_mode(db_path)
    enable_wal(db_path)
    return create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"timeout": BUSY_TIMEOUT_S},
    )


def sessionmaker_for(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """The session factory bound to one engine."""
    return async_sessionmaker(engine, expire_on_commit=False)
