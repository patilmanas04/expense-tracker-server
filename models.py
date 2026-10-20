from datetime import datetime, timezone
from decimal import Decimal
import enum
from typing import List, Optional
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
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
