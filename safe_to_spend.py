"""Safe to spend until payday: Balance left minus what is still due to leave the bank before the next salary.

**Formula**: `safe = balance − Σ subtracted`, where `balance` is Balance left (`balance.summarize(...).current`) and
the items are every `upcoming.list_upcoming(today, until)` item (notices due from today − 7, expected payments of
recurring series, late ones included), each subtracting what `subtract` says. `safe` may be negative (short).
`per_day = floor(safe / days)` in whole rupees, only when `safe > 0` and the state is `upcoming` or `month_end`.

**Payday** (`payday`, pure): the watchdog's rule (doc 13), with its constants. With at least `SALARY_MIN` (2) salary
days on or after today − `SALARY_WINDOW` (120 days), the expected salary is
`pay_periods.expected_after(days, pay_periods.usual_offset(days))` (all salary days are passed, as the watchdog
does):
* after today → `upcoming`; items due up to the day before it count; `days` = days from today to it.
* today → `today`; items due up to today count; `days` is None.
* before today → `late` (the salary hasn't been seen yet); items due up to today count; `days` is None.
Otherwise → `month_end`: next payday is the 1st of next month, items due up to the last day of this month count,
`days` = days from today to that 1st.

**Items** (`subtract`, pure), checked in this order:
* doesn't leave the bank (`leaves_bank` false: a card-charged auto-debit, a series paid by card or in cash) → 0,
  note `card_or_cash`;
* card bill → what is still owed, `max(amount − paid_amount, 0)`; when nothing is left, 0 with note `paid`. The
  card bill's status is ignored (its "paid" means the minimum was reached, not the whole bill);
* status `upcoming` or `late` → the amount, no note;
* status `paid` or `not_seen` → 0, the status as its note (listed, not subtracted).
The app lists an item under "Not subtracted" exactly when its note is set.

**Cost**: `compute` = summarize 1 + salary_days 1 + list_upcoming 3–5 = 5–7 statements; the route adds the
balance setting: 6–8. Read-only, so no Origin check; the app loads it only while Settings is open.
"""
import datetime as dt
from calendar import monthrange
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, NamedTuple, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import limits
import pay_periods
import upcoming
from auth import current_user
from balance import _money, summarize
from database import get_db
from limits import IST
from models import BalanceSetting, User
from watchdog import SALARY_MIN, SALARY_WINDOW

PaydayState = Literal["upcoming", "today", "late", "month_end"]
Note = Literal["card_or_cash", "paid", "not_seen"]

DAY = dt.timedelta(days=1)
CENT = Decimal("0.01")


class Payday(NamedTuple):
    next_payday: dt.date
    state: PaydayState
    until: dt.date  # the last due date whose items are subtracted (inclusive)
    days: Optional[int]  # None for today / late


def payday(salary_days: Sequence[dt.date], today: dt.date) -> Payday:
    """The next payday, its state, the last due date counted and the days left (pure; see the module docstring)."""
    days = list(salary_days)
    if sum(day >= today - SALARY_WINDOW for day in days) >= SALARY_MIN:
        expected = pay_periods.expected_after(days, pay_periods.usual_offset(days))
        if expected > today:
            return Payday(expected, "upcoming", expected - DAY, (expected - today).days)
        if expected == today:
            return Payday(expected, "today", today, None)
        return Payday(expected, "late", today, None)
    first_of_next = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    last_of_this = today.replace(day=monthrange(today.year, today.month)[1])
    return Payday(first_of_next, "month_end", last_of_this, (first_of_next - today).days)


def subtract(item: upcoming.UpcomingItem) -> tuple[Decimal, Optional[Note]]:
    """What this item takes off Balance left, and why not when it takes nothing (pure)."""
    if not item.leaves_bank:
        return Decimal(0), "card_or_cash"
    if item.kind == upcoming.CARD_BILL:
        left = max(item.amount - item.paid_amount, Decimal(0))
        return (left, None) if left > 0 else (Decimal(0), "paid")
    if item.status in ("upcoming", "late"):
        return item.amount, None
    return Decimal(0), item.status


class SafeItem(BaseModel):
    date: dt.date
    payee: str
    kind: upcoming.Kind
    status: upcoming.Status
    amount: str  # "18000.00"
    min_amount: Optional[str]  # card bills that give one
    paid_amount: str  # "0.00" except card bills
    subtracted: str  # "0.00" when note says why not
    note: Optional[Note]


class SafeToSpend(BaseModel):
    currency: str
    balance: str  # Balance left
    next_payday: dt.date
    payday_state: PaydayState
    days: Optional[int]  # upcoming / month_end only, >= 1
    due: list[SafeItem]  # list_upcoming's order: date, card bill / auto-debit / expected, payee
    safe: str  # may be negative ("-3200.00")
    per_day: Optional[str]  # whole rupees ("1307.00"); only when safe > 0 and days is set


async def compute(db: AsyncSession, owner: User, setting: BalanceSetting, today: dt.date) -> SafeToSpend:
    """Safe to spend for `today` (IST). 5–7 statements."""
    summary = await summarize(db, owner, setting)
    balance = Decimal(str(summary.current)).quantize(CENT)  # a float: str() then quantize removes the noise
    when = payday(await pay_periods.salary_days(db, owner), today)
    due, total = [], Decimal(0)
    for item in await upcoming.list_upcoming(db, owner, today, when.until):
        amount, note = subtract(item)
        total += amount
        due.append(SafeItem(
            date=item.date, payee=item.payee, kind=item.kind, status=item.status, amount=_money(item.amount),
            min_amount=None if item.min_amount is None else _money(item.min_amount),
            paid_amount=_money(item.paid_amount), subtracted=_money(amount), note=note,
        ))
    safe = balance - total
    per_day = None
    if safe > 0 and when.state in ("upcoming", "month_end"):
        per_day = _money(safe // when.days)  # positive, so // floors to whole rupees
    return SafeToSpend(currency=setting.currency, balance=_money(balance), next_payday=when.next_payday,
                       payday_state=when.state, days=when.days, due=due, safe=_money(safe), per_day=per_day)


router = APIRouter(prefix="/api/v1/balance", tags=["Balance"])


@router.get("/safe-to-spend", response_model=SafeToSpend)
async def get_safe_to_spend(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Balance left minus what's still due from the bank before the next payday. 404 with no balance saved."""
    setting = await db.scalar(select(BalanceSetting).where(BalanceSetting.user_id == user.id))
    if setting is None:
        raise HTTPException(404, "No balance set.")
    today = limits.utc_now().astimezone(IST).date()
    return await compute(db, user, setting, today)
