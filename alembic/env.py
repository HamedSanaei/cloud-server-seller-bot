"""Alembic environment configuration.

This script configures the Alembic migration context to use the application's
SQLAlchemy metadata. It auto-detects whether to run in offline or online mode.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import engine_from_config, pool

from alembic import context

# Ensure the application source tree is importable when running migrations
# directly via `alembic` CLI without an installed package.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

# Import the application's metadata after sys.path is set up
from cloud_platform.db.base import Base

# Use environment variable for DB URL, falling back to the config value.
# Alembic runs synchronously, so we strip the asyncpg prefix if present.
_default_db_url = "postgresql://cloud:cloud@localhost:5432/cloud"
_raw_db_url = os.environ.get("DATABASE_URL") or (
    context.config.get_main_option("sqlalchemy.url") if context.config else _default_db_url
)
# If the config URL is a placeholder like "driver://user:pass@..." use default
if not _raw_db_url or _raw_db_url.startswith("driver://"):
    DB_URL = _default_db_url
else:
    DB_URL = _raw_db_url.replace("postgresql+asyncpg://", "postgresql://")

# target_metadata is used for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and uses SQLAlchemy's
    ``literal_binds`` to render the SQL directly.
    """
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an engine and connects to the database so migrations can
    run against a live database connection.
    """
    connectable = engine_from_config(
        context.config.get_section(context.config.config_ini_section, {}) if context.config else {},
        url=DB_URL,
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
