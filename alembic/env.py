"""Alembic environment configuration.

This script configures the Alembic migration context to use the application's
SQLAlchemy metadata. It auto-detects whether to run in offline or online mode.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Ensure the application source tree is importable when running migrations
# directly via `alembic` CLI without an installed package.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from cloud_platform.db.base import Base  # noqa: E402

# Use environment variable for DB URL, falling back to the config value.
DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    context.config.get_main_option("sqlalchemy.url") if context.config else "",
)