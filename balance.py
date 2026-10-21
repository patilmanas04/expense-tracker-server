"""Running bank balance: the amount the owner enters, adjusted by every parsed
transaction recorded after it. Credit-card alerts are skipped because card spends
leave the bank account only when the card bill is paid, which is its own debit SMS.
Cash entries added by hand are skipped too: they never pass through the bank."""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import OWNER_EMAIL, current_user, same_origin
from dashboard import ownership
from database import get_db
from models import BalanceSetting, Transaction, User

router = APIRouter(prefix="/api/v1/balance", tags=["Balance"])

MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14, 2)

# Credit-card wording used by HDFC/ICICI alerts: "BLOCK CC", "Avl Limit", "Cardmember", "total due".
CARD_RE = re.compile(
    r"credit\s*card|\bblock\s+cc\b|\bavl\.?\s*(?:limit|lmt)\b|available\s+limit|cardmember|"
    r"\b(?:total|minimum)\s+(?:amount\s+)?due\b",
    re.IGNORECASE,
)
BANK_ACCOUNT_RE = re.compile(r"\b(?:a/c|acct|acc|account)\b", re.IGNORECASE)
CASH_ACCOUNT = "Cash"  # account_ref of a transaction added by hand and paid in cash


def affects_bank_balance(raw_message: str) -> bool:
    """False for credit-card spends, refunds and bill payments received on the card.

    A card bill paid *from* a bank account mentions the account too, so it still counts.
    """
    text = re.sub(r"card\s+account", "card", raw_message, flags=re.IGNORECASE)
    return not CARD_RE.search(text) or bool(BANK_ACCOUNT_RE.search(text))


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
        select(Transaction.type, Transaction.amount, Transaction.raw_message, Transaction.account_ref).where(
            ownership(user),
            Transaction.is_parsed.is_(True),
            Transaction.currency == setting.currency,
            Transaction.transaction_time > _utc(setting.as_of),
        )
    )).all()
    spent = received = Decimal(0)
    counted = excluded = 0
    for kind, amount, raw_message, account_ref in rows:
        if account_ref == CASH_ACCOUNT:
            continue
        if not affects_bank_balance(raw_message):
            excluded += 1
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
