"""Recurring payments: subscriptions, EMIs, rent and other payments that repeat every week, month or year, found
in your own payment history. Nothing about a series is stored: every read works it out again from the payments,
so deletes, merges and "Not a duplicate" are reflected at once.

**What counts**: the owner's parsed INR debits (SMS rows with no user count as the owner's, hand-added cash
included), not Credit Card Payment or Self Transfer (spending.NOT_COUNTED; investments such as SIPs do count, so
spending_category_filter() is not used), not held out as a possible duplicate, in the last WINDOW_DAYS days.
Grouped by categorize.merchant_key (case and extra spaces ignored); no merchant or "Unknown" never groups. Dates
are IST calendar dates.

**Window**: 855 days (2 × 380 + 95), not the design's 400. With 400 days a yearly pair is found only for about
35 days after each renewal; 855 keeps it listed through its renewal and its "late?" grace, after which it drops
off when its older payment leaves the window.

**Detection** (`detect`, pure):
1. The newest gap gives the cadence: 5–9 days weekly, 26–35 monthly, 350–380 yearly; 10–18 and 52–70 are one
   missed week or month. Anything else, including 0 (two payments on one day), gives no series.
2. The run walks back from the newest payment while each gap is in the cadence's range, allowing one missed gap
   per run (none for yearly).
3. It forms when some MIN_PAYMENTS consecutive run payments (3; 2 for yearly) are all within ±15% of their median.
4. Period = the median of the run's gaps (a missed gap counted as two halves), rounded down to whole days;
   next expected = last payment + period. Late once today > next expected + period // 4; stopped (not
   returned) once today > last payment + 2 × period + period // 4.
5. Amount is the newest payment's; "was" (previous_amount) is the payment before, only while the two differ by
   more than both 5% and ₹10.

**Decisions** (`recurring_decisions`, one per owner, merchant and cadence): "Not recurring" (dismissed) moves the
series to `hidden`; "Cancelled" holds while no payment came after the decision's IST day, and a later payment
makes the series active again (the row is kept). Decisions without a current series are ignored.

**Notifications** (`check_after_save`, a post_save check for every cause): only for the newest payment of its
series and never for a dismissed merchant and cadence. `recurring_new` once per merchant and cadence (dedupe
`recurring_new:<cadence>:<sha256 of the key>`), when the series would not exist with that cadence without this
payment, so long-standing subscriptions send no burst. Otherwise `recurring_price` (dedupe
`recurring_price:<transaction id>`) when the price changed ("was" is set) and the two payments before the
newest were within 5% of each other.

**Cost**: `GET /api/v1/recurring` = 2 statements after the session (payments, decisions), reading 4 narrow
columns of up to ~2.3 years of debits; the check = 1 statement, +1 for decisions when this payment is the newest
of a series, +2 (create_once) when it notifies.
"""
import math
import statistics
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import limits
from auth import current_user, same_origin
from categorize import merchant_key
from dashboard import ownership
from database import get_db
from limits import IST, _encodable, _short_label, _utc, format_money
from merchants import has_invalid_characters, name_hash
from models import RecurringDecision, Transaction, User
from notifications import create_once
from post_save import PostSave
from spending import NOT_COUNTED, not_held_out

WINDOW_DAYS = 2 * 380 + 95  # 855: the longest yearly gap twice, plus a yearly grace (see the docstring)
CADENCES = {"weekly": (5, 9), "monthly": (26, 35), "yearly": (350, 380)}
MISSED = {"weekly": (10, 18), "monthly": (52, 70)}  # one missed week or month; ranges are inclusive
MIN_PAYMENTS = {"weekly": 3, "monthly": 3, "yearly": 2}
BAND = Decimal("0.15")  # amounts within ±15% of their median
CHANGE_PCT = Decimal("0.05")  # "was" needs more than 5% …
CHANGE_MIN = Decimal("10")  # … and more than ₹10
# Exact factors: Decimal(52) / 12 isn't, and the total is rounded once to whole rupees.
MONTHLY_FACTOR = {"weekly": Fraction(52, 12), "monthly": Fraction(1), "yearly": Fraction(1, 12)}
UNIT = {"weekly": "a week", "monthly": "a month", "yearly": "a year"}
LINK = "/report/recurring"
ACTIVE, DISMISSED, CANCELLED = "active", "dismissed", "cancelled"
MAX_KEY_LENGTH = 255  # String(255)

Cadence = Literal["weekly", "monthly", "yearly"]


@dataclass(frozen=True)
class Payment:
    txn_id: uuid.UUID
    day: date  # IST
    amount: Decimal
    name: str  # the merchant as written


@dataclass(frozen=True)
class Series:
    merchant_key: str
    name: str
    cadence: str
    period_days: int
    amount: Decimal
    previous_amount: Optional[Decimal]
    payments: int
    first_date: date
    last_date: date
    next_expected: date
    late: bool
    newest_txn_id: uuid.UUID
    steady_before: bool = False  # the two payments before the newest are within 5%; only check_after_save reads it
    status: str = ACTIVE  # set by list_series: active / cancelled / dismissed


@dataclass(frozen=True)
class RecurringList:
    items: list[Series]  # active (late first, then by next date), then cancelled
    hidden: list[Series]  # dismissed
    monthly_total: Decimal  # active only, unrounded


def changed(new: Decimal, old: Decimal) -> bool:
    """True when the price moved by more than both 5% and ₹10."""
    return abs(new - old) > max(CHANGE_PCT * old, CHANGE_MIN)


def _in(gap: int, bounds: tuple[int, int]) -> bool:
    return bounds[0] <= gap <= bounds[1]


def _classify(gap: int) -> Optional[tuple[str, bool]]:
    """(cadence, is_missed) for a gap in days, or None."""
    for cadence, bounds in CADENCES.items():
        if _in(gap, bounds):
            return cadence, False
    for cadence, bounds in MISSED.items():
        if _in(gap, bounds):
            return cadence, True
    return None


def _forms(amounts: list[Decimal], size: int) -> bool:
    for start in range(len(amounts) - size + 1):
        window = amounts[start:start + size]
        middle = statistics.median(window)
        if all(abs(a - middle) <= BAND * middle for a in window):
            return True
    return False


def detect(key: str, payments: list[Payment], today: date) -> Optional[Series]:
    """The series one merchant's payments (oldest first) form, or None."""
    if len(payments) < 2:
        return None
    found = _classify((payments[-1].day - payments[-2].day).days)
    if found is None:
        return None
    cadence, missed_used = found
    start = len(payments) - 2
    while start > 0:
        gap = (payments[start].day - payments[start - 1].day).days
        if _in(gap, CADENCES[cadence]):
            start -= 1
        elif cadence in MISSED and not missed_used and _in(gap, MISSED[cadence]):
            missed_used = True
            start -= 1
        else:
            break
    run = payments[start:]
    if len(run) < MIN_PAYMENTS[cadence] or not _forms([p.amount for p in run], MIN_PAYMENTS[cadence]):
        return None
    gaps: list[Fraction] = []
    for older, newer in zip(run, run[1:]):
        gap = (newer.day - older.day).days
        gaps += [Fraction(gap, 2)] * 2 if not _in(gap, CADENCES[cadence]) else [Fraction(gap)]
    period = math.floor(statistics.median(gaps))
    newest, last = run[-1], run[-1].day
    if today > last + timedelta(days=2 * period + period // 4):
        return None  # stopped
    next_expected = last + timedelta(days=period)
    before = run[-2].amount
    return Series(
        merchant_key=key,
        name=" ".join(newest.name.split()),
        cadence=cadence,
        period_days=period,
        amount=newest.amount,
        previous_amount=before if changed(newest.amount, before) else None,
        payments=len(run),
        first_date=run[0].day,
        last_date=last,
        next_expected=next_expected,
        late=today > next_expected + timedelta(days=period // 4),
        newest_txn_id=newest.txn_id,
        steady_before=len(run) >= 3 and abs(run[-2].amount - run[-3].amount) <= CHANGE_PCT * run[-3].amount,
    )


async def load_payments(db: AsyncSession, owner: User, today: date) -> dict[str, list[Payment]]:
    """Every counted INR debit in the window, grouped by merchant key, oldest first. One statement."""
    since = datetime.combine(today - timedelta(days=WINDOW_DAYS), time.min, tzinfo=IST).astimezone(timezone.utc)
    rows = (await db.execute(
        select(Transaction.id, Transaction.transaction_time, Transaction.amount, Transaction.merchant).where(
            ownership(owner),
            Transaction.is_parsed.is_(True),
            Transaction.type == "DEBIT",
            Transaction.currency == "INR",
            Transaction.transaction_time >= since,
            # Not spending_category_filter(): investments (SIPs) recur too.
            or_(Transaction.category.is_(None), Transaction.category.not_in(sorted(NOT_COUNTED))),
            not_held_out(),
        ).order_by(Transaction.transaction_time, Transaction.id)
    )).all()
    grouped: dict[str, list[Payment]] = {}
    for txn_id, when, amount, merchant in rows:
        key = merchant_key(merchant)
        if key is None:
            continue
        grouped.setdefault(key, []).append(
            Payment(txn_id, _utc(when).astimezone(IST).date(), Decimal(str(amount)), merchant))
    return grouped


async def _decisions(db: AsyncSession, owner_id: uuid.UUID) -> dict[tuple[str, str], tuple[str, date]]:
    """(merchant_hash, cadence) → (status, IST day of the decision). One statement."""
    rows = (await db.execute(select(
        RecurringDecision.merchant_hash, RecurringDecision.cadence, RecurringDecision.status,
        RecurringDecision.decided_at,
    ).where(RecurringDecision.user_id == owner_id))).all()
    return {(h, cadence): (decision, _utc(at).astimezone(IST).date()) for h, cadence, decision, at in rows}


async def list_series(db: AsyncSession, owner: User, today: date) -> RecurringList:
    """Every current series with your decisions applied. Two statements."""
    grouped = await load_payments(db, owner, today)
    decisions = await _decisions(db, owner.id)
    active, cancelled, hidden = [], [], []
    for key, payments in grouped.items():
        series = detect(key, payments, today)
        if series is None:
            continue
        decision = decisions.get((name_hash(series.merchant_key), series.cadence))
        if decision and decision[0] == DISMISSED:
            hidden.append(replace(series, status=DISMISSED))
        elif decision and decision[0] == CANCELLED and series.last_date <= decision[1]:
            cancelled.append(replace(series, status=CANCELLED))
        else:
            active.append(series)  # a cancelled series paid again is active; its row is kept
    active.sort(key=lambda s: (not s.late, s.next_expected, s.name.casefold()))
    cancelled.sort(key=lambda s: (s.name.casefold(), s.cadence))
    hidden.sort(key=lambda s: (s.name.casefold(), s.cadence))
    total = sum((Fraction(s.amount) * MONTHLY_FACTOR[s.cadence] for s in active), Fraction(0))
    return RecurringList(items=active + cancelled, hidden=hidden,
                         monthly_total=Decimal(total.numerator) / Decimal(total.denominator))


class RecurringSeriesOut(BaseModel):
    merchant_key: str
    name: str
    cadence: Cadence
    amount: str  # "649.00"
    previous_amount: Optional[str]  # set only while it differs from amount by more than both 5% and ₹10
    payments: int
    first_date: date
    last_date: date
    next_expected: date
    late: bool
    status: Literal["active", "cancelled", "dismissed"]


class RecurringSummary(BaseModel):
    currency: str = "INR"
    monthly_total: str  # active only, whole rupees: "24067.00"
    items: list[RecurringSeriesOut]
    hidden: list[RecurringSeriesOut]


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _series_out(series: Series) -> RecurringSeriesOut:
    return RecurringSeriesOut(
        merchant_key=series.merchant_key, name=series.name, cadence=series.cadence, amount=_amount(series.amount),
        previous_amount=None if series.previous_amount is None else _amount(series.previous_amount),
        payments=series.payments, first_date=series.first_date, last_date=series.last_date,
        next_expected=series.next_expected, late=series.late, status=series.status,
    )


def summary_out(result: RecurringList) -> RecurringSummary:
    total = result.monthly_total.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return RecurringSummary(monthly_total=_amount(total), items=[_series_out(s) for s in result.items],
                            hidden=[_series_out(s) for s in result.hidden])


class DecisionKey(BaseModel):
    merchant_key: str
    cadence: Cadence

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)  # `_key` then rejects them

    @field_validator("merchant_key")
    @classmethod
    def _key(cls, value: str) -> str:
        if has_invalid_characters(value):
            raise ValueError("Merchant names can't contain invalid characters.")
        key = merchant_key(value)
        if key is None:
            raise ValueError("Enter a merchant name.")
        if len(key) > MAX_KEY_LENGTH:
            raise ValueError(f"Merchant names can be up to {MAX_KEY_LENGTH} characters.")
        return key


class DecisionIn(DecisionKey):
    status: Literal["dismissed", "cancelled"]


router = APIRouter(prefix="/api/v1/recurring", tags=["Recurring payments"])


@router.get("", response_model=RecurringSummary)
async def get_recurring(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    today = limits.utc_now().astimezone(IST).date()
    return summary_out(await list_series(db, user, today))


@router.put("/decisions", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def set_decision(body: DecisionIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Writes or replaces your decision for one merchant and cadence."""
    key_hash = name_hash(body.merchant_key)
    user_id = user.id  # read once: the rollback below expires `user`, and reloading it under asyncio fails

    async def save() -> None:
        row = await db.scalar(select(RecurringDecision).where(
            RecurringDecision.user_id == user_id, RecurringDecision.merchant_hash == key_hash,
            RecurringDecision.cadence == body.cadence))
        now = limits.utc_now()
        if row is None:
            db.add(RecurringDecision(user_id=user_id, merchant_key=body.merchant_key, merchant_hash=key_hash,
                                     cadence=body.cadence, status=body.status, decided_at=now, created_at=now))
        else:
            row.status, row.merchant_key, row.decided_at = body.status, body.merchant_key, now
        await db.commit()

    try:
        await save()
    except IntegrityError:
        await db.rollback()  # two saves raced to create the row; update the winner
        await save()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/decisions/clear", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def clear_decision(body: DecisionKey, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Removes your decision for one merchant and cadence; 204 also when there was none."""
    await db.execute(delete(RecurringDecision).where(
        RecurringDecision.user_id == user.id, RecurringDecision.merchant_hash == name_hash(body.merchant_key),
        RecurringDecision.cadence == body.cadence))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _money_text(amount: Decimal) -> str:
    """"₹649", "₹10,688.10": format_money without a trailing ".00"."""
    return format_money(amount, "INR").removesuffix(".00")


def _day_text(day: date) -> str:
    return f"{day.day} {day:%b}"  # "3 Dec"


async def check_after_save(ctx: PostSave) -> None:
    """Registered with post_save for every cause. The runner already skipped held-out rows, other users' rows and
    a missing owner, and catches anything raised here."""
    txn = ctx.txn
    if not txn.is_parsed or txn.type != "DEBIT" or txn.currency != "INR" or txn.category in NOT_COUNTED:
        return
    key = merchant_key(txn.merchant)
    if key is None:
        return
    owner_id = ctx.owner.id
    today = limits.utc_now().astimezone(IST).date()
    mine = (await load_payments(ctx.db, ctx.owner, today)).get(key, [])
    series = detect(key, mine, today)
    if series is None or series.newest_txn_id != txn.id:
        return  # no series, or an older payment edited / a late SMS
    key_hash = name_hash(key)
    dismissed = await ctx.db.scalar(select(RecurringDecision.id).where(
        RecurringDecision.user_id == owner_id, RecurringDecision.merchant_hash == key_hash,
        RecurringDecision.cadence == series.cadence, RecurringDecision.status == DISMISSED))
    if dismissed is not None:
        return
    label, next_day = _short_label(series.name), _day_text(series.next_expected)
    before = detect(key, [p for p in mine if p.txn_id != txn.id], today)
    if before is None or before.cadence != series.cadence:
        await create_once(ctx.db, owner_id, kind="recurring_new", title=f"New recurring payment: {label}",
                          body=f"{_money_text(series.amount)} {UNIT[series.cadence]}, next around {next_day}",
                          link=LINK, dedupe_key=f"recurring_new:{series.cadence}:{key_hash}")
        return
    if series.previous_amount is not None and series.steady_before:
        direction = "up" if series.amount > series.previous_amount else "down"
        await create_once(ctx.db, owner_id, kind="recurring_price",
                          title=f"{label} went {direction}: {_money_text(series.amount)}",
                          body=f"Was {_money_text(series.previous_amount)}. Next around {next_day}.",
                          link=LINK, dedupe_key=f"recurring_price:{txn.id}")
