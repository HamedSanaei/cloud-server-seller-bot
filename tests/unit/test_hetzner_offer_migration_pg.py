"""Exercise migration 0048 against a real, isolated PostgreSQL TEMP table."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cloud_platform.core.config import get_settings

_MIGRATION_PROBE = """
import importlib.util
import os
import sys
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

spec = importlib.util.spec_from_file_location('offer_billing_0048', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
engine = sa.create_engine(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://'))
try:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text('''
                CREATE TEMP TABLE sellable_offers (
                    provider_key varchar(32) NOT NULL,
                    product_id varchar(64) NOT NULL,
                    location_id varchar(32) NOT NULL,
                    billing_model varchar(32) NOT NULL,
                    CONSTRAINT uq_sellable_offers_provider_product_location
                        UNIQUE (provider_key, product_id, location_id)
                ) ON COMMIT DROP
            '''))
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            connection.execute(sa.text('''
                INSERT INTO sellable_offers VALUES
                ('hetzner', 'cx22', 'fsn1', 'prepaid_monthly_fixed'),
                ('hetzner', 'cx22', 'fsn1', 'hourly')
            '''))
            rows = connection.execute(sa.text(
                "SELECT billing_model FROM sellable_offers WHERE provider_key='hetzner'"
            )).scalars().all()
            assert set(rows) == {'prepaid_monthly_fixed', 'hourly'}
        finally:
            transaction.rollback()
finally:
    engine.dispose()
"""


def test_hourly_and_monthly_coexist_after_0048_upgrade(tmp_path):
    migration = (
        Path(__file__).resolve().parents[2] / "alembic/versions/0048_offer_billing_identity.py"
    )
    # The repository's alembic/ package shadows the installed library when
    # tests run at project root; subprocess cwd isolates the real Alembic API.
    env = dict(os.environ)
    env["DATABASE_URL"] = get_settings().database_url
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", _MIGRATION_PROBE, str(migration)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
