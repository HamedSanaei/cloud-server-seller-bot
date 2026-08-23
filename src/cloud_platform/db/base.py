"""Database schema definitions for the cloud server platform.

This module defines the SQLAlchemy declarative models that represent
the core domain entities: Users, Wallets, Providers, ProviderAccounts, Catalog, and Cloud Servers.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""

    pass


# Core domain models
class User(Base):
    """Platform user entity.

    Attributes:
        id: Primary key
        username: Unique username
        email: Unique email address
        status: User status - active/frozen/banned
        terms_accepted_at: Timestamp when user accepted latest terms
        created_at: Record creation timestamp
        updated_at: Last update timestamp
    """

    __tablename__ = "users"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    username = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    status = Column(String, nullable=False, server_default="active")
    role = Column(String, nullable=False, server_default="user")
    terms_accepted_at = Column(DateTime, nullable=True)
    telegram_user_id = Column(BigInteger, nullable=True, unique=True)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP", onupdate="CURRENT_TIMESTAMP")

    # Relationships
    account_providers = relationship("ProviderAccount", back_populates="user")
    wallet = relationship("Wallet", uselist=False, back_populates="user")
    servers = relationship("Server", back_populates="user")


class Wallet(Base):
    """Immutable ledger for user balances (in minor currency units).

    Attributes:
        id: Primary key
        user_id: Foreign key to User (one wallet per user)
        balance: Current balance in minor currency units (e.g., cents)
        currency: ISO 4217 currency code
    """

    __tablename__ = "wallets"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    user_id = Column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    balance = Column(BigInteger, default=0, nullable=False)
    currency = Column(String(3), nullable=False, server_default="EUR")
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP", onupdate="CURRENT_TIMESTAMP")

    # Relationships
    user = relationship("User", back_populates="wallet")


class Provider(Base):
    """Cloud infrastructure provider (e.g., Hetzner, local Iranian provider).

    Attributes:
        id: Primary key
        name: Provider identifier (must be unique)
        region: Region name or code
    """

    __tablename__ = "providers"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    name = Column(String, unique=True, nullable=False)
    region = Column(String, nullable=True)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")

    # Relationships
    accounts = relationship("ProviderAccount", back_populates="provider")
    catalog_entries = relationship("Catalog", back_populates="provider")
    servers = relationship("Server", back_populates="provider")


class ProviderAccount(Base):
    """A user's account with a provider.

    Attributes:
        id: Primary key
        provider_id: Foreign key to Provider
        user_id: Foreign key to User
        status: Account status - active/draining/disabled/degraded
        credentials_encrypted: Encrypted provider credentials (JSON)
    """

    __tablename__ = "provider_accounts"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    provider_id = Column(PG_UUID, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String, default="active", nullable=False)
    credentials_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP", onupdate="CURRENT_TIMESTAMP")

    # Relationships
    provider = relationship("Provider", back_populates="accounts")
    user = relationship("User", back_populates="account_providers")
    servers = relationship("Server", back_populates="provider_account")


class Catalog(Base):
    """Catalog entry representing sellable server offerings.

    Attributes:
        id: Primary key
        name: Offer name
        description: Optional description
        provider_id: Foreign key to Provider
        provider_plan_id: Provider's internal plan ID
        provider_location_id: Provider's internal location ID
        architecture: CPU architecture (e.g., x86, arm)
        vcpu: Number of virtual CPUs
        memory_mb: Memory in megabytes
        disk_gb: Disk size in gigabytes
        price_per_quantum: Provider cost per billing quantum (minor units)
        currency: ISO 4217 currency code
        quantum_seconds: Billing quantum in seconds
        enabled: Whether this offer is available for purchase
        extra_metadata: Additional provider-specific data
    """

    __tablename__ = "catalog"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    provider_id = Column(PG_UUID, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    provider_plan_id = Column(String, nullable=False)
    provider_location_id = Column(String, nullable=False)
    architecture = Column(String, nullable=False)
    vcpu = Column(Integer, nullable=False)
    memory_mb = Column(Integer, nullable=False)
    disk_gb = Column(Integer, nullable=False)
    price_per_quantum = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, server_default="EUR")
    quantum_seconds = Column(Integer, nullable=False, server_default="3600")
    enabled = Column(Boolean, nullable=False, server_default="true")
    extra_metadata = Column(JSONB, nullable=False, server_default="{}")
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP", onupdate="CURRENT_TIMESTAMP")

    # Relationships
    provider = relationship("Provider", back_populates="catalog_entries")
    servers = relationship("Server", back_populates="catalog_entry")


class Server(Base):
    """Represents a cloud server instance with lifecycle states.

    Attributes:
        id: Primary key
        user_id: Foreign key to the owning user
        provider_id: Foreign key to the provider
        provider_account_id: Foreign key to the provider account
        catalog_id: Foreign key to the catalog entry (for pricing/specs)
        state: Current lifecycle state
        provider_server_id: External ID from provider (if provisioned)
        ipv4: Public IPv4 address
        ipv6: Public IPv6 address
        price_per_quantum: Locked-in provider cost per quantum at provisioning
        currency: ISO 4217 currency code
        quantum_seconds: Billing quantum in seconds
    """

    __tablename__ = "servers"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    user_id = Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_id = Column(PG_UUID, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    provider_account_id = Column(
        PG_UUID, ForeignKey("provider_accounts.id", ondelete="CASCADE"), nullable=False
    )
    catalog_id = Column(PG_UUID, ForeignKey("catalog.id", ondelete="RESTRICT"), nullable=False)
    state = Column(String, nullable=False, server_default="requested")
    contained_from = Column(String, nullable=True)
    provider_server_id = Column(String, nullable=True)
    ipv4 = Column(String, nullable=True)
    ipv6 = Column(String, nullable=True)
    price_per_quantum = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, server_default="EUR")
    quantum_seconds = Column(Integer, nullable=False, server_default="3600")
    idempotency_key = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP", onupdate="CURRENT_TIMESTAMP")
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="servers")
    provider = relationship("Provider", back_populates="servers")
    provider_account = relationship("ProviderAccount", back_populates="servers")
    catalog_entry = relationship("Catalog", back_populates="servers")


class Outbox(Base):
    """Transactional outbox for reliable event publishing.

    Attributes:
        id: Primary key
        event_type: Type of domain event
        payload: Event data as JSON
        created_at: When the event was enqueued
        processed_at: When the event was published (null = pending)
    """

    __tablename__ = "outbox"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    event_type = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    processed_at = Column(DateTime, nullable=True)


class LedgerEntry(Base):
    """Immutable ledger entry for wallet transactions.

    Attributes:
        id: Primary key
        wallet_id: Foreign key to Wallet
        entry_type: Type of entry (deposit/hold/release/charge/refund/adjustment)
        amount: Amount in minor currency units (positive = credit, negative = debit)
        currency: ISO 4217 currency code
        idempotency_key: Unique key preventing duplicate entries
        reference_type: Type of referenced entity (server/payment/etc.)
        reference_id: ID of referenced entity
        description: Human-readable description
    """

    __tablename__ = "ledger"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    wallet_id = Column(PG_UUID, ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False)
    entry_type = Column(String, nullable=False)
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False)
    idempotency_key = Column(String, nullable=False)
    reference_type = Column(String, nullable=True)
    reference_id = Column(PG_UUID, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")

    # Unique constraint on (wallet_id, idempotency_key) is in migration


# ---------------------------------------------------------------------------
# Hold model
# ---------------------------------------------------------------------------


class Hold(Base):
    """Persistent hold reservation that prevents concurrent overspend.

    Attributes:
        id: Primary key
        wallet_id: Foreign key to Wallet
        amount: Reserved amount in minor currency units
        currency: ISO 4217 currency code
        idempotency_key: Unique key preventing duplicate holds
        status: CREATED/CAPTURED/RELEASED
        created_at: When the hold was created
        captured_at: When captured (non-null if captured)
        released_at: When released (non-null if released)
    """

    __tablename__ = "holds"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    wallet_id = Column(
        PG_UUID,
        ForeignKey("wallets.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False)
    idempotency_key = Column(String, nullable=False)
    status = Column(String, nullable=False, server_default="created")
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    captured_at = Column(DateTime, nullable=True)
    released_at = Column(DateTime, nullable=True)

    # Unique constraint on (wallet_id, idempotency_key) is in migration


# ---------------------------------------------------------------------------
# AuditEvent model — append-only log of sensitive actions
# ---------------------------------------------------------------------------


class AuditEvent(Base):
    """Immutable audit record: who did what to which resource, and why.

    The table is append-only: a database trigger rejects UPDATE and DELETE.
    The ORM attribute for the metadata column is ``event_metadata`` because
    ``metadata`` is reserved by SQLAlchemy's declarative base; the database
    column itself is named ``metadata``.

    Attributes:
        id: Primary key
        actor_type: Kind of actor (user/admin/system)
        actor_id: Id of the acting user, when applicable
        action: Machine-readable action name (e.g. "wallet.adjust")
        resource_type: Kind of resource affected (e.g. "wallet")
        resource_id: Identifier of the affected resource
        reason: Human-readable justification
        event_metadata: Structured extra data (DB column ``metadata``, JSONB)
        occurred_at: When the event happened
    """

    __tablename__ = "audit_events"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    actor_type = Column(String, nullable=False)
    actor_id = Column(PG_UUID, nullable=True)
    action = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)
    resource_id = Column(String, nullable=True)
    reason = Column(Text, nullable=False, server_default="")
    event_metadata = Column("metadata", JSONB, nullable=False, server_default="{}")
    occurred_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")

    # Append-only trigger + indexes are in migration 0005


# ---------------------------------------------------------------------------
# PaymentSession model — gateway-side inbound payment attempts (M09-002)
# ---------------------------------------------------------------------------


class PaymentSession(Base):
    """Persistent record of one inbound payment attempt at a gateway.

    The (gateway_key, gateway_payment_id) pair carries a UNIQUE constraint
    (migration 0006): a replayed webhook cannot create a second session for
    the same external payment.

    Attributes:
        id: Primary key
        user_id: Wallet owner the deposit belongs to
        gateway_key: Identifier of the payment gateway
        gateway_payment_id: External id assigned by the gateway (nullable)
        amount_minor: Positive integer minor units
        currency: ISO-4217 3-letter uppercase code
        status: pending | succeeded | failed
        idempotency_key: Key sent to the gateway on creation
        credited_at: When the matching ledger deposit was posted
        created_at / updated_at: Timestamps
    """

    __tablename__ = "payment_sessions"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    user_id = Column(PG_UUID, nullable=False)
    gateway_key = Column(String, nullable=False)
    gateway_payment_id = Column(String, nullable=True)
    amount_minor = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String, nullable=False, server_default="pending")
    idempotency_key = Column(String, nullable=False)
    credited_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")


# ---------------------------------------------------------------------------
# AbuseCase model — reported abuse mapped to responsible users (M10-006)
# ---------------------------------------------------------------------------


class AbuseCase(Base):
    """One reported abuse finding, linked to the responsible user.

    Attributes:
        id: Primary key
        provider_key: Provider that owns the reported resource
        resource_type: provider_server | ipv4 | ipv6
        resource_id: The reported resource identifier
        user_id: Responsible user (resolved at intake)
        server_id: Internal server, when the resource maps to one
        reason: Human-readable abuse description
        reporter: Who/what reported it (provider name, email, admin)
        status: open | investigating | resolved | dismissed
        created_at / updated_at / resolved_at: Timestamps
    """

    __tablename__ = "abuse_cases"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    provider_key = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)
    resource_id = Column(String, nullable=False)
    user_id = Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    server_id = Column(PG_UUID, ForeignKey("servers.id", ondelete="SET NULL"), nullable=True)
    reason = Column(Text, nullable=False)
    reporter = Column(String, nullable=False)
    status = Column(String, nullable=False, server_default="open")
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    resolved_at = Column(DateTime, nullable=True)

    # user_id + status indexes are in migration 0007


# ---------------------------------------------------------------------------
# PriceBookVersion model — versioned price books (M06-001)
# ---------------------------------------------------------------------------


class PriceBookVersion(Base):
    """One immutable version of a named price book.

    Attributes:
        id: Primary key
        book_name: Book identifier (e.g. "retail-eur")
        version: Monotonic version number (>= 1), unique per book
        effective_at: Instant from which this version prices sales
        rules: Margin rules as JSONB (see pricing.domain.rule_to_dict)
        created_at: Record creation timestamp
    """

    __tablename__ = "price_book_versions"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    book_name = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    effective_at = Column(DateTime(timezone=True), nullable=False)
    rules = Column(JSONB, nullable=False, server_default="[]")
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")

    # uq (book_name, version) + (book_name, effective_at) index in migration 0009


# ---------------------------------------------------------------------------
# ServerPriceSnapshot model — immutable per-server price (M06-002)
# ---------------------------------------------------------------------------


class ServerPriceSnapshot(Base):
    """The immutable price of one server, fixed at provisioning.

    Billing reads this row — never the catalog or price book — so historical
    prices are unaffected by later catalog or price-book changes. One row per
    server; the row is never updated.

    Attributes:
        id: Primary key
        server_id: Unique internal server the snapshot is pinned to
        provider_key / plan_id / location_id / currency: The offer pinned
        cost_minor: Provider cost per quantum (minor units)
        selling_minor: Customer selling price per quantum (minor units)
        book_name / book_version: Price book version the price was derived from
        margin_rule: The margin rule applied (JSONB, see rule_to_dict)
        priced_at: Instant the price was fixed
        created_at: Record creation timestamp
    """

    __tablename__ = "server_price_snapshots"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    server_id = Column(
        PG_UUID,
        ForeignKey("servers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    provider_key = Column(String, nullable=False)
    plan_id = Column(String, nullable=False)
    location_id = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    cost_minor = Column(BigInteger, nullable=False)
    selling_minor = Column(BigInteger, nullable=False)
    book_name = Column(String, nullable=False)
    book_version = Column(Integer, nullable=False)
    margin_rule = Column(JSONB, nullable=False)
    priced_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")


# ---------------------------------------------------------------------------
# Operation model — provider-mutation intent ledger (M07-002)
# ---------------------------------------------------------------------------


class Operation(Base):
    """Durable intent for one provider-mutating action.

    The unique ``operation_key`` is also the IdempotencyKey sent to the
    provider, so the provider applies the mutation at most once per intent.

    Attributes:
        id: Primary key
        operation_key: Unique, deterministic intent key
        operation_type: server_create | ...
        resource_type / resource_id: The target (e.g. server + its id)
        provider_key: Provider that owns the mutation
        status: pending | in_flight | completed | failed
        provider_response: Recorded correlation (JSONB, e.g. provider server id)
        error: Last error, for failed/re-queued operations
        attempts: Number of execution attempts
        created_at / updated_at: Timestamps
    """

    __tablename__ = "operations"

    id = Column(PG_UUID, primary_key=True, server_default="uuid_generate_v4()")
    operation_key = Column(String, unique=True, nullable=False)
    operation_type = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)
    resource_id = Column(PG_UUID, nullable=False)
    provider_key = Column(String, nullable=False)
    status = Column(String, nullable=False, server_default="pending")
    provider_response = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
