"""Alembic environment: derives the URL from msks settings."""

from alembic import context
from msks.model import Base
from msks.settings import Settings
from sqlalchemy import engine_from_config, pool

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    # Bare `alembic` CLI use only; the daemon always passes the
    # programmatic URL for the live database path.
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite:///{Settings.from_env().server.db_path}",
    )
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        # The engine (and its sqlite handle) is per-run; leaving it to
        # the garbage collector trips ResourceWarnings under pytest.
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
