"""Pay periods: months that start on payday ('Month starts on payday', setting periods.payday, default off).

A salary day is the IST date of a parsed INR credit in category Salary (hand-added rows count, since they're
parsed), yours or with no owner, that isn't a possible duplicate waiting for your decision.

Each salary day has a name, the month it pays for: its own month on the 1st–15th, the next month on the 16th or
later (the app's income rule, SALARY_NEXT_MONTH_FROM). For each name, the earliest salary day starts a pay period
with that name; later Salary credits with the same name (a bonus, a split salary) start nothing. When the
previous name has no period and that day is on or after the 1st of the named month, the period starts on the 1st
instead (that calendar month was already running under the same name). A period ends the day before the next one
starts, and no later than the 15th of the month after its name (its latest end). Nothing is predicted: while a
salary is late, the current period simply continues.

Days outside every pay period (before the first salary, after a latest end) count in their calendar month, cut to
those days. The open period (the newest, while today is in it) has an expected last day, the day before the same
date one month after its start, never before today and never after its end; it's used for display, "days left"
and the monthly limit window only, never to decide which period a payment belongs to.

This module is the one server copy of these rules (expense-tracker-client/lib/periods.ts is the app's). It has no
clock: `build` is pure and `monthly_window` takes the moment. `salary_days` doesn't look at the setting, so other
features can use it. limits.py imports this module, so this module never imports limits.

The usual payday (the SMS watchdog, doc 13; later doc 17) is one rule, also here: each salary day becomes its
distance from the nearest 1st of a month (`day_offset`: 31 Oct → −1, 2 Nov → 1), the usual payday is the lower
median of the last 3 (`usual_offset`, at least 2 needed), and the next expected salary is that offset from the
1st of the month after the newest salary's name (`expected_after`). These are pure: no clock, no settings, no
deduping of the days given.
"""
import statistics
from bisect import bisect_right
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import settings_store
from auth import current_user
from dashboard import ownership
from database import get_db
from models import Transaction, User
from spending import not_held_out

IST = ZoneInfo("Asia/Kolkata")
SALARY = "Salary"
SALARY_NEXT_MONTH_FROM = 16  # matches expense-tracker-client/lib/categories.ts
SETTING = "periods.payday"

Window = tuple[date, datetime, datetime]  # first IST day, UTC start, exclusive UTC end (as limits.period_window)


@dataclass(frozen=True)
class PayPeriod:
    month: str   # "YYYY-MM", the name
    start: date  # first IST day
    end: date    # last IST day, inclusive (for the newest period: its latest end)


def _utc(value: datetime) -> datetime:
    # SQLite drops the offset; values are always stored in UTC (as limits._utc).
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _to_utc(day: date) -> datetime:
    """The UTC moment an IST day starts."""
    return datetime.combine(day, time.min, IST).astimezone(timezone.utc)


def _shift(month: str, delta: int) -> str:
    year, number = int(month[:4]), int(month[5:7])
    index = year * 12 + number - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _first_of(month: str) -> date:
    return date(int(month[:4]), int(month[5:7]), 1)


def _month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def salary_month(day: date) -> str:
    """The month a salary on `day` pays for: its own on the 1st–15th, the next one on the 16th or later."""
    month = f"{day:%Y-%m}"
    return month if day.day < SALARY_NEXT_MONTH_FROM else _shift(month, 1)


def day_offset(day: date) -> int:
    """Days from the nearest 1st of a month: the 1st–15th → day − 1 (1st → 0); the 16th or later → minus the days
    left to the next 1st (31 Oct → −1, 30 Nov → −1, 28 Feb → −1, 25 Oct → −7). The 16th boundary is
    SALARY_NEXT_MONTH_FROM, so the sign agrees with salary_month."""
    if day.day < SALARY_NEXT_MONTH_FROM:
        return day.day - 1
    return day.day - monthrange(day.year, day.month)[1] - 1


def usual_offset(days: Sequence[date]) -> Optional[int]:
    """The usual payday as a day_offset: the lower median of the last 3 salary days (ascending, as salary_days
    gives them); None with fewer than 2."""
    recent = list(days)[-3:]
    if len(recent) < 2:
        return None
    return statistics.median_low([day_offset(day) for day in recent])


def payday_in(month: str, offset: int) -> date:
    """The payday for `month` ("YYYY-MM"): its 1st plus `offset` days (−1 is the last day of the month before)."""
    return _first_of(month) + timedelta(days=offset)


def expected_after(days: Sequence[date], offset: int) -> date:
    """The payday after the newest salary: payday_in(the month after salary_month(newest), offset).
    Newest 31 Oct (salary month November), offset −1 → 30 Nov."""
    return payday_in(_shift(salary_month(max(days)), 1), offset)


async def salary_days(db: AsyncSession, user: User) -> list[date]:
    """Your salary days (IST dates), distinct and ascending. One statement; independent of the setting."""
    times = await db.scalars(select(Transaction.transaction_time).where(
        ownership(user),
        Transaction.type == "CREDIT",
        Transaction.is_parsed.is_(True),
        Transaction.currency == "INR",
        Transaction.category == SALARY,
        not_held_out(),  # a duplicated salary SMS must not start anything
    ))
    return sorted({_utc(value).astimezone(IST).date() for value in times})


def build(days: Sequence[date]) -> list[PayPeriod]:
    """The pay periods for these salary days, oldest first (pure; order and repeats in `days` don't matter)."""
    earliest: dict[str, date] = {}
    for day in sorted(set(days)):
        earliest.setdefault(salary_month(day), day)
    names = sorted(earliest)
    starts = []
    for name in names:
        start = earliest[name]
        if _shift(name, -1) not in earliest and start >= _first_of(name):
            start = _first_of(name)  # that calendar month was already running under this name
        starts.append(start)
    periods = []
    for i, name in enumerate(names):
        latest_end = _first_of(_shift(name, 1)) + timedelta(days=14)  # the 15th of the month after the name
        end = latest_end if i + 1 == len(names) else min(starts[i + 1] - timedelta(days=1), latest_end)
        if end >= starts[i]:  # a guard; can't fail with valid input
            periods.append(PayPeriod(name, starts[i], end))
    return periods


def expected_last_day(start: date) -> date:
    """The day before the same date one month after `start`, clamped to that month's length
    (31 Oct → 29 Nov, 31 Jan 2027 → 27 Feb)."""
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    same_day = date(year, month, min(start.day, monthrange(year, month)[1]))
    return same_day - timedelta(days=1)


def span(periods: Sequence[PayPeriod], day: date) -> tuple[str, date, date]:
    """Name, first day and last day (inclusive) of the month `day` is listed under, with `day` as today: its pay
    period (the open one ends on its expected last day, never before `day`), or its calendar month cut to the
    days no pay period covers."""
    i = bisect_right([p.start for p in periods], day) - 1
    if i >= 0 and day <= periods[i].end:
        period = periods[i]
        last = period.end
        if i == len(periods) - 1:  # the open period
            last = min(period.end, max(expected_last_day(period.start), day))
        return period.month, period.start, last
    first, last = day.replace(day=1), _month_end(day)
    if i >= 0 and periods[i].end >= first:
        first = periods[i].end + timedelta(days=1)
    if i + 1 < len(periods) and periods[i + 1].start <= last:
        last = periods[i + 1].start - timedelta(days=1)
    return f"{day:%Y-%m}", first, last


def monthly_window(periods: Sequence[PayPeriod], moment: datetime) -> Window:
    """The monthly window holding `moment`, shaped like limits.period_window. With no periods it's the calendar
    month, exactly as period_window("monthly", moment)."""
    _, first, last = span(periods, moment.astimezone(IST).date())
    return first, _to_utc(first), _to_utc(last + timedelta(days=1))


def previous_window(periods: Sequence[PayPeriod], start: date) -> Optional[Window]:
    """The pay period ending the day before `start`, when `start` begins a pay period too; otherwise None."""
    if not any(p.start == start for p in periods):
        return None
    before = start - timedelta(days=1)
    period = next((p for p in periods if p.end == before), None)
    if period is None:
        return None
    return period.start, _to_utc(period.start), _to_utc(period.end + timedelta(days=1))


async def load(db: AsyncSession, user: User, enabled: Optional[bool] = None) -> Optional[list[PayPeriod]]:
    """Your pay periods, or None when the setting is off. `enabled` skips the settings read when the caller
    already has it. Off: 1 statement (0 with `enabled` given); on: 1 more, the salary query."""
    if enabled is None:
        enabled = await settings_store.get(db, user.id, SETTING)
    if not enabled:
        return None
    return build(await salary_days(db, user))


# ---------- API ----------

router = APIRouter(prefix="/api/v1/periods", tags=["Pay periods"])


class PayPeriodOut(BaseModel):
    month: str  # "YYYY-MM"
    start: date
    end: date  # inclusive; the newest period's is its latest end (the app works out the expected last day)


class PayPeriodsOut(BaseModel):
    enabled: bool
    periods: list[PayPeriodOut]


@router.get("", response_model=PayPeriodsOut)
async def read_periods(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Your pay periods, oldest first, back to the first salary; [] when the setting is off or no salary is
    found. Read-only, so no Origin check."""
    periods = await load(db, user)
    if periods is None:
        return PayPeriodsOut(enabled=False, periods=[])
    return PayPeriodsOut(enabled=True, periods=[PayPeriodOut(month=p.month, start=p.start, end=p.end) for p in periods])
