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
    case,
    select,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship
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
    )  # "regex", "regex+user", "regex+history", "regex+ai", "regex+rule", "ai_agent", "manual", "edited" or "failed"
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


class NotificationDismissal(Base):
    """A notification you swiped away or cleared. It's hidden, not deleted: the notification row
    stays, so the alert it stands for still counts as sent and isn't repeated on the next payment."""

    __tablename__ = "notification_dismissals"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )



class MerchantLimit(Base):
    """Weekly/monthly spending limit for one merchant name, exactly as picked or typed (merchants.py
    decides which payments it counts)."""

    __tablename__ = "merchant_limits"

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
    merchant: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )  # case kept: "Swiggy" and "SWIGGY" are different limits
    merchant_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )  # SHA-256 of `merchant` (merchants.name_hash)
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
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )  # list order
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Unique on the hash, not the name: under a case-insensitive collation (MySQL's default) the
    # name itself would make "Swiggy" and "SWIGGY" collide, but their hashes always differ.
    __table_args__ = (
        UniqueConstraint("user_id", "merchant_hash", name="uq_merchant_limit"),
    )


class KnownMerchant(Base):
    """A merchant you typed when adding a limit, spelled exactly as typed. Merchants you've paid
    are known from their payments; this keeps typed ones known (with their case, which merchant
    memory doesn't keep) even after their limit is deleted."""

    __tablename__ = "known_merchants"

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
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    name_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )  # SHA-256 of `name` (merchants.name_hash)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # As for MerchantLimit: the hash keeps names that differ only in case apart on every database.
    __table_args__ = (
        UniqueConstraint("user_id", "name_hash", name="uq_known_merchant"),
    )


DUPLICATE_PENDING = "pending"  # held out of every total until you decide
DUPLICATE_DISMISSED = "dismissed"  # "Not a duplicate": counts, and is never flagged again


class DuplicateFlag(Base):
    """A transaction that may be a second record of an earlier one (duplicates.py). While pending it's
    held out of every total; a dismissed row stays so the transaction is never flagged again. Rows are
    removed explicitly when either transaction is deleted (the foreign keys are only a backup)."""

    __tablename__ = "duplicate_flags"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        primary_key=True,
    )  # the newer one, held out while pending
    original_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )  # the earlier one
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )  # "reference", "same_details" or "manual_entry"
    status: Mapped[str] = mapped_column(
        String(10),
        default=DUPLICATE_PENDING,
        nullable=False,
    )  # "pending" or "dismissed"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )  # set by "Not a duplicate"


class MerchantAlias(Base):
    """A merged name: every payment named `from_name` (any case, extra spaces ignored) is saved and
    shown as `to_name` (rules.py). Applied on write: past payments are renamed when it's saved."""

    __tablename__ = "merchant_aliases"

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
    from_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )  # the name as you saw it ("SWIGGY"), cleaned; for display
    from_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )  # categorize.merchant_key(from_name): lower case, single spaces
    from_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )  # SHA-256 of `from_key` (merchants.name_hash)
    to_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )  # the merged name, cleaned like a typed merchant name
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Unique on the hash, as for MerchantLimit: no database collation changes which name a merge is for.
    __table_args__ = (
        UniqueConstraint("user_id", "from_hash", name="uq_merchant_alias"),
    )


class CategoryRule(Base):
    """A category rule: payments matching every condition set (words in the name, words in the SMS, a
    smallest INR amount, money out or in) get its category. The newest matching rule wins (rules.py)."""

    __tablename__ = "category_rules"

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
    merchant_words: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )  # words in the merchant name
    sms_words: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )  # words in the SMS text
    min_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )  # INR, inclusive
    direction: Mapped[Optional[str]] = mapped_column(
        String(6),
        nullable=True,
    )  # "DEBIT" or "CREDIT"; None = either
    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )  # never Uncategorized
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )  # newest first decides the order
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UserSetting(Base):
    """Your preferences, one row per setting (settings_store.KEYS). Only preferences: feature state lives in the
    feature's own table. A missing row means the default; a stored JSON null means Off."""

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # KEY is a reserved word in MySQL/TiDB. SQLAlchemy quotes it, so no raw SQL may use this table.
    key: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )  # e.g. "alerts.large_amount"
    value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )  # JSON text, e.g. 5000, null, true
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )  # set in Python


# The original's id while the flag is pending, else None: every TransactionResponse carries it (one
# sub-select per row, on the primary key). Loaded with the row, so refresh a new row before reading it.
Transaction.duplicate_of = column_property(
    select(DuplicateFlag.original_id)
    .where(DuplicateFlag.transaction_id == Transaction.id, DuplicateFlag.status == DUPLICATE_PENDING)
    .correlate_except(DuplicateFlag)
    .scalar_subquery()
)


class RecurringDecision(Base):
    """Your decision about one recurring series (recurring.py): "Not recurring" (dismissed) or "Cancelled".
    The series themselves are computed on read and never stored."""

    __tablename__ = "recurring_decisions"

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
    merchant_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )  # categorize.merchant_key form: casefolded, spaces collapsed
    merchant_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )  # SHA-256 of `merchant_key` (merchants.name_hash)
    cadence: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )  # weekly / monthly / yearly
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )  # dismissed / cancelled
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )  # set in Python on every write; a later payment makes a cancelled series active again
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Unique on the hash, not the key, as merchant_limits: TiDB's collation can't merge two names.
    __table_args__ = (
        UniqueConstraint("user_id", "merchant_hash", "cadence", name="uq_recurring_decision"),
    )


class JobRun(Base):
    """When each daily job (jobs.py) last claimed its run. Runner state, not a preference, so it isn't in
    user_settings and needs no owner. One row per job name, created on its first run."""

    __tablename__ = "job_runs"

    name: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )  # as registered, e.g. "watchdog"
    last_run: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )  # UTC, set in Python by jobs._claim


class UpcomingPayment(Base):
    """A payment the bank said is coming (an auto-debit pre-debit notice or a card bill), stored from an ignored
    SMS (upcoming.py). No user_id: notices arrive through the webhook, so every row is the owner's, like
    unassigned SMS transactions. Paid / "No payment seen" are computed on read, never stored."""

    __tablename__ = "upcoming_payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    kind: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
    )  # auto_debit / card_bill
    payee: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )  # "Netflix", "HDFC card XX9012"
    account_ref: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )  # as Transaction.account_ref: "HDFC XX9012"
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )  # the amount due (a card bill's total)
    min_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )  # a card bill's minimum due
    due_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )  # IST calendar date
    notice_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )  # SHA-256 of "<kind>|<ref>|<due ISO>": a repeated reminder SMS stores nothing new
    raw_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )  # when the notice was received
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )  # swiped away on /upcoming

    __table_args__ = (
        UniqueConstraint("notice_key", name="uq_upcoming_notice"),
    )


class RefundWatch(Base):
    """A payment you expect money back for (refunds.py). Open while credit_id is null, refunded once you confirm
    a credit. "No refund expected" deletes the row. Rows are cleaned up explicitly when either transaction is
    deleted (main.delete_transaction); the foreign keys are only a backup."""

    __tablename__ = "refund_watches"

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
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
    )  # the payment (a parsed debit)
    expected_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )  # more than zero, at most the payment
    due_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )  # IST calendar date: "expected by"
    credit_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
    )  # the credit you confirmed as the refund
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )  # set when you confirm the refund

    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_refund_watch_transaction"),
        UniqueConstraint("credit_id", name="uq_refund_watch_credit"),  # one credit closes one watch; NULLs repeat
    )


# "expected" / "refunded" when this row is a watched payment, else None; and, on a credit you confirmed as a
# refund, the payment it refunded. One sub-select each per row, on a unique key, like duplicate_of.
Transaction.refund = column_property(
    select(case((RefundWatch.credit_id.is_(None), "expected"), else_="refunded"))
    .where(RefundWatch.transaction_id == Transaction.id)
    .correlate_except(RefundWatch)
    .scalar_subquery()
)
Transaction.refund_for = column_property(
    select(RefundWatch.transaction_id)
    .where(RefundWatch.credit_id == Transaction.id)
    .correlate_except(RefundWatch)
    .scalar_subquery()
)


class TransferPair(Base):
    """A suggested transfer between two of your own accounts (transfers.py): a bank-account debit and a same-amount
    credit into a different account within 30 minutes. pending until you Confirm (both become Self Transfer) or
    reject; expired when Confirm's re-check fails. Unique on (debit, credit), so a rejected pair is never suggested
    again. Rows are deleted explicitly with either transaction (main.delete_transaction); the foreign keys are only
    a backup."""

    __tablename__ = "transfer_pairs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )  # set in code before insert, so the notification's dedupe_key "pair:<id>" is built first
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    debit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )  # always the DEBIT leg
    credit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )  # always the CREDIT leg
    status: Mapped[str] = mapped_column(
        String(10),
        default="pending",
        nullable=False,
    )  # "pending", "confirmed", "rejected" or "expired"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )  # set on confirm, reject or expire

    __table_args__ = (
        UniqueConstraint("debit_id", "credit_id", name="uq_transfer_pair"),
    )


# Lend & borrow (ledger.py). Each kind's sign in a person's balance: positive = they owe you.
LEDGER_KINDS = {"lent": 1, "repaid_by_me": 1, "borrowed": -1, "repaid_to_me": -1}
DEBIT_KINDS = frozenset({"lent", "repaid_by_me"})  # on a payment (or cash)
CREDIT_KINDS = frozenset({"borrowed", "repaid_to_me"})  # on money received (or cash)


class LedgerEntry(Base):
    """Money between you and a person (ledger.py). Linked to a parsed INR transaction (`transaction_id`, its
    share of that payment or credit; totals then count the transaction's amount minus its entries), or a cash
    entry with its own `day`. Kinds and signs: lent +1, repaid_by_me +1 (a payment, or cash you gave);
    borrowed −1, repaid_to_me −1 (money received, or cash you got). A person's balance is Σ sign × amount;
    positive means they owe you. Rows are deleted explicitly with their transaction (main.delete_transaction);
    the foreign key is only a backup."""

    __tablename__ = "ledger_entries"

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
    person: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )  # as typed, spaces collapsed
    person_key: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )  # ledger.person_key: groups "Rahul" and "rahul "
    kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )  # a LEDGER_KINDS key
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )  # more than zero
    day: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
    )  # IST day, cash entries only; a linked entry uses its transaction's date
    transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    note: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        # Microseconds on TiDB/MySQL too (a plain DATETIME there drops them): the entries of one PUT are a
        # microsecond apart to keep the order they were sent in, and GET /ledger orders by it.
        DateTime(timezone=True).with_variant(mysql.DATETIME(timezone=True, fsp=6), "mysql"),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_ledger_person", "user_id", "person_key"),
    )



class LimitRollover(Base):
    """Rollover on a monthly limit (limits.py): while a row exists, the scope's monthly limit adds what you didn't
    spend last month, or takes off what you overspent, clamped to ₹0 … twice the limit. `category` is "All" for the
    overall limit, else the category limit's category. Carry starts with the first month that begins after
    `enabled_at` (IST). Turning it off, removing the limit or its monthly amount deletes the row."""

    __tablename__ = "limit_rollovers"

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
    )  # "All" = the overall limit
    enabled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )  # turning it on again while on keeps the first

    __table_args__ = (
        UniqueConstraint("user_id", "category", name="uq_limit_rollover"),
    )


class SavingsGoal(Base):
    """Something you're saving toward (goals.py). Progress is worked out in the app: `manual_saved` plus every
    parsed INR payment tagged #`tag` (its counted amount). One active goal per tag."""

    __tablename__ = "savings_goals"

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
    name: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
    )
    target: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )  # more than zero
    target_month: Mapped[Optional[str]] = mapped_column(
        String(7),
        nullable=True,
    )  # "YYYY-MM"; None = no target month
    tag: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )  # normalised (goals.clean_tag), without "#"
    manual_saved: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        default=0,
        nullable=False,
    )  # "Saved so far" added by hand, never below zero
    archived: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class CustomCategory(Base):
    """A category of your own (categories.py): a name next to the built-in list, with an icon and a colour the
    app draws. `name_key` is the name case-folded: names are unique per user without case."""

    __tablename__ = "custom_categories"

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
    name: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )  # as stored on transactions, rules and limits
    name_key: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )
    icon: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )  # categories.ICONS
    colour: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )  # categories.COLOURS
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name_key", name="uq_custom_category"),
    )

# Σ of the transaction's lend & borrow entries (0 when none); totals count amount − this. One sub-select per row on
# an indexed key, like duplicate_of. Compare it as Decimal(str(...)): SQLite may hand back an int or a float.
Transaction.linked_amount = column_property(
    select(func.coalesce(func.sum(LedgerEntry.amount), 0))
    .where(LedgerEntry.transaction_id == Transaction.id)
    .correlate_except(LedgerEntry)
    .scalar_subquery()
)
