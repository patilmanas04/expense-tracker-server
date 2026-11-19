"""Weekly and monthly spend limits: overall ("All"), per category and per merchant. The first time
each is reached, an in-app notification is created (bell on Home), plus a Gmail alert if GMAIL_*
keys are configured (optional; retried for overall limits, best effort for category and merchant
limits).

Before a limit is reached, one in-app warning (kind spend_warning, no email) when spending is at or above
limits.warn_percent of the limit (settings_store; default 80, null = Off) and still below it. It goes out once per
limit, period, limit amount and level (keys spend_warn:…). The rule is state-based, so every check (new payment,
edit, AI retry, merge, Not a duplicate) gives the same answer.

"Spent" matches the app: parsed debits in the limit's currency, card spends included, except categories that aren't spending (spending.py: Credit Card Payment, Self Transfer, Investments & Finance), and not possible duplicates waiting for your decision (duplicates.py).
Each payment counts minus its lend & borrow entries (ledger.py; spending.counted_amount): your ₹600 share of a ₹1,800
dinner you split counts ₹600.
Weeks run Monday to Sunday (IST). Months follow the calendar, or pay periods when 'Month starts on payday' is on
(pay_periods.py; current_windows).
Which payments a merchant limit counts is decided in merchants.py.

Rollover (limit_rollovers, PUT /rollover): a monthly limit, overall or per category, can roll over. Its limit then
adds what you didn't spend of last month's limit, or takes off what you overspent, clamped between ₹0 and twice the
limit: rollover_limit(base, previous) = base + max(−base, min(base, base − previous)). "Last month" is the previous
monthly window (the previous pay period with 'Month starts on payday' on; no carry when there is none), and carry
starts with the first month that begins after the IST day it was turned on. The summary shows the rollover limit
(with `base` and `carried`); alerts compare spending with it, but their keys (and SpendAlert.limit_amount) use the
base limit, so a late SMS for last month that changes the carry never sends the same alert twice. Weekly and
merchant limits never roll over.

The checks run after each saved payment through post_save.py (post_save_check, registered in main.py for every
cause); check_spend_limits runs them alone, for rules.py's merges and rule applies.
"""
import html
import logging
import unicodedata
import uuid
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import String, and_, delete, exists, func, literal, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import categories
import mailer
import merchants
import pay_periods
import post_save
from auth import FRONTEND_URL, current_user, same_origin
from dashboard import ownership
from database import AsyncSessionLocal, get_db
from models import (CategoryLimit, KnownMerchant, LimitRollover, MerchantCategory, MerchantLimit, Notification,
                    SpendAlert, SpendLimit, Transaction, User)
from notifications import create_once
from parser import CATEGORIES, UNCATEGORIZED
from post_save import PostSave
from spending import EXCLUDED_FROM_SPENDING, counted_amount, counted_filter, counts_as_spending, linked_totals

router = APIRouter(prefix="/api/v1/limits", tags=["Spend limits"])
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
PERIODS = ("weekly", "monthly")
MAX_ATTEMPTS = 3  # a failed email is retried on the next payments, up to this many tries
MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14, 2)
MAX_MERCHANT_LENGTH = 255  # String(255)
LABEL_LENGTH = 120  # a merchant's name in a notification title, which holds 200 characters
ALL = "All"  # the overall limit, across every category
SCOPE_MESSAGE = "Pick a spending category, or All."
# Limits apply to spending, so money-in categories, Uncategorized and categories that aren't spending
# (spending.py) can't have one. Your own categories (categories.py) can.
NOT_LIMITABLE = frozenset({UNCATEGORIZED, "Salary", "Income", "Refunds & Cashback"}) | EXCLUDED_FROM_SPENDING
LIMIT_CATEGORIES = [c for c in CATEGORIES if c not in NOT_LIMITABLE]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    # SQLite drops the offset; values are always stored in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def period_window(period: str, moment: datetime) -> tuple[date, datetime, datetime]:
    """First IST day, UTC start and exclusive UTC end of the week (Mon–Sun) or month containing `moment`."""
    day = moment.astimezone(IST).date()
    if period == "weekly":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    else:
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)

    def to_utc(d: date) -> datetime:
        return datetime.combine(d, time.min, IST).astimezone(timezone.utc)

    return start, to_utc(start), to_utc(end)


Window = tuple[date, datetime, datetime]  # first IST day, UTC start, exclusive UTC end


async def current_windows(db: AsyncSession, user: User, now: datetime,
                          payday: Optional[bool] = None) -> dict[str, Optional[Window]]:
    """The weekly and monthly windows for `now`, computed once per entry point and passed down. Monthly is the pay
    period (or cut calendar month) when 'Month starts on payday' is on; then "previous_monthly" is set too: the pay
    period ending the day before, or None. With the setting off that key is absent (callers that need it check
    `"previous_monthly" in windows`), so loops walk PERIODS, never .values(). `payday` skips the settings read
    when the caller already has it."""
    windows: dict[str, Optional[Window]] = {period: period_window(period, now) for period in PERIODS}
    periods = await pay_periods.load(db, user, payday)
    if periods is not None:
        windows["monthly"] = pay_periods.monthly_window(periods, now)
        windows["previous_monthly"] = pay_periods.previous_window(periods, windows["monthly"][0])
    return windows


def _last_day(end_utc: datetime) -> date:
    return (end_utc.astimezone(IST) - timedelta(days=1)).date()


def format_money(amount: Decimal, currency: str) -> str:
    """₹1,23,456.50 with Indian digit grouping for INR; "USD 12.50" otherwise."""
    value = Decimal(amount).quantize(Decimal("0.01"))
    whole, fraction = f"{abs(value):.2f}".split(".")
    if currency == "INR" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head, *groups, tail] if head else [*groups, tail])
    elif currency != "INR":
        whole = f"{int(whole):,}"
    sign = "-" if value < 0 else ""
    return f"{sign}₹{whole}.{fraction}" if currency == "INR" else f"{sign}{currency} {whole}.{fraction}"


def _spent_query(user: User, currency: str, start: datetime, end: datetime, category: Optional[str] = None):
    linked = linked_totals()  # each payment counts minus its lend & borrow entries
    query = select(func.coalesce(func.sum(counted_amount(linked)), 0)).select_from(Transaction).outerjoin(
        linked, linked.c.transaction_id == Transaction.id
    ).where(
        ownership(user),
        Transaction.is_parsed.is_(True),
        Transaction.type == "DEBIT",
        Transaction.currency == currency,
        Transaction.transaction_time >= start,
        Transaction.transaction_time < end,
        counted_filter(),
    )
    return query.where(Transaction.category == category) if category else query


def _money(total) -> Decimal:
    return Decimal(str(total)).quantize(Decimal("0.01"))


async def spent_between(db: AsyncSession, user: User, currency: str, start: datetime, end: datetime,
                        category: Optional[str] = None) -> Decimal:
    return _money(await db.scalar(_spent_query(user, currency, start, end, category)))


async def _setting(db: AsyncSession, user_id: uuid.UUID) -> Optional[SpendLimit]:
    return await db.scalar(select(SpendLimit).where(SpendLimit.user_id == user_id))


def _notification_key(period: str, start: date, limit: Decimal, category: Optional[str] = None) -> str:
    # The limit is part of the key, so raising a limit mid-period notifies again.
    scope = f"{category}:" if category else ""
    return f"spend_limit:{scope}{period}:{start.isoformat()}:{Decimal(limit):.2f}"


def _merchant_notification_key(limit_id: uuid.UUID, period: str, start: date, limit: Decimal) -> str:
    # The limit's id, not its name, keeps "Swiggy" and "SWIGGY" apart under any collation.
    return f"spend_limit:merchant:{limit_id}:{period}:{start.isoformat()}:{Decimal(limit):.2f}"


def _warning_key(period: str, start: date, limit: Decimal, warn_percent: int, category: Optional[str] = None) -> str:
    # Like _notification_key, plus the level: changing the limit or the level allows a new warning.
    scope = f"{category}:" if category else ""
    return f"spend_warn:{scope}{period}:{start.isoformat()}:{Decimal(limit):.2f}:{warn_percent}"


def _merchant_warning_key(limit_id: uuid.UUID, period: str, start: date, limit: Decimal, warn_percent: int) -> str:
    return f"spend_warn:merchant:{limit_id}:{period}:{start.isoformat()}:{Decimal(limit):.2f}:{warn_percent}"


def _warns(spent: Decimal, limit: Decimal, warn_percent: Optional[int]) -> bool:
    """At or above the warning level (callers check the limit first). Exact in Decimal, no division."""
    return warn_percent is not None and limit > 0 and spent * 100 >= limit * warn_percent


def _reached(spent: Decimal, limit: Decimal) -> bool:
    # A rollover limit can be ₹0: nothing spent hasn't reached it.
    return spent >= limit and spent > 0


def _percent(spent: Decimal, limit: Decimal) -> int:
    """Spent as a whole percent of the limit; 100 for a ₹0 (rollover) limit."""
    return int(spent * 100 / limit) if limit else 100


def rollover_limit(base: Decimal, previous_spent: Decimal) -> Decimal:
    """This month's limit when it rolls over: the base plus last month's unspent amount, or minus its overspend,
    between ₹0 and twice the base."""
    return base + max(-base, min(base, base - previous_spent))


def previous_monthly_window(windows: dict[str, Optional[Window]]) -> Optional[Window]:
    """Last month's window: the previous pay period when 'Month starts on payday' is on (None when there is none),
    else the calendar month before."""
    if "previous_monthly" in windows:
        return windows["previous_monthly"]
    return period_window("monthly", windows["monthly"][1] - timedelta(seconds=1))


def carry_applies(monthly_limit: Optional[Decimal], rollover_since: Optional[datetime],
                  windows: dict[str, Optional[Window]]) -> bool:
    """Whether this month's limit carries last month's: rollover is on, there is a monthly limit, the month began
    after the IST day rollover was turned on, and there is a previous month."""
    if monthly_limit is None or rollover_since is None:
        return False
    if _utc(rollover_since).astimezone(IST).date() >= windows["monthly"][0]:
        return False
    return previous_monthly_window(windows) is not None


def merchant_debits(user: User, currency: str, since: Optional[datetime] = None):
    """(merchant, amount, time) of every payment a merchant limit can count. The summary, the
    alert check, known merchants and the preview all read through this, so they always agree.
    Only counted spending (spending.counted_filter), so a merchant paid only in excluded categories isn't
    known and its limit shows ₹0, and a possible duplicate waiting for your decision isn't counted. The amount is
    the counted amount: the payment minus its lend & borrow entries (ledger.py)."""
    linked = linked_totals()  # each payment counts minus its lend & borrow entries
    query = select(
        Transaction.merchant, counted_amount(linked).label("amount"), Transaction.transaction_time
    ).select_from(Transaction).outerjoin(linked, linked.c.transaction_id == Transaction.id).where(
        ownership(user),
        Transaction.is_parsed.is_(True),
        Transaction.type == "DEBIT",
        Transaction.currency == currency,
        counted_filter(),
    )
    return query if since is None else query.where(Transaction.transaction_time >= since)


async def _merchant_rows(db: AsyncSession, user: User, currency: str,
                         since: Optional[datetime] = None) -> list[tuple[Optional[str], Decimal, datetime]]:
    rows = (await db.execute(merchant_debits(user, currency, since))).all()
    return [(merchant, amount, _utc(when)) for merchant, amount, when in rows]


async def load_merchant_limits(db: AsyncSession, user_id: uuid.UUID) -> list[MerchantLimit]:
    """Your merchant limits, oldest first (the order they're listed in)."""
    return list(await db.scalars(
        select(MerchantLimit).where(MerchantLimit.user_id == user_id).order_by(MerchantLimit.created_at, MerchantLimit.id)
    ))


def _like_words(name: str) -> str:
    """A LIKE pattern (escaped with "/") for any text holding the cleaned name's words in order,
    with anything between them: "Amazon Pay" finds a stored "Amazon  Pay" too."""
    escaped = (word.replace("/", "//").replace("%", "/%").replace("_", "/_") for word in name.split(" "))
    return "%" + "%".join(escaped) + "%"


async def _is_known(db: AsyncSession, user: User, name: str) -> bool:
    """Whether you've paid a merchant with this cleaned name, typed it before, or have a limit on
    it. `name` must be cleaned. The SQL only narrows the rows (a name that's the same once cleaned
    always passes it, on any database); the exact, case-sensitive comparison of cleaned names
    happens here, so no collation can change the answer."""
    # Stored names aren't normalised (hand-entered or AI-read ones can hold decomposed accents), so
    # the NFD spelling of the name is looked for too.
    patterns = [func.lower(_like_words(form)) for form in {name, unicodedata.normalize("NFD", name)}]

    def narrowed(column):
        return or_(*(func.lower(column).like(pattern, escape="/") for pattern in patterns))

    paid = merchant_debits(user, "INR").with_only_columns(Transaction.merchant.label("name")).where(narrowed(Transaction.merchant))
    typed = select(KnownMerchant.name.label("name")).where(KnownMerchant.user_id == user.id, narrowed(KnownMerchant.name))
    limited = select(MerchantLimit.merchant.label("name")).where(
        MerchantLimit.user_id == user.id, MerchantLimit.merchant_hash == merchants.name_hash(name)
    )
    found = (await db.scalars(union_all(paid, typed, limited))).all()
    return name in {merchants.clean(found_name) for found_name in found if found_name}


@dataclass(frozen=True)
class Rule:
    """A limit you've set: overall (category "All") or for one category."""

    category: str
    currency: str
    weekly_limit: Optional[Decimal]
    monthly_limit: Optional[Decimal]
    rollover_since: Optional[datetime] = None  # when rollover was turned on; None = off


async def load_rules(db: AsyncSession, user_id: uuid.UUID) -> list[Rule]:
    """The overall limit and every category limit, with their rollover rows, in one query. Overall first, then the
    built-ins in list order, then your own categories by name.
    Category limits on categories that can no longer have one (left from before spending.py) are
    skipped; scripts/recategorise_card_payments.py deletes them."""
    overall = select(
        literal(ALL, String).label("category"), SpendLimit.currency, SpendLimit.weekly_limit, SpendLimit.monthly_limit,
        LimitRollover.enabled_at,
    ).select_from(SpendLimit).outerjoin(
        LimitRollover, and_(LimitRollover.user_id == SpendLimit.user_id, LimitRollover.category == ALL)
    ).where(SpendLimit.user_id == user_id)
    per_category = select(
        CategoryLimit.category, CategoryLimit.currency, CategoryLimit.weekly_limit, CategoryLimit.monthly_limit,
        LimitRollover.enabled_at,
    ).select_from(CategoryLimit).outerjoin(
        LimitRollover, and_(LimitRollover.user_id == CategoryLimit.user_id, LimitRollover.category == CategoryLimit.category)
    ).where(CategoryLimit.user_id == user_id, CategoryLimit.category.not_in(sorted(NOT_LIMITABLE)))
    rows = (await db.execute(union_all(overall, per_category))).all()
    rules = [Rule(*row[:4], _utc(row[4]) if row[4] is not None else None) for row in rows]
    order = {name: i for i, name in enumerate([ALL, *LIMIT_CATEGORIES])}
    # Built-ins in list order, then your own categories by name.
    return sorted(rules, key=lambda rule: (order.get(rule.category, len(order)), rule.category.casefold(), rule.category))


def _window_text(start: date, end_utc: datetime) -> str:
    last = _last_day(end_utc)
    return f"{start:%a} {start.day} {start:%b} – {last:%a} {last.day} {last:%b}"


def _alert_key(user_id: uuid.UUID, period: str, start: date, limit: Decimal):
    return (
        SpendAlert.user_id == user_id,
        SpendAlert.period == period,
        SpendAlert.period_start == start,
        SpendAlert.limit_amount == limit,
    )


# ---------- API ----------

def _limit_field():
    return Field(default=None, gt=0, le=MAX_AMOUNT, max_digits=14, decimal_places=2)


def _encodable(data):
    """A 422 echoes the values it rejects, and a lone surrogate can't be encoded in the response;
    so invalid characters in any text field become U+FFFD (itself invalid) before validation."""
    if not isinstance(data, dict):
        return data
    return {key: merchants.mark_invalid_characters(value) if isinstance(value, str) else value for key, value in data.items()}


def _limit_scope(value: Optional[str]) -> Optional[str]:
    """None for the overall limit (empty or "All"), else the name as typed (trimmed); the routes check it with
    limit_category, which knows your own categories."""
    value = (value or "").strip()
    return None if not value or value.casefold() == ALL.casefold() else value


async def limit_category(db: AsyncSession, user: User, value: str, message: str) -> str:
    """A category that can have a limit, in its stored spelling: a spending built-in or one of yours; 422 with
    `message` otherwise."""
    category = categories.canonical(value, await categories.names_for(db, user))
    if category is None or category in NOT_LIMITABLE:
        raise HTTPException(422, message)
    return category


class LimitsInput(BaseModel):
    category: Optional[str] = Field(default=None, description='A spending category; omit or "All" for overall spending')
    weekly_limit: Optional[Decimal] = _limit_field()  # null = no limit
    monthly_limit: Optional[Decimal] = _limit_field()

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)

    @field_validator("category")
    @classmethod
    def _category(cls, value: Optional[str]) -> Optional[str]:
        return _limit_scope(value)


class MerchantLimitInput(BaseModel):
    id: Optional[uuid.UUID] = Field(default=None, description="The limit being changed or removed, when editing one")
    merchant: str = Field(description="Exactly as picked or typed; case matters")
    weekly_limit: Optional[Decimal] = _limit_field()  # both null = remove the limit
    monthly_limit: Optional[Decimal] = _limit_field()
    category: Optional[str] = Field(default=None, description="Only for a merchant you haven't paid or typed before")

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)  # `_merchant` then rejects them

    @field_validator("merchant")
    @classmethod
    def _merchant(cls, value: str) -> str:
        if merchants.has_invalid_characters(value):
            raise ValueError("Merchant names can't contain invalid characters.")
        value = merchants.clean(value)
        if not merchants.words(value):
            raise ValueError("Enter a merchant name.")
        if len(value) > MAX_MERCHANT_LENGTH:
            raise ValueError(f"Merchant names can be up to {MAX_MERCHANT_LENGTH} characters.")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: Optional[str]) -> Optional[str]:
        # Spending categories only (checked in the route): Uncategorized would make merchant memory forget it.
        return (value or "").strip() or None


class RolloverInput(BaseModel):
    category: Optional[str] = Field(default=None, description='A spending category; omit or "All" for the overall limit')
    enabled: bool

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)

    @field_validator("category")
    @classmethod
    def _category(cls, value: Optional[str]) -> Optional[str]:
        return _limit_scope(value)


class PeriodStatus(BaseModel):
    limit: Optional[float]  # the rollover limit when carry applies (may be 0.0)
    spent: float
    start: date
    end: date  # last day, inclusive
    alerted: bool  # notification created for this period and limit
    base: Optional[float] = None  # monthly with carry only: the limit as set
    carried: Optional[float] = None  # monthly with carry only: limit − base (negative after an overspent month)


class EmailStatus(BaseModel):
    configured: bool
    to: Optional[str]
    last_error: Optional[str]


class CategoryStatus(BaseModel):
    category: str
    weekly: PeriodStatus
    monthly: PeriodStatus
    rollover: bool = False  # rollover is on for this category's monthly limit


class MerchantLimitStatus(BaseModel):
    id: uuid.UUID
    merchant: str
    counts: list[str]  # names this limit counts, all time, most payments first; [] before any payment
    weekly: PeriodStatus
    monthly: PeriodStatus


class LimitsSummary(BaseModel):
    currency: str
    weekly: PeriodStatus  # overall
    monthly: PeriodStatus
    categories: list[CategoryStatus] = []  # only categories with a limit
    merchants: list[MerchantLimitStatus] = []  # oldest first
    email: EmailStatus
    rollover: bool = False  # rollover is on for the overall monthly limit


class KnownMerchantOut(BaseModel):
    name: str
    payments: int  # 0 for a merchant you typed but haven't paid


class MerchantPreview(BaseModel):
    known: bool  # False: the sheet asks for the new merchant's category
    counts: list[str]


async def summarize(db: AsyncSession, user: User, rules: list[Rule],
                    merchant_limits: Sequence[MerchantLimit] = ()) -> LimitsSummary:
    """Every meter, overall and per category, in a single query: the database may be a continent
    away, so each round trip costs far more than the work itself. Merchant limits add their
    "notified" flags to that query and one read of your debits, matched here in Python. A monthly
    limit that rolls over adds last month's spending to the same query."""
    now = utc_now()
    overall = next((rule for rule in rules if rule.category == ALL), None)
    per_category = [rule for rule in rules if rule.category != ALL]
    currency = overall.currency if overall else "INR"
    windows = await current_windows(db, user, now)
    previous = previous_monthly_window(windows)

    def amounts(rule: Optional[Rule]) -> dict[str, Optional[Decimal]]:
        return {period: getattr(rule, f"{period}_limit") if rule else None for period in PERIODS}

    def carries(rule: Optional[Rule]) -> bool:
        return rule is not None and carry_applies(rule.monthly_limit, rule.rollover_since, windows)

    # (column prefix, category or None for overall, currency, limits, rule)
    meters = [("all", None, currency, amounts(overall), overall)]
    meters += [(f"c{i}", rule.category, rule.currency, amounts(rule), rule) for i, rule in enumerate(per_category)]
    columns = []
    for prefix, category, meter_currency, limits, rule in meters:
        for period in PERIODS:
            start_date, start, end = windows[period]
            limit = limits[period]
            columns.append(_spent_query(user, meter_currency, start, end, category).scalar_subquery().label(f"{prefix}_{period}_spent"))
            if not limit:
                continue
            columns.append(exists().where(
                Notification.user_id == user.id,
                Notification.dedupe_key == _notification_key(period, start_date, limit, category),
            ).label(f"{prefix}_{period}_alerted"))
            if category is None:  # emails for overall limits are tracked (and retried)
                columns.append(select(SpendAlert.error).where(
                    *_alert_key(user.id, period, start_date, limit), SpendAlert.status == "failed",
                ).scalar_subquery().label(f"{prefix}_{period}_error"))
        if carries(rule):
            _, prev_start, prev_end = previous
            columns.append(_spent_query(user, meter_currency, prev_start, prev_end, category).scalar_subquery()
                           .label(f"{prefix}_monthly_prev"))
    for i, rule in enumerate(merchant_limits):
        for period, limit in amounts(rule).items():
            if limit:
                columns.append(exists().where(
                    Notification.user_id == user.id,
                    Notification.dedupe_key == _merchant_notification_key(rule.id, period, windows[period][0], limit),
                ).label(f"m{i}_{period}_alerted"))
    row = (await db.execute(select(*columns))).one()._mapping

    def period_status(period: str, limit: Optional[Decimal], spent, alerted, previous_spent=None) -> PeriodStatus:
        """`previous_spent` (monthly with carry only): the shown limit is the rollover limit; `alerted` stays keyed
        on the base limit, as the alerts are."""
        start_date, _, end = windows[period]
        shown, base, carried = limit, None, None
        if limit is not None and previous_spent is not None:
            shown = rollover_limit(limit, _money(previous_spent))
            base, carried = float(limit), float(shown - limit)
        return PeriodStatus(
            limit=float(shown) if shown is not None else None,
            spent=float(_money(spent)),
            start=start_date,
            end=_last_day(end),
            alerted=bool(limit) and bool(alerted),
            base=base,
            carried=carried,
        )

    def statuses(prefix: str, limits: dict[str, Optional[Decimal]], rule: Optional[Rule]) -> dict[str, PeriodStatus]:
        previous_spent = row[f"{prefix}_monthly_prev"] if carries(rule) else None
        return {period: period_status(period, limits[period], row[f"{prefix}_{period}_spent"],
                                      limits[period] and row[f"{prefix}_{period}_alerted"],
                                      previous_spent if period == "monthly" else None)
                for period in PERIODS}

    merchant_statuses = []
    if merchant_limits:
        rows = await _merchant_rows(db, user, "INR")
        spans = {period: windows[period][1:] for period in PERIODS}
        for i, rule in enumerate(merchant_limits):
            limits = amounts(rule)
            weekly, monthly, counts = merchants.meter(rule.merchant, rows, spans)
            status = {period: period_status(period, limits[period], spent, limits[period] and row[f"m{i}_{period}_alerted"])
                      for period, spent in (("weekly", weekly), ("monthly", monthly))}
            merchant_statuses.append(MerchantLimitStatus(
                id=rule.id, merchant=rule.merchant, counts=counts, weekly=status["weekly"], monthly=status["monthly"],
            ))

    overall_limits = meters[0][3]
    # The monthly failure wins over the weekly one, as before.
    last_error = next((row[f"all_{period}_error"] for period in reversed(PERIODS)
                       if overall_limits[period] and row[f"all_{period}_error"] is not None), None)
    overall_status = statuses("all", overall_limits, overall)
    categories = []
    for prefix, category, _, limits, rule in meters[1:]:
        status = statuses(prefix, limits, rule)
        categories.append(CategoryStatus(category=category, weekly=status["weekly"], monthly=status["monthly"],
                                         rollover=rule.rollover_since is not None))
    return LimitsSummary(
        currency=currency,
        weekly=overall_status["weekly"],
        monthly=overall_status["monthly"],
        categories=categories,
        merchants=merchant_statuses,
        email=EmailStatus(configured=mailer.configured(), to=mailer.recipient() or None, last_error=last_error),
        rollover=overall is not None and overall.rollover_since is not None,
    )


async def _summary(db: AsyncSession, user: User) -> LimitsSummary:
    # Merchant limits are loaded before the meters, so their "notified" keys can join that query.
    rules = await load_rules(db, user.id)
    return await summarize(db, user, rules, await load_merchant_limits(db, user.id))


@router.get("", response_model=LimitsSummary)
async def get_limits(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _summary(db, user)


@router.put("", response_model=LimitsSummary, dependencies=[Depends(same_origin)])
async def set_limits(body: LimitsInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Sets the overall limit, or one category's when `category` is given. Leaving both amounts
    empty removes a category's limit. Removing a limit, or its monthly amount, turns its rollover off."""
    category = await limit_category(db, user, body.category, SCOPE_MESSAGE) if body.category else None
    rollover_row = delete(LimitRollover).where(LimitRollover.user_id == user.id, LimitRollover.category == (category or ALL))
    if category and body.weekly_limit is None and body.monthly_limit is None:
        await db.execute(delete(CategoryLimit).where(CategoryLimit.user_id == user.id, CategoryLimit.category == category))
        await db.execute(rollover_row)
        await db.commit()
        return await _summary(db, user)

    async def save() -> None:
        if category:
            rule = await db.scalar(select(CategoryLimit).where(CategoryLimit.user_id == user.id, CategoryLimit.category == category))
            if rule is None:
                rule = CategoryLimit(user_id=user.id, category=category, currency="INR")
                db.add(rule)
            rule.updated_at = utc_now()
        else:
            rule = await _setting(db, user.id)
            if rule is None:
                rule = SpendLimit(user_id=user.id, currency="INR")
                db.add(rule)
        rule.weekly_limit = body.weekly_limit
        rule.monthly_limit = body.monthly_limit
        if body.monthly_limit is None:
            await db.execute(rollover_row)  # nothing left to roll over
        await db.commit()

    try:
        await save()
    except IntegrityError:
        await db.rollback()  # two saves raced to create the row; update the winner
        await save()
    return await _summary(db, user)


@router.put("/rollover", response_model=LimitsSummary, dependencies=[Depends(same_origin)])
async def set_rollover(body: RolloverInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Turns rollover on or off for the overall monthly limit, or a category's. Turning it on needs a monthly
    limit; when it is already on, the first `enabled_at` is kept. Turning it off always succeeds."""
    user_id = user.id  # read now: the rollback below expires `user`, which may share this session
    category = await limit_category(db, user, body.category, SCOPE_MESSAGE) if body.category else None
    scope = category or ALL
    where = (LimitRollover.user_id == user_id, LimitRollover.category == scope)
    if not body.enabled:
        await db.execute(delete(LimitRollover).where(*where))
        await db.commit()
        return await _summary(db, user)
    if category:
        monthly = await db.scalar(select(CategoryLimit.monthly_limit).where(
            CategoryLimit.user_id == user_id, CategoryLimit.category == category))
    else:
        monthly = await db.scalar(select(SpendLimit.monthly_limit).where(SpendLimit.user_id == user_id))
    if monthly is None:
        raise HTTPException(422, "Set a monthly limit first.")
    if await db.scalar(select(LimitRollover.id).where(*where)) is None:
        db.add(LimitRollover(user_id=user_id, category=scope, enabled_at=utc_now()))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # turned on twice at once: the first row stays
            user = await db.get(User, user_id) or user
    return await _summary(db, user)


@router.put("/merchants", response_model=LimitsSummary, dependencies=[Depends(same_origin)])
async def set_merchant_limit(body: MerchantLimitInput, user: User = Depends(current_user),
                             db: AsyncSession = Depends(get_db)):
    """Sets the limit for one merchant name, exactly as given. Leaving both amounts empty removes
    it. A merchant you've never paid or typed needs a `category`: the name becomes a known
    merchant, and its category goes into merchant memory unless memory already has one for it."""
    # A local import, as before: categorize used to import this module at its top.
    import categorize

    name, key = body.merchant, merchants.name_hash(body.merchant)
    clear = body.weekly_limit is None and body.monthly_limit is None
    category = await limit_category(db, user, body.category, "Pick a spending category.") if body.category else None

    async def save() -> None:
        if body.id is not None:
            # Editing a limit: if another device deleted it meanwhile, say so rather than re-create it.
            rule = await db.scalar(select(MerchantLimit).where(MerchantLimit.user_id == user.id, MerchantLimit.id == body.id))
            if rule is None:
                if clear:
                    return  # already gone
                raise HTTPException(404, "This limit was deleted.")
        else:
            rule = await db.scalar(select(MerchantLimit).where(MerchantLimit.user_id == user.id, MerchantLimit.merchant_hash == key))
        if rule is not None:
            # No "known" check: the name may no longer appear in any payment.
            if clear:
                await db.delete(rule)
            else:
                rule.weekly_limit, rule.monthly_limit, rule.updated_at = body.weekly_limit, body.monthly_limit, utc_now()
            await db.commit()
            return
        if clear:
            return  # nothing to remove
        if not await _is_known(db, user, name):
            if category is None:
                raise HTTPException(422, "Pick a category for this new merchant.")
            db.add(KnownMerchant(user_id=user.id, name=name, name_hash=key))
            # Memory ignores case, so a typed "swiggy" mustn't change the category "Swiggy" already has.
            remembered = await db.scalar(select(MerchantCategory.id).where(
                MerchantCategory.user_id == user.id,
                MerchantCategory.merchant_key == categorize.merchant_key(name),
                MerchantCategory.type == "DEBIT",
            ))
            if remembered is None:
                await categorize.remember(db, user.id, name, "DEBIT", category)
        db.add(MerchantLimit(user_id=user.id, merchant=name, merchant_hash=key, currency="INR",
                             weekly_limit=body.weekly_limit, monthly_limit=body.monthly_limit))
        await db.commit()

    try:
        await save()
    except IntegrityError:
        await db.rollback()  # two saves raced to create the limit; this run updates the winner
        await save()
    return await _summary(db, user)


@router.get("/merchants/known", response_model=list[KnownMerchantOut])
async def list_known_merchants(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Merchants to pick from: every name you've paid ("Swiggy" and "SWIGGY" are two) and every one
    you typed, most payments first. Names are made distinct here, not in SQL, so case is kept on
    every database."""
    paid = merchant_debits(user, "INR").with_only_columns(Transaction.merchant.label("name"), literal(1).label("paid"))
    typed = select(KnownMerchant.name.label("name"), literal(0).label("paid")).where(KnownMerchant.user_id == user.id)
    rows = (await db.execute(union_all(paid, typed))).all()
    names = merchants.known_names([row for row in rows if row.paid], [row.name for row in rows if not row.paid])
    return [KnownMerchantOut(name=name, payments=payments) for name, payments in names]


@router.get("/merchants/preview", response_model=MerchantPreview)
async def preview_merchant(merchant: str = "", user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """For the limit sheet as you type: whether the name is known (if not, the sheet asks for its
    category) and the names a limit on it would count. Never an error; a name that can't have a
    limit just gives nothing."""
    if merchants.has_invalid_characters(merchant):
        return MerchantPreview(known=False, counts=[])
    name = merchants.clean(merchant)
    if not merchants.words(name) or len(name) > MAX_MERCHANT_LENGTH:
        return MerchantPreview(known=False, counts=[])
    known = await _is_known(db, user, name)
    windows = await current_windows(db, user, utc_now())
    spans = {period: windows[period][1:] for period in PERIODS}
    _, _, counts = merchants.meter(name, await _merchant_rows(db, user, "INR"), spans)
    return MerchantPreview(known=known, counts=counts)


@router.post("/test-email", dependencies=[Depends(same_origin)])
async def send_test_email(user: User = Depends(current_user)):
    to = mailer.recipient()
    text = (
        "This is a test from Folio. Spend-limit alerts will arrive at this address.\n\n"
        f"Open Folio: {FRONTEND_URL}\n"
    )
    try:
        await mailer.send_email(to, "Folio test email: alerts are working", text, _html_email(
            "Alerts are working", "<p>This is a test from Folio. Spend-limit alerts will arrive at this address.</p>"
        ))
    except mailer.MailNotConfigured as error:
        raise HTTPException(503, str(error))
    except mailer.MailError as error:
        raise HTTPException(502, str(error))
    return {"status": "sent", "to": to}


# ---------- Alerts (registered with post_save: run after each saved payment) ----------

def _html_email(heading: str, body: str) -> str:
    return (
        '<div style="font-family:-apple-system,Segoe UI,Inter,Arial,sans-serif;max-width:520px;margin:0 auto;color:#111113">'
        '<div style="background:linear-gradient(160deg,#8653f3,#b796fb);border-radius:20px;padding:24px;color:#fff">'
        f'<p style="margin:0;font-size:14px;opacity:.85">Folio</p><h1 style="margin:6px 0 0;font-size:22px">{heading}</h1></div>'
        f'<div style="padding:20px 4px;font-size:15px;line-height:1.55">{body}'
        f'<p style="margin-top:20px"><a href="{html.escape(FRONTEND_URL)}" style="background:#141211;color:#fff;'
        'padding:10px 18px;border-radius:999px;text-decoration:none;font-weight:600">Open Folio</a></p></div></div>'
    )


def _limit_name(period: str, scope: Optional[str]) -> str:
    """ "Weekly spend limit" overall, "Weekly Food & Dining limit" for a category, "Weekly Swiggy
    limit" for a merchant."""
    label = "Weekly" if period == "weekly" else "Monthly"
    return f"{label} {scope or 'spend'} limit"


def _short_label(name: str) -> str:
    # Notification titles hold 200 characters and a merchant name can have 255; the body keeps it whole.
    return name if len(name) <= LABEL_LENGTH else name[:LABEL_LENGTH - 1] + "…"


def compose_alert(period: str, limit: Decimal, spent: Decimal, start: date, end_utc: datetime,
                  txn: Transaction, currency: str, category: Optional[str] = None, *,
                  label: Optional[str] = None) -> tuple[str, str, str]:
    """Subject, plain text and HTML for a limit alert. `label` is a merchant limit's full name."""
    name = _limit_name(period, label or category)
    on = f" at {label}" if label else f" on {category}" if category else ""
    # A merchant's name keeps its case ("Swiggy" and "SWIGGY" are different limits).
    of_limit = f"{period} {label} limit" if label else name.lower()
    noun = "week" if period == "weekly" else "month"
    state = "reached" if spent == limit else "crossed"
    percent = _percent(spent, limit)
    window = _window_text(start, end_utc)
    when = _utc(txn.transaction_time).astimezone(IST)
    latest_where = txn.merchant or txn.category or "a payment"
    account = f" ({txn.account_ref})" if txn.account_ref else ""
    clock = f"{when:%I:%M}".lstrip("0") + (" am" if when.hour < 12 else " pm")
    latest = f"{format_money(txn.amount, txn.currency)} at {latest_where}{account}, {when.day} {when:%b}, {clock}"
    spent_text, limit_text = format_money(spent, currency), format_money(limit, currency)

    subject = f"{name} {state}: {spent_text} of {limit_text}"
    text = (
        f"You've spent {spent_text}{on} this {noun} ({window}), {percent}% of your {limit_text} {of_limit}.\n\n"
        f"Latest payment: {latest}.\n\n"
        f"Open Folio: {FRONTEND_URL}\n\n"
        f"You get one email per limit each {noun}. Change limits in Folio → Settings.\n"
    )
    body = (
        f"<p>You've spent <strong>{spent_text}</strong>{html.escape(on)} this {noun} ({window}), "
        f"<strong>{percent}%</strong> of your {limit_text} {html.escape(of_limit)}.</p>"
        f"<p style='color:#6e6e78'>Latest payment: {html.escape(latest)}.</p>"
        f"<p style='color:#6e6e78;font-size:13px'>You get one email per limit each {noun}. Change limits in Folio → Settings.</p>"
    )
    return subject, text, _html_email(html.escape(f"{name} {state}"), body)


async def _alert_once(db: AsyncSession, user: User, period: str, start: date, end: datetime,
                      limit: Decimal, spent: Decimal, txn: Transaction, currency: str,
                      key_limit: Optional[Decimal] = None) -> None:
    """`limit` is the limit the email shows (a rollover limit); `key_limit` the one the alert is kept under (the
    base limit; defaults to `limit`)."""
    key_limit = limit if key_limit is None else key_limit
    alert = await db.scalar(select(SpendAlert).where(*_alert_key(user.id, period, start, key_limit)))
    if alert is None:
        alert = SpendAlert(user_id=user.id, period=period, period_start=start, limit_amount=key_limit,
                           spent_amount=spent, status="sending", attempts=0)
        db.add(alert)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # a simultaneous SMS already claimed this alert
            return
    elif alert.status == "failed" and alert.attempts < MAX_ATTEMPTS:
        claimed = await db.execute(
            update(SpendAlert).where(SpendAlert.id == alert.id, SpendAlert.status == "failed").values(status="sending")
        )
        await db.commit()
        if claimed.rowcount != 1:
            return
        await db.refresh(alert)
    else:
        return  # already sent, being sent, or out of retries

    subject, text, body = compose_alert(period, limit, spent, start, end, txn, currency)
    try:
        await mailer.send_email(mailer.recipient(), subject, text, body)
        alert.status, alert.error = "sent", None
    except Exception as error:  # noqa: BLE001 - recorded and retried on the next payment
        logger.warning("Spend alert email failed: %s", error)
        alert.status, alert.error = "failed", str(error)[:500]
    alert.attempts += 1
    alert.spent_amount = spent
    await db.commit()


async def _notify_once(db: AsyncSession, user: User, period: str, start: date, end: datetime,
                       limit: Decimal, spent: Decimal, currency: str, category: Optional[str] = None, *,
                       label: Optional[str] = None, dedupe_key: Optional[str] = None,
                       warn_percent: Optional[int] = None) -> bool:
    """True if the notification is new (it's only ever created once per limit and period).
    `label` is a merchant limit's full name; it's shortened in the title only. With `warn_percent`, the early
    warning at that level instead of the reached alert (same body)."""
    noun = "week" if period == "weekly" else "month"
    on = f" at {label}" if label else f" on {category}" if category else ""
    scope = _short_label(label) if label else category
    if warn_percent is None:
        state = "reached" if spent == limit else "crossed"
        kind, title = "spend_limit", f"{_limit_name(period, scope)} {state}"
        key = dedupe_key or _notification_key(period, start, limit, category)
    else:
        kind = "spend_warning"
        title = f"{warn_percent}% of your {'weekly' if period == 'weekly' else 'monthly'} {scope or 'spend'} limit used"
        key = dedupe_key or _warning_key(period, start, limit, warn_percent, category)
    return await create_once(
        db, user.id,
        kind=kind,
        title=title,
        body=(f"{format_money(spent, currency)} of {format_money(limit, currency)}{on} this {noun} "
              f"({_window_text(start, end)}) · {_percent(spent, limit)}%"),
        link="/settings/limits",
        dedupe_key=key,
    )


async def post_save_check(ctx: PostSave) -> None:
    """Registered with post_save for every cause: the overall, category and merchant limits this payment
    can reach. post_save.load has already skipped other users' rows and held-out possible duplicates."""
    db, owner, txn = ctx.db, ctx.owner, ctx.txn
    if not txn.is_parsed or txn.type != "DEBIT" or not counts_as_spending(txn.category):
        return
    now, when = utc_now(), _utc(txn.transaction_time)
    warn_percent = ctx.settings["limits.warn_percent"]  # None = warnings Off
    windows = await current_windows(db, owner, now, ctx.settings.get("periods.payday", False))
    setting, rollover_since = await _overall_limit(db, owner.id)
    if setting is not None and txn.currency == setting.currency:
        currency = setting.currency
        limits = {period: getattr(setting, f"{period}_limit") for period in PERIODS}
        for period in PERIODS:
            limit = limits[period]
            if limit is None:
                continue
            start_date, start, end = windows[period]
            if not start <= when < end:
                continue  # a late SMS for a past week/month doesn't alert
            spent = await spent_between(db, owner, currency, start, end)
            effective = await _effective_limit(db, owner, currency, period, limit, rollover_since, windows)
            if _reached(spent, effective):
                await _notify_once(db, owner, period, start_date, end, effective, spent, currency,
                                   dedupe_key=_notification_key(period, start_date, limit))
                # Email is optional: with no Gmail keys nothing is recorded, so it still goes out once configured.
                if mailer.configured():
                    await _alert_once(db, owner, period, start_date, end, effective, spent, txn, currency, key_limit=limit)
            elif _warns(spent, effective, warn_percent):  # in-app only, never emailed
                await _notify_once(db, owner, period, start_date, end, effective, spent, currency,
                                   dedupe_key=_warning_key(period, start_date, limit, warn_percent),
                                   warn_percent=warn_percent)
    await _check_category(db, owner, txn, windows, when, warn_percent)
    try:  # last and on its own, so a failure here can't stop the overall and category alerts
        await _check_merchants(db, owner, txn, windows, when, warn_percent)
    except Exception:
        logger.exception("Merchant limit check failed")


async def _overall_limit(db: AsyncSession, user_id: uuid.UUID) -> tuple[Optional[SpendLimit], Optional[datetime]]:
    """The overall limit and when its rollover was turned on (None = off), in one statement."""
    row = (await db.execute(
        select(SpendLimit, LimitRollover.enabled_at).outerjoin(
            LimitRollover, and_(LimitRollover.user_id == SpendLimit.user_id, LimitRollover.category == ALL)
        ).where(SpendLimit.user_id == user_id)
    )).first()
    return (None, None) if row is None else (row[0], row[1])


async def _effective_limit(db: AsyncSession, owner: User, currency: str, period: str, limit: Decimal,
                           rollover_since: Optional[datetime], windows: dict[str, Optional[Window]],
                           category: Optional[str] = None) -> Decimal:
    """The limit spending is compared with: the rollover limit for a monthly limit that carries (one more query,
    last month's spending), else the limit as set."""
    if period != "monthly" or not carry_applies(limit, rollover_since, windows):
        return limit
    _, previous_start, previous_end = previous_monthly_window(windows)
    previous = await spent_between(db, owner, currency, previous_start, previous_end, category)
    return rollover_limit(limit, previous)


async def _check(db: AsyncSession, transaction_id: uuid.UUID) -> None:
    """Limits only, loaded the way the post-save runner loads (owner, transaction, held-out test, settings)."""
    ctx = await post_save.load(db, transaction_id, "edit")
    if ctx is not None:
        await post_save_check(ctx)


async def _check_category(db: AsyncSession, owner: User, txn: Transaction, windows: dict[str, Optional[Window]],
                          when: datetime, warn_percent: Optional[int]) -> None:
    """The limit on this transaction's category: a notification, plus one best-effort email; or the in-app
    warning at `warn_percent` (None = Off) while still under it."""
    if txn.category is None or txn.category in NOT_LIMITABLE:
        return
    found = (await db.execute(
        select(CategoryLimit, LimitRollover.enabled_at).outerjoin(
            LimitRollover, and_(LimitRollover.user_id == CategoryLimit.user_id,
                                LimitRollover.category == CategoryLimit.category)
        ).where(CategoryLimit.user_id == owner.id, CategoryLimit.category == txn.category)
    )).first()
    if found is None or found[0].currency != txn.currency:
        return
    rule, rollover_since = found
    # Plain values, as in post_save_check: a lost notification race rolls back and expires the row.
    category, currency = rule.category, rule.currency
    limits = {period: getattr(rule, f"{period}_limit") for period in PERIODS}
    for period in PERIODS:
        limit = limits[period]
        if limit is None:
            continue
        start_date, start, end = windows[period]
        if not start <= when < end:
            continue
        spent = await spent_between(db, owner, currency, start, end, category)
        effective = await _effective_limit(db, owner, currency, period, limit, rollover_since, windows, category)
        if not _reached(spent, effective):
            if _warns(spent, effective, warn_percent):  # in-app only, never emailed
                await _notify_once(db, owner, period, start_date, end, effective, spent, currency, category,
                                   dedupe_key=_warning_key(period, start_date, limit, warn_percent, category),
                                   warn_percent=warn_percent)
            continue
        created = await _notify_once(db, owner, period, start_date, end, effective, spent, currency, category,
                                     dedupe_key=_notification_key(period, start_date, limit, category))
        if created and mailer.configured():
            try:
                subject, text, body = compose_alert(period, effective, spent, start_date, end, txn, currency, category)
                await mailer.send_email(mailer.recipient(), subject, text, body)
            except Exception as error:  # noqa: BLE001 - the notification already went out
                logger.warning("Category limit email failed: %s", error)


async def _check_merchants(db: AsyncSession, owner: User, txn: Transaction, windows: dict[str, Optional[Window]],
                           when: datetime, warn_percent: Optional[int]) -> None:
    """Every merchant limit whose name matches this transaction's merchant: a notification each,
    plus one best-effort email, as for category limits; or the in-app warning while still under it."""
    if txn.currency != "INR":
        return  # merchant limits are in rupees
    # Plain values, as in post_save_check: a lost notification race rolls back and expires the rows.
    rules = [(rule.id, rule.merchant, rule.currency, rule.weekly_limit, rule.monthly_limit)
             for rule in await load_merchant_limits(db, owner.id) if merchants.matches(rule.merchant, txn.merchant)]
    if not rules:
        return
    since = min(windows[period][1] for period in PERIODS)  # never .values(): it may hold previous_monthly
    rows = await _merchant_rows(db, owner, "INR", since)
    spans = {period: windows[period][1:] for period in PERIODS}
    for rule_id, merchant, currency, weekly_limit, monthly_limit in rules:
        weekly, monthly, _ = merchants.meter(merchant, rows, spans)
        for period, spent, limit in (("weekly", weekly, weekly_limit), ("monthly", monthly, monthly_limit)):
            if limit is None:
                continue
            start_date, start, end = windows[period]
            if not start <= when < end:
                continue  # a late SMS for a past week/month doesn't alert
            if spent < limit:
                if _warns(spent, limit, warn_percent):  # in-app only, never emailed
                    await _notify_once(db, owner, period, start_date, end, limit, spent, currency, label=merchant,
                                       dedupe_key=_merchant_warning_key(rule_id, period, start_date, limit, warn_percent),
                                       warn_percent=warn_percent)
                continue
            created = await _notify_once(db, owner, period, start_date, end, limit, spent, currency, label=merchant,
                                         dedupe_key=_merchant_notification_key(rule_id, period, start_date, limit))
            if created and mailer.configured():
                try:
                    subject, text, body = compose_alert(period, limit, spent, start_date, end, txn, currency, label=merchant)
                    await mailer.send_email(mailer.recipient(), subject, text, body)
                except Exception as error:  # noqa: BLE001 - the notification already went out
                    logger.warning("Merchant limit email failed: %s", error)


async def check_spend_limits(transaction_id: uuid.UUID) -> None:
    """Limits only, for callers that need nothing else: rules.py after a merge or a rule apply. Saving,
    editing and categorising a payment run every after-save check instead (post_save.run). Never raises:
    alerts must not affect recording."""
    try:
        async with AsyncSessionLocal() as db:
            await _check(db, transaction_id)
    except Exception:
        logger.exception("Spend-limit check failed")
