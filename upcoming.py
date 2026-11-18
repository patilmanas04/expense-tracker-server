"""Upcoming payments: what is about to leave your accounts, from two sources, all worked out on read.

* **Notices** (`upcoming_payments`): an auto-debit pre-debit notice or a card bill the bank sent. The table is
  filled from ignored SMS by a later step (notices.py, once real HDFC/ICICI samples are in); until then it is
  empty and every read works with no rows. No user_id: every row is the owner's, like unassigned SMS rows.
* **Expected** items: active recurring series (recurring.py) whose next payment falls in the range, late ones
  included with their past date. A series is left out when an auto-debit notice (dismissed or not) shares a word
  with its name (merchants.share_word) and is due within 7 days of its expected date: the notice replaces it.

**Paid / No payment seen** (`status`, pure; nothing is stored, so a hand-added, edited or deleted payment shows
at once). Payments are the owner's parsed INR rows, not held out as a possible duplicate.
* Auto-debit: paid by a debit dated from the due date − 2 to the due date + 5 (IST), within ±1% of the amount,
  that shares a word with the payee (merchant and SMS text). Not paid: "not_seen" after the due date + 5.
* Card bill: paid when the card's own "payment received" credits (category Credit Card Payment, the same
  account_ref compared without case or extra spaces, dated on or after the notice arrived) reach the minimum,
  or the total when there is no minimum. paid_amount is that sum either way. Not paid: "not_seen" after the
  due date.

`leaves_bank` (for Safe to spend): card bills yes; auto-debits unless the notice is about a credit card
(balance.affects_bank_balance); expected items as their series' newest payment, never a Cash one.

**Cost**: `list_upcoming` = notices 1, payments 1 (only when a notice is open), recurring.list_series 2, newest
transactions 1 (only when a series is expected): 3–5 statements.

**Reminder** (`daily`, jobs.register("upcoming_reminders")): one card_bill_due notification per unpaid card bill
due today or tomorrow, once per bill. No email.
"""
import datetime as dt
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, NamedTuple, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status as http_status
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import limits
import recurring
from auth import current_user, same_origin
from balance import CASH_ACCOUNT, affects_bank_balance
from dashboard import ownership
from database import get_db
from limits import IST, _short_label, _utc
from merchants import share_word
from models import Transaction, UpcomingPayment, User
from notifications import create_once
from spending import not_held_out

logger = logging.getLogger(__name__)

AUTO_DEBIT, CARD_BILL, EXPECTED = "auto_debit", "card_bill", "expected"
CARD_PAYMENT = "Credit Card Payment"
PAID_BEFORE = dt.timedelta(days=2)  # a debit up to 2 days before the due date pays an auto-debit …
PAID_AFTER = dt.timedelta(days=5)  # … and up to 5 days after; after that it's "No payment seen"
AMOUNT_TOLERANCE = Decimal("0.01")  # ±1% of the notice amount, inclusive
MERGE_DAYS = dt.timedelta(days=7)  # a notice within 7 days of a series' expected date replaces it
LOOKBACK = dt.timedelta(days=7)  # notices due up to 7 days ago are still listed
MAX_DAYS = 62
LINK = "/upcoming"
KIND_ORDER = {CARD_BILL: 0, AUTO_DEBIT: 1, EXPECTED: 2}

Kind = Literal["auto_debit", "card_bill", "expected"]
Status = Literal["upcoming", "paid", "not_seen", "late"]


@dataclass(frozen=True)
class Candidate:
    """One payment that could settle a notice (the candidates query)."""
    id: uuid.UUID
    type: str  # DEBIT / CREDIT
    amount: Decimal
    merchant: Optional[str]
    raw_message: str
    category: Optional[str]
    account_ref: Optional[str]
    day: dt.date  # IST date of transaction_time


class Settlement(NamedTuple):
    status: Status  # "upcoming" / "paid" / "not_seen"
    paid_amount: Decimal  # card bills: the matching credits; 0 for auto-debits


@dataclass(frozen=True)
class UpcomingItem:
    id: Optional[uuid.UUID]  # None for expected items
    kind: Kind
    payee: str
    account_ref: Optional[str]
    amount: Decimal
    min_amount: Optional[Decimal]
    date: dt.date
    status: Status
    paid_amount: Decimal
    leaves_bank: bool


def _ref(text: Optional[str]) -> str:
    return " ".join((text or "").split()).casefold()


def _ist_day(moment: dt.datetime) -> dt.date:
    return _utc(moment).astimezone(IST).date()


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def status(notice: UpcomingPayment, payments: Iterable[Candidate], today: dt.date) -> Settlement:
    """Paid, not seen or upcoming for one notice, from the candidate payments (pure)."""
    due = notice.due_date
    amount = _decimal(notice.amount)
    if notice.kind == CARD_BILL:
        ref = _ref(notice.account_ref)
        received = _ist_day(notice.created_at)
        credits = sum((p.amount for p in payments if ref and p.type == "CREDIT" and p.category == CARD_PAYMENT
                       and _ref(p.account_ref) == ref and p.day >= received), Decimal(0))
        target = _decimal(notice.min_amount or notice.amount)  # the minimum, or the total when there is none
        if credits >= target:
            return Settlement("paid", credits)
        return Settlement("not_seen" if today > due else "upcoming", credits)
    for p in payments:
        if (p.type == "DEBIT" and due - PAID_BEFORE <= p.day <= due + PAID_AFTER
                and abs(p.amount - amount) <= amount * AMOUNT_TOLERANCE
                and share_word(notice.payee, f"{p.merchant or ''} {p.raw_message}")):
            return Settlement("paid", Decimal(0))
    return Settlement("not_seen" if today > due + PAID_AFTER else "upcoming", Decimal(0))


def _suppressed(series: recurring.Series, notices: list[UpcomingPayment]) -> bool:
    """True when an auto-debit notice (dismissed or not) stands for this series' next payment."""
    return any(n.kind == AUTO_DEBIT and share_word(n.payee, series.name)
               and abs(n.due_date - series.next_expected) <= MERGE_DAYS for n in notices)


def _sort_key(item: UpcomingItem):
    return item.date, KIND_ORDER[item.kind], item.payee.casefold()


async def _notices(db: AsyncSession, today: dt.date, until: dt.date) -> list[UpcomingPayment]:
    """Notices due from today − 7 to until, dismissed ones too. One statement."""
    return list(await db.scalars(select(UpcomingPayment).where(
        UpcomingPayment.due_date >= today - LOOKBACK, UpcomingPayment.due_date <= until)))


async def _candidates(db: AsyncSession, owner: User, since: dt.date) -> list[Candidate]:
    """The owner's parsed INR debits and card-payment credits from `since` (IST). One statement."""
    start = dt.datetime.combine(since, dt.time.min, tzinfo=IST).astimezone(dt.timezone.utc)
    rows = (await db.execute(select(
        Transaction.id, Transaction.type, Transaction.amount, Transaction.merchant, Transaction.raw_message,
        Transaction.category, Transaction.account_ref, Transaction.transaction_time,
    ).where(
        ownership(owner),
        Transaction.is_parsed.is_(True),
        Transaction.currency == "INR",
        Transaction.transaction_time >= start,
        or_(Transaction.type == "DEBIT", and_(Transaction.type == "CREDIT", Transaction.category == CARD_PAYMENT)),
        not_held_out(),
    ))).all()
    return [Candidate(txn_id, kind, _decimal(amount), merchant, raw or "", category, ref, _ist_day(when))
            for txn_id, kind, amount, merchant, raw, category, ref, when in rows]


async def _newest(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str, Optional[str]]]:
    """raw_message and account_ref of each series' newest payment. One statement."""
    rows = (await db.execute(select(Transaction.id, Transaction.raw_message, Transaction.account_ref)
                             .where(Transaction.id.in_(ids)))).all()
    return {txn_id: (raw or "", ref) for txn_id, raw, ref in rows}


async def list_upcoming(db: AsyncSession, owner: User, today: dt.date, until: dt.date) -> list[UpcomingItem]:
    """Every not-dismissed notice due from today − 7 to until, and every expected payment up to until, sorted by
    date, then card bills, auto-debits, expected, then payee. Used by the route, the daily job and Safe to spend.
    3–5 statements."""
    notices = await _notices(db, today, until)
    open_ = [n for n in notices if n.dismissed_at is None]
    payments: list[Candidate] = []
    if open_:
        since = min(min(n.due_date for n in open_) - PAID_BEFORE, min(_ist_day(n.created_at) for n in open_))
        payments = await _candidates(db, owner, since)
    items = []
    for n in open_:
        settled = status(n, payments, today)
        items.append(UpcomingItem(
            id=n.id, kind=n.kind, payee=n.payee, account_ref=n.account_ref, amount=_decimal(n.amount),
            min_amount=None if n.min_amount is None else _decimal(n.min_amount), date=n.due_date,
            status=settled.status, paid_amount=settled.paid_amount,
            leaves_bank=True if n.kind == CARD_BILL else affects_bank_balance(n.raw_message),
        ))
    series = [s for s in (await recurring.list_series(db, owner, today)).items
              if s.status == recurring.ACTIVE and s.next_expected <= until and not _suppressed(s, notices)]
    if series:
        newest = await _newest(db, [s.newest_txn_id for s in series])
        for s in series:
            found = newest.get(s.newest_txn_id)
            # A row deleted between the two reads: assume it leaves the bank (the cautious answer).
            leaves = True if found is None else affects_bank_balance(found[0]) and found[1] != CASH_ACCOUNT
            items.append(UpcomingItem(
                id=None, kind=EXPECTED, payee=s.name, account_ref=None, amount=s.amount, min_amount=None,
                date=s.next_expected, status="late" if s.late else "upcoming", paid_amount=Decimal(0),
                leaves_bank=leaves,
            ))
    items.sort(key=_sort_key)
    return items


# ---------- API ----------

class UpcomingItemOut(BaseModel):
    id: Optional[uuid.UUID]  # null for expected items
    kind: Kind
    payee: str
    account_ref: Optional[str]
    amount: str  # "649.00"
    min_amount: Optional[str]
    date: dt.date  # "2026-12-12"
    status: Status
    paid_amount: str  # "0.00" for auto-debits and expected items
    leaves_bank: bool


class UpcomingList(BaseModel):
    currency: str = "INR"
    items: list[UpcomingItemOut]  # no total: the app adds up the range it shows


def _item_out(item: UpcomingItem) -> UpcomingItemOut:
    return UpcomingItemOut(
        id=item.id, kind=item.kind, payee=item.payee, account_ref=item.account_ref,
        amount=recurring._amount(item.amount),
        min_amount=None if item.min_amount is None else recurring._amount(item.min_amount),
        date=item.date, status=item.status, paid_amount=recurring._amount(item.paid_amount),
        leaves_bank=item.leaves_bank,
    )


router = APIRouter(prefix="/api/v1/upcoming", tags=["Upcoming payments"])


@router.get("", response_model=UpcomingList)
async def get_upcoming(days: int = Query(30, ge=1, le=MAX_DAYS, description="How many days ahead, today included"),
                       user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    today = limits.utc_now().astimezone(IST).date()
    items = await list_upcoming(db, user, today, today + dt.timedelta(days=days - 1))
    return UpcomingList(items=[_item_out(item) for item in items])


@router.post("/{item_id}/dismiss", status_code=http_status.HTTP_204_NO_CONTENT,
             dependencies=[Depends(same_origin)])
async def dismiss(item_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Hides a notice from the list; 204 also when it was already dismissed."""
    row = await db.get(UpcomingPayment, item_id)
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Upcoming payment not found.")
    if row.dismissed_at is None:
        row.dismissed_at = limits.utc_now()
        await db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


# ---------- Daily reminder ----------

async def daily(db: AsyncSession, owner: User, today: dt.date) -> None:
    """jobs.register("upcoming_reminders", upcoming.daily): one card_bill_due notification per unpaid card bill
    due today or tomorrow, once per bill. Never raises; sends no email."""
    tomorrow = today + dt.timedelta(days=1)
    try:
        for item in await list_upcoming(db, owner, today, tomorrow):
            if item.kind != CARD_BILL or item.status != "upcoming" or item.date not in (today, tomorrow):
                continue
            when = "today" if item.date == today else "tomorrow"
            minimum = f" (minimum {recurring._money_text(item.min_amount)})" if item.min_amount else ""
            await create_once(db, owner.id, kind="card_bill_due",
                              title=f"{_short_label(item.payee)} bill due {when}",
                              body=f"{recurring._money_text(item.amount)} due on {recurring._day_text(item.date)}"
                                   f"{minimum}. No payment seen yet.",
                              link=LINK, dedupe_key=f"card_bill_due:{item.id}")
    except Exception:
        logger.exception("Upcoming payments reminder failed")
        await db.rollback()
