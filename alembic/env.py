"""Alembic environment configuration — async-compatible."""
import asyncio
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# ----- Alembic Config object -----
config = context.config

# -- Configure Python logging from alembic.ini -----
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import the application metadata
# All ORM models must be imported here so that Base.metadata knows about them
from app.database import Base  # noqa: E402  # type: ignore
import app.models.user  # noqa: F401, E402
import app.models.subscription  # noqa: F401, E402
import app.models.device_code  # noqa: F401, E402
import app.models.machine_id  # noqa: F401, E402
import app.models.session  # noqa: F401, E402
import app.models.message  # noqa: F401, E402
import app.models.telemetry_event  # noqa: F401, E402
import app.models.model_cache  # noqa: F401, E402
import app.models.email_queue  # noqa: F401, E402

target_metadata = Base.metadata


def get_url() -> str:
    """Read database URL from app config (not from alembic.ini sqlalchemy.url)."""
    from app.config import get_settings  # noqa: E402
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine.
    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations using it."""
    # Use asyncpg-compatible URL
    url = get_url()
    # Convert postgresql+psycopg:// to postgresql+asyncpg:// for alembic if needed
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    # Windows: default ProactorEventLoop is incompatible with psycopg3 async.
    # Switch to SelectorEventLoop before running.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
