"""Database schema definitions for the cloud server platform.

This module defines the SQLAlchemy declarative models that represent
the core domain entities: Users, Wallets, Providers, ProviderAccounts, and Catalog.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
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
        created_at: Record creation timestamp
        updated_at: Last update timestamp
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")
    updated_at = Column(DateTime, server_default="CURRENT_TIMESTAMP", onupdate="CURRENT_TIMESTAMP")

    # Relationships
    account_providers = relationship("ProviderAccount", back_populates="user")
    wallet = relationship("Wallet", uselist=False, back_populates="user")


class Wallet(Base):
    """Immutable ledger for user balances (in minor currency units).

    Attributes:
        id: Primary key
        balance: Current balance in minor currency units (e.g., cents)
    """

    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True)
    balance = Column(Integer, default=0, nullable=False)

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

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    region = Column(String, default=None)
    created_at = Column(DateTime, server_default="CURRENT_TIMESTAMP")

    # Relationships
    accounts = relationship("ProviderAccount", back_populates="provider")


class ProviderAccount(Base):
    """A user's account with a provider.

    Attributes:
        id: Primary key
        provider_id: Foreign key to Provider
        account_id: Identity tied to provider (numeric in current schema)
        status: Account status - active/inactive/suspended
    """

    __tablename__ = "provider_accounts"

    id = Column(Integer, primary_key=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    account_id = Column(Integer, nullable=False, default=0)
    status = Column(String, default="active", nullable=False)

    # Relationships
    provider = relationship("Provider", back_populates="accounts")
    user = relationship("User", back_populates="account_providers")


class Catalog(Base):
    """Catalog entry representing sellable server offerings.

    Attributes:
        id: Primary key
        name: Offer name
        description: Optional description
    """

    __tablename__ = "catalog"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String, default=None)