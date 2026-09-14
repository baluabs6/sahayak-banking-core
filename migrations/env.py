"""
Alembic environment, wired to the app's own Settings/Base rather than a
static URL baked into alembic.ini — so the same POSTGRES_DSN env var /
.env file that runs the app also drives migrations, and staging/production
never risk drifting onto a different DSN than the app actually uses.

Runs migrations in 'online' mode only (a synchronous psycopg2-style URL
derived from the app's async asyncpg DSN); this project doesn't use
Alembic's async template since migrations are an ops-time, not
request-time, operation.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.postgres import Base

# Import all ORM models so they're registered on Base.metadata before
# autogenerate compares against it.
from app.models import postgres_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_dsn() -> str:
    """Alembic's default sync engine can't use the app's asyncpg DSN
    directly; swap the driver to psycopg2 for migration runs only. The
    app itself keeps using asyncpg at request time — this only affects
    `alembic upgrade`/`alembic revision --autogenerate`."""
    settings = get_settings()
    return settings.postgres_dsn.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _sync_dsn()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
