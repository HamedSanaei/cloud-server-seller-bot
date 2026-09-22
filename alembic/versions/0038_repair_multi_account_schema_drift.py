"""Forward repair of Leaseweb multi-account schema drift (LEASEWEB-MULTIACCOUNT).

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-22

WHY THIS REVISION EXISTS
------------------------
A production database reported ``relation "provider_routes" does not exist``
while the release image shipped (and ran) code that queries it, and every offer
upsert failed with ``invalid input for query argument: 'CURRENT_TIMESTAMP'``
(that one was an ORM bug, fixed in the models — nothing here).

The database had DRIFTED: objects that migrations 0035/0037 create were absent
even though the installation reported a newer revision. A drifted database is
repaired forward, never recreated and never re-stamped.

WHAT THIS REVISION DOES
-----------------------
Idempotent, inspector-guarded repair. It creates ONLY what is actually missing:

* ``provider_routes`` (with the ``(provider_key, credential_account_id,
  location_id)`` unique constraint and the ``(provider_key, location_id)``
  index) — created EMPTY; the next successful read-only catalog sync populates
  it;
* ``provider_orders.credential_account_id``;
* ``servers.credential_account_id``.

On a healthy database every one of those objects already exists, so this
revision is a NO-OP for them. It never drops or recreates an existing
(populated) table, never rewrites Alembic history, and never stamps a revision.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* It does **not** touch ``sellable_offers``. Offers that migration 0037 pinned
  to the legacy id ``default`` are re-pinned by the next successful catalog sync
  to the credential account that actually supplies them (the German Sales
  Organization for ``FRA-*``, the UK one for ``LON-*``, and so on). A catalog
  refresh is the authority on provenance; a migration is not.
* It does **not** guess credential-account ownership. Adding a column leaves
  history NULL, which the application treats as "no proven owner" and fails
  closed on (``UnknownCredentialAccountError``).

FAIL CLOSED WHEN OWNERSHIP CANNOT BE PROVEN
-------------------------------------------
If this revision has to ADD a credential-account column to a table that already
holds rows for a MULTI-ACCOUNT provider, then no data in this database can prove
which key created those rows. Assigning them to ``default`` (as migration 0035
did for the pre-multi-account era) would be a guess with real consequences: a
server addressed with the wrong Sales Organization key cannot be managed, and an
order searched in the wrong account can never be found.

So the revision refuses BEFORE any DDL, and prints what to do:

1. Record ownership for those resources from provider evidence (the provider's
   own console says which Sales Organization owns them).
2. Acknowledge the ones that remain unproven, ONCE, in the server-owned
   configuration file (mounted read-only into every container, including the
   ``migrate`` service; the application ignores keys it does not know):

       [database]
       acknowledge_unproven_credential_accounts = true

3. Re-run the deployment. The columns are added, NO ownership value is written,
   and those resources stay fail-closed until an operator records their account
   explicitly. Remove the key afterwards.

Installations with no such history (including the incident database, which has
zero Leaseweb servers and zero Leaseweb provider orders) never see this path.
"""

from __future__ import annotations

import os
import tomllib
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | None = None
depends_on: str | None = None

#: Server-owned configuration knob (see the module docstring).
ACK_KEY = "acknowledge_unproven_credential_accounts"
ACK_SECTION = "database"
CONFIG_FILE_ENV = "CLOUD_PLATFORM_CONFIG_FILE"

ROUTES_TABLE = "provider_routes"
ROUTES_UNIQUE_CONSTRAINT = "uq_provider_routes_account_location"
ROUTES_INDEX = "ix_provider_routes_provider_location"
CREDENTIAL_COLUMN = "credential_account_id"

#: Tables that must carry credential-account provenance.
CREDENTIAL_TABLES: tuple[str, ...] = ("provider_orders", "servers")

#: Providers whose resources come from a *set* of credentials, so an unproven
#: owner cannot be assumed. Single-credential providers are routed logically and
#: are unaffected.
MULTI_ACCOUNT_PROVIDERS: tuple[str, ...] = ("leaseweb",)

#: Account id the deprecated single-credential form maps onto
#: (``cloud_platform.providers.routing.DEFAULT_CREDENTIAL_ACCOUNT``). Used in
#: messages only — this revision never writes it.
LEGACY_ACCOUNT_ID = "default"


def _notify(message: str) -> None:
    """Make the repair visible in the deployment log."""
    print(f"[migration {revision}] {message}", flush=True)


def _inspect() -> Any:
    """A FRESH schema snapshot (an Inspector memoizes what it reflects)."""
    return sa.inspect(op.get_bind())


def _column_names(inspector: Any, table: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table)}


def _unproven_rows(bind: Any, tables: list[str]) -> int:
    """Rows belonging to a multi-account provider in the tables to be altered.

    These are exactly the rows whose credential account cannot be derived from
    anything stored, so they must not be assigned one by inference.
    """
    total = 0
    if "provider_orders" in tables:
        orders = sa.table(
            "provider_orders",
            sa.column("provider_key", sa.String),
        )
        total += int(
            bind.execute(
                sa.select(sa.func.count())
                .select_from(orders)
                .where(orders.c.provider_key.in_(MULTI_ACCOUNT_PROVIDERS))
            ).scalar_one()
            or 0
        )
    if "servers" in tables:
        servers = sa.table(
            "servers",
            sa.column("provider_id", UUID(as_uuid=True)),
        )
        providers = sa.table(
            "providers",
            sa.column("id", UUID(as_uuid=True)),
            sa.column("name", sa.String),
        )
        total += int(
            bind.execute(
                sa.select(sa.func.count())
                .select_from(servers)
                .where(
                    servers.c.provider_id.in_(
                        sa.select(providers.c.id).where(
                            providers.c.name.in_(MULTI_ACCOUNT_PROVIDERS)
                        )
                    )
                )
            ).scalar_one()
            or 0
        )
    return total


def _acknowledged() -> bool:
    """Whether the operator explicitly acknowledged unproven ownership."""
    path = os.environ.get(CONFIG_FILE_ENV)
    if not path:
        return False
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    section = document.get(ACK_SECTION)
    return bool(isinstance(section, dict) and section.get(ACK_KEY))


def _create_provider_routes() -> None:
    op.create_table(
        ROUTES_TABLE,
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column(
            "credential_account_id",
            sa.String(length=64),
            server_default=LEGACY_ACCOUNT_ID,
            nullable=False,
        ),
        sa.Column("location_id", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            server_default="transient_unknown",
            nullable=False,
        ),
        sa.Column(
            "account_state",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column(
            "product_ids",
            JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("last_error_class", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_key",
            "credential_account_id",
            "location_id",
            name=ROUTES_UNIQUE_CONSTRAINT,
        ),
    )
    # The unique constraint travels with the table; the lookup index does not.
    op.create_index(ROUTES_INDEX, ROUTES_TABLE, ["provider_key", "location_id"])
    _notify(
        "provider_routes was MISSING (schema drift) — created empty; the next "
        "successful catalog sync populates it"
    )


def _ensure_routes_constraints(inspector: Any) -> None:
    """A partially applied 0035 can leave the table without its keys."""
    unique_names = {
        str(constraint.get("name")) for constraint in inspector.get_unique_constraints(ROUTES_TABLE)
    }
    if ROUTES_UNIQUE_CONSTRAINT not in unique_names:
        op.create_unique_constraint(
            ROUTES_UNIQUE_CONSTRAINT,
            ROUTES_TABLE,
            ["provider_key", "credential_account_id", "location_id"],
        )
        _notify(f"created the missing unique constraint {ROUTES_UNIQUE_CONSTRAINT}")
    index_names = {str(index.get("name")) for index in inspector.get_indexes(ROUTES_TABLE)}
    if ROUTES_INDEX not in index_names:
        op.create_index(ROUTES_INDEX, ROUTES_TABLE, ["provider_key", "location_id"])
        _notify(f"created the missing index {ROUTES_INDEX}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = _inspect()
    tables = set(inspector.get_table_names())

    # Scope check: this revision repairs drift for the multi-account objects. A
    # database missing the tables themselves has drifted further than that, and
    # silently skipping them would hide a much bigger problem.
    for table in CREDENTIAL_TABLES:
        if table not in tables:
            raise RuntimeError(
                f"0038 repairs multi-account schema drift, but {table} does not exist; "
                "this database has drifted past what this revision can repair — "
                "restore it (or run the full migration chain) before deploying"
            )

    missing_columns = [
        table
        for table in CREDENTIAL_TABLES
        if CREDENTIAL_COLUMN not in _column_names(inspector, table)
    ]

    # FAIL CLOSED, before any DDL: adding provenance to rows that already exist
    # for a multi-account provider would leave the platform unable to address
    # them, and nothing stored here can prove which key created them.
    if missing_columns:
        unproven = _unproven_rows(bind, missing_columns)
        if unproven and not _acknowledged():
            raise RuntimeError(
                f"0038 must add {CREDENTIAL_COLUMN} to {', '.join(missing_columns)}, but "
                f"{unproven} row(s) belong to a multi-account provider "
                f"({', '.join(MULTI_ACCOUNT_PROVIDERS)}). Their owning credential account "
                "cannot be derived from this database, and this revision will NOT guess "
                f"(it will not assign '{LEGACY_ACCOUNT_ID}'). Nothing was changed. Before "
                "deploying: (1) record ownership for those resources from provider "
                "evidence, then (2) for any that remain unproven, add to the server-owned "
                f"configuration file:  [{ACK_SECTION}]  {ACK_KEY} = true  and re-run the "
                "deployment. The columns are then added with NO ownership value written, "
                "and those resources stay fail-closed (UnknownCredentialAccountError) "
                "until an operator records their account. Remove the key afterwards."
            )
        if unproven:
            _notify(
                f"operator acknowledged {unproven} row(s) with unproven credential-account "
                "ownership; adding the columns WITHOUT assigning any account — those "
                "resources stay fail-closed until ownership is recorded explicitly"
            )

    if ROUTES_TABLE not in tables:
        # A missing table is created complete (constraint + index).
        _create_provider_routes()
    else:
        # A PRE-EXISTING table may be a partially applied 0035: repair only the
        # keys an inspector says are absent.
        _ensure_routes_constraints(inspector)

    for table in missing_columns:
        op.add_column(
            table,
            sa.Column(CREDENTIAL_COLUMN, sa.String(length=64), nullable=True),
        )
        _notify(
            f"{table}.{CREDENTIAL_COLUMN} was MISSING (schema drift) — added NULLable; "
            "no ownership value was written (fail closed until an operator records it)"
        )

    if not missing_columns and ROUTES_TABLE in tables:
        _notify("multi-account schema already complete — nothing to repair")


def downgrade() -> None:
    """Refuse: this revision repairs drift and must not destroy state.

    A downgrade would have to drop objects it may not have created (another
    revision's populated table, or columns holding real provenance). The
    platform never downgrades a database automatically, so this raises and tells
    the operator to restore instead.
    """
    raise RuntimeError(
        "0038 has no automatic downgrade: it repaired schema drift and cannot know "
        "which objects it created. Dropping them could destroy provenance or a "
        "populated table — restore the database from a backup instead."
    )
