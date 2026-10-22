from datetime import date, datetime, timezone
from decimal import Decimal
import enum
from typing import List, Optional
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from database import Base


class TransactionType(str, enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class User(Base):
    """User account model, supporting Google OAuth authentication."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    google_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=True,
    )
    name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    picture: Mapped[Optional[str]] = mapped_column(
        String(1024),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.id})>"


class Transaction(Base):
    """Transaction model storing both parsed expense details and raw SMS."""

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Financial details
    type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )  # DEBIT or CREDIT
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(
        String(10),
        default="INR",
        nullable=False,
    )
    merchant: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )
    category: Mapped[Optional[str]] = mapped_column(
        String(100),
        default="Uncategorized",
        nullable=True,
        index=True,
    )
    account_ref: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )  # e.g., "HDFC XX1234", "ICICI CC XX9012"
    balance: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    reference_id: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )  # UPI Ref / UTR / Txn number

    # SMS / Ingestion metadata
    sender: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )  # e.g., "VK-HDFCBK", "AD-ICICIB"
    raw_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    message_hash: Mapped[Optional[str]] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=True,
    )  # SHA-256 hash of message to prevent duplicate inserts
    is_parsed: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    parsed_by: Mapped[str] = mapped_column(
        String(50),
        default="regex",
        nullable=False,
    )  # "regex" or "ai_agent"
    notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Timestamps
    transaction_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="transactions",
    )

    # Composite Index for fast user transaction history queries
    __table_args__ = (
        Index("idx_transactions_user_time", "user_id", "transaction_time"),
    )

    def __repr__(self) -> str:
        return f"<Transaction {self.type} {self.currency} {self.amount} at {self.merchant} ({self.id})>"


class BalanceSetting(Base):
    """Bank balance the owner entered; transactions after `as_of` adjust it."""

    __tablename__ = "balance_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(
        String(10),
        default="INR",
        nullable=False,
    )
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )  # transactions strictly after this moment are applied
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<BalanceSetting {self.currency} {self.opening_balance} as of {self.as_of}>"


class SpendLimit(Base):
    """Weekly/monthly spending limits; reaching one sends an email alert."""

    __tablename__ = "spend_limits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    weekly_limit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )  # None = no limit
    monthly_limit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    currency: Mapped[str] = mapped_column(
        String(10),
        default="INR",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SpendAlert(Base):
    """One row per limit reached per period, so each alert email goes out once."""

    __tablename__ = "spend_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    period: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )  # "weekly" or "monthly"
    period_start: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )  # Monday / first of the month, IST
    limit_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    spent_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )  # "sending", "sent" or "failed"
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Raising a limit mid-period re-arms the alert because the limit is part of the key.
    __table_args__ = (
        UniqueConstraint("user_id", "period", "period_start", "limit_amount", name="uq_spend_alert_once"),
    )


class Notification(Base):
    """In-app notification shown under the bell on the dashboard's Home screen."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )  # e.g. "spend_limit"
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    link: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )  # dashboard path opened on tap
    dedupe_key: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )  # one notification per event, e.g. "spend_limit:weekly:2026-10-19:5000.00"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notification_once"),
    )


class MerchantCategory(Base):
    """A category you picked for a merchant (by editing or adding a transaction). Later SMS from the
    same merchant get it too, ahead of the keyword rules."""

    __tablename__ = "merchant_categories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    merchant_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )  # lower-case, single-spaced merchant name, e.g. "hungerbox"
    type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )  # DEBIT or CREDIT: paying a merchant and a refund from it differ
    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "merchant_key", "type", name="uq_merchant_category"),
    )


class CategoryLimit(Base):
    """Weekly/monthly spending limit for one category. The overall ("All") limit is SpendLimit."""

    __tablename__ = "category_limits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    weekly_limit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )  # None = no limit
    monthly_limit: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    currency: Mapped[str] = mapped_column(
        String(10),
        default="INR",
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "category", name="uq_category_limit"),
    )

