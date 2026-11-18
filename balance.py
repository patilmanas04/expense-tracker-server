"""Running bank balance: the amount the owner enters, adjusted by every parsed
transaction recorded after it. Credit-card alerts are skipped because card spends
leave the bank account only when the card bill is paid, which is its own debit SMS.
Cash entries added by hand are skipped too: they never pass through the bank. Possible duplicates
waiting for your decision (duplicates.py) are skipped too. `classify` gives each transaction since the
saved balance one reason it was skipped, or None when it counts; `GET /api/v1/balance/breakdown` lists them."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import OWNER_EMAIL, current_user, same_origin
from dashboard import ownership
from database import get_db
from models import BalanceSetting, Transaction, User
from parser import BANK_ACCOUNT_RE, CARD_RE, card_text
from spending import not_held_out

router = APIRouter(prefix="/api/v1/balance", tags=["Balance"])

MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14, 2)

CASH_ACCOUNT = "Cash"  # account_ref of a transaction added by hand and paid in cash


def affects_bank_balance(raw_message: str) -> bool:
    """False for credit-card spends, refunds and bill payments received on the card.

    A card bill paid *from* a bank account mentions the account too, so it still counts.
    """
    text = card_text(raw_message)
    return not CARD_RE.search(text) or bool(BANK_ACCOUNT_RE.search(text))


SkipReason = Literal["unreadable", "other_currency", "duplicate", "cash", "credit_card"]
SKIP_REASONS: tuple[SkipReason, ...] = ("unreadable", "other_currency", "duplicate", "cash", "credit_card")
# Totals can't be added up for these: no amount, or mixed currencies.
UNTOTALLED: frozenset[str] = frozenset({"unreadable", "other_currency"})


def classify(is_parsed: bool, currency: str, held_out: bool, account_ref: Optional[str], raw_message: str,
             setting_currency: str) -> Optional[SkipReason]:
    """Why this transaction doesn't move Balance left, or None when it does.

    The first reason in a fixed order wins: unreadable, other currency, possible duplicate, cash, credit card.
    "We don't know the amount" comes before "we know it but it doesn't touch the bank", so each row has
    exactly one reason and the skipped totals add up.
    """
    if not is_parsed:
        return "unreadable"
    if currency != setting_currency:
        return "other_currency"
    if held_out:
        return "duplicate"
    if account_ref == CASH_ACCOUNT:
        return "cash"
    if not affects_bank_balance(raw_message):
        return "credit_card"
    return None


def _money(value) -> str:
    """A decimal string with 2 places, no exponent and never "-0.00", e.g. "45000.00" or "-1249.00"."""
    value = Decimal(value).quantize(Decimal("0.01"))
    if not value:
        value = Decimal("0.00")
    return format(value, "f")


def _utc(value: datetime) -> datetime:
    # SQLite drops the offset; values are always stored in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class BalanceInput(BaseModel):
    amount: Decimal = Field(max_digits=14, decimal_places=2, ge=-MAX_AMOUNT, le=MAX_AMOUNT,
                            description="Bank balance right now (or at `as_of`)")
    currency: str = Field(default="INR", pattern="^[A-Z]{3}$")
    as_of: Optional[AwareDatetime] = Field(default=None, description="Defaults to now")


class BalanceSummary(BaseModel):
    opening_balance: float
    currency: str
    as_of: datetime
    spent: float
    received: float
    current: float
    counted: int
    excluded_card: int


async def summarize(db: AsyncSession, user: User, setting: BalanceSetting) -> BalanceSummary:
    rows = (await db.execute(
        select(Transaction.type, Transaction.amount, Transaction.raw_message, Transaction.account_ref,
               Transaction.is_parsed, Transaction.currency).where(
            ownership(user),
            Transaction.is_parsed.is_(True),
            Transaction.currency == setting.currency,
            Transaction.transaction_time > _utc(setting.as_of),
            not_held_out(),
        )
    )).all()
    spent = received = Decimal(0)
    counted = excluded = 0
    for kind, amount, raw_message, account_ref, is_parsed, currency in rows:
        # held_out=False: not_held_out() already removed them above.
        reason = classify(is_parsed, currency, False, account_ref, raw_message, setting.currency)
        if reason == "credit_card":
            excluded += 1
        if reason is not None:
            continue
        counted += 1
        if kind == "CREDIT":
            received += amount
        else:
            spent += amount
    opening = Decimal(setting.opening_balance)
    return BalanceSummary(
        opening_balance=float(opening),
        currency=setting.currency,
        as_of=_utc(setting.as_of),
        spent=float(spent),
        received=float(received),
        current=float(opening - spent + received),
        counted=counted,
        excluded_card=excluded,
    )


class BalanceStart(BaseModel):
    amount: str  # decimal string, the saved balance
    as_of: datetime  # UTC; transactions strictly after it count


class CountedRow(BaseModel):
    id: uuid.UUID
    effect: str  # "-1249.00" for a debit, "500.00" for a credit
    running: str  # Balance left right after this row


class SkipGroup(BaseModel):
    count: int
    total: Optional[str]  # signed net if they had counted; null for unreadable / other_currency
    ids: list[uuid.UUID]  # oldest first


class SkippedGroups(BaseModel):
    unreadable: SkipGroup
    other_currency: SkipGroup
    duplicate: SkipGroup
    cash: SkipGroup
    credit_card: SkipGroup


class BalanceBreakdown(BaseModel):
    currency: str
    start: BalanceStart
    counted: list[CountedRow]  # oldest first (running-total order)
    skipped: SkippedGroups
    current: str  # equals summarize().current


async def breakdown(db: AsyncSession, user: User, setting: BalanceSetting) -> BalanceBreakdown:
    """Every transaction since the saved balance, in running-total order: the ones that moved it, with the
    balance after each, and the rest grouped by the one reason `classify` gives them."""
    rows = (await db.execute(
        select(Transaction.id, Transaction.type, Transaction.amount, Transaction.currency, Transaction.is_parsed,
               Transaction.account_ref, Transaction.raw_message, Transaction.duplicate_of,
               Transaction.transaction_time, Transaction.created_at)
        .where(ownership(user), Transaction.transaction_time > _utc(setting.as_of))
        .order_by(Transaction.transaction_time, Transaction.created_at, Transaction.id)
    )).all()
    opening = Decimal(setting.opening_balance)
    running = opening
    counted: list[CountedRow] = []
    groups = {reason: {"count": 0, "total": Decimal(0), "ids": []} for reason in SKIP_REASONS}
    for row in rows:
        amount = Decimal(row.amount)
        signed = amount if row.type == "CREDIT" else -amount
        reason = classify(row.is_parsed, row.currency, row.duplicate_of is not None, row.account_ref,
                          row.raw_message, setting.currency)
        if reason is None:
            running += signed
            counted.append(CountedRow(id=row.id, effect=_money(signed), running=_money(running)))
            continue
        group = groups[reason]
        group["count"] += 1
        group["ids"].append(row.id)
        if reason not in UNTOTALLED:
            group["total"] += signed
    skipped = SkippedGroups(**{
        reason: SkipGroup(count=group["count"], ids=group["ids"],
                          total=None if reason in UNTOTALLED else _money(group["total"]))
        for reason, group in groups.items()
    })
    return BalanceBreakdown(
        currency=setting.currency,
        start=BalanceStart(amount=_money(opening), as_of=_utc(setting.as_of)),
        counted=counted,
        skipped=skipped,
        current=_money(running),
    )


async def owner_balance(db: AsyncSession) -> Optional[BalanceSummary]:
    """Balance for the dashboard owner; used by the SMS webhook, which has no session."""
    if not OWNER_EMAIL:
        return None
    row = (await db.execute(
        select(BalanceSetting, User).join(User, BalanceSetting.user_id == User.id).where(User.email == OWNER_EMAIL)
    )).first()
    return await summarize(db, row.User, row.BalanceSetting) if row else None


@router.get("", response_model=Optional[BalanceSummary])
async def get_balance(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    setting = await db.scalar(select(BalanceSetting).where(BalanceSetting.user_id == user.id))
    return await summarize(db, user, setting) if setting else None


@router.get("/breakdown", response_model=BalanceBreakdown)
async def get_breakdown(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    setting = await db.scalar(select(BalanceSetting).where(BalanceSetting.user_id == user.id))
    if setting is None:
        raise HTTPException(404, "No balance set.")
    return await breakdown(db, user, setting)


@router.put("", response_model=BalanceSummary, dependencies=[Depends(same_origin)])
async def set_balance(body: BalanceInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    as_of = (body.as_of or now).astimezone(timezone.utc)
    if as_of > now + timedelta(minutes=5):
        raise HTTPException(422, "The balance date can't be in the future.")

    async def save() -> BalanceSetting:
        setting = await db.scalar(select(BalanceSetting).where(BalanceSetting.user_id == user.id))
        if setting is None:
            setting = BalanceSetting(user_id=user.id)
            db.add(setting)
        setting.opening_balance = body.amount
        setting.currency = body.currency
        setting.as_of = as_of
        await db.commit()
        return setting

    try:
        setting = await save()
    except IntegrityError:
        # Two saves raced to create the row; the unique user_id is authoritative, so update it.
        await db.rollback()
        setting = await save()
    await db.refresh(setting)
    return await summarize(db, user, setting)


@router.delete("", dependencies=[Depends(same_origin)])
async def clear_balance(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    setting = await db.scalar(select(BalanceSetting).where(BalanceSetting.user_id == user.id))
    if setting:
        await db.delete(setting)
        await db.commit()
    return {"status": "cleared"}
