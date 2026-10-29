"""Weekly and monthly spend limits, overall ("All") and per category. The first time each is
reached, an in-app notification is created (bell on Home), plus a Gmail alert if GMAIL_* keys
are configured (optional; retried for overall limits, best effort for category limits).

"Spent" matches the dashboard: parsed debits in the limit's currency, card spends included.
Weeks run Monday to Sunday and months follow the calendar, both in India Standard Time.
"""
import html
import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, delete, exists, func, literal, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import mailer
from auth import FRONTEND_URL, OWNER_EMAIL, current_user, same_origin
from dashboard import ownership
from database import AsyncSessionLocal, get_db
from models import CategoryLimit, Notification, SpendAlert, SpendLimit, Transaction, User
from notifications import create_once
from parser import CATEGORIES, UNCATEGORIZED

router = APIRouter(prefix="/api/v1/limits", tags=["Spend limits"])
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
PERIODS = ("weekly", "monthly")
MAX_ATTEMPTS = 3  # a failed email is retried on the next payments, up to this many tries
MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14, 2)
ALL = "All"  # the overall limit, across every category
# Limits apply to spending, so money-in categories (and Uncategorized) can't have one.
LIMIT_CATEGORIES = [c for c in CATEGORIES if c not in (UNCATEGORIZED, "Salary", "Income", "Refunds & Cashback")]


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
    query = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        ownership(user),
        Transaction.is_parsed.is_(True),
        Transaction.type == "DEBIT",
        Transaction.currency == currency,
        Transaction.transaction_time >= start,
        Transaction.transaction_time < end,
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


@dataclass(frozen=True)
class Rule:
    """A limit you've set: overall (category "All") or for one category."""

    category: str
    currency: str
    weekly_limit: Optional[Decimal]
    monthly_limit: Optional[Decimal]


async def load_rules(db: AsyncSession, user_id: uuid.UUID) -> list[Rule]:
    """The overall limit and every category limit, in one query. Overall first, then list order."""
    overall = select(
        literal(ALL, String).label("category"), SpendLimit.currency, SpendLimit.weekly_limit, SpendLimit.monthly_limit
    ).where(SpendLimit.user_id == user_id)
    per_category = select(
        CategoryLimit.category, CategoryLimit.currency, CategoryLimit.weekly_limit, CategoryLimit.monthly_limit
    ).where(CategoryLimit.user_id == user_id)
    rules = [Rule(*row) for row in (await db.execute(union_all(overall, per_category))).all()]
    order = {name: i for i, name in enumerate([ALL, *LIMIT_CATEGORIES])}
    return sorted(rules, key=lambda rule: order.get(rule.category, len(order)))


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


class LimitsInput(BaseModel):
    category: Optional[str] = Field(default=None, description='A spending category; omit or "All" for overall spending')
    weekly_limit: Optional[Decimal] = _limit_field()  # null = no limit
    monthly_limit: Optional[Decimal] = _limit_field()

    @field_validator("category")
    @classmethod
    def _category(cls, value: Optional[str]) -> Optional[str]:
        wanted = (value or "").strip().casefold()
        if not wanted or wanted == ALL.casefold():
            return None  # overall
        match = next((c for c in LIMIT_CATEGORIES if c.casefold() == wanted), None)
        if match is None:
            raise ValueError("Pick a spending category, or All.")
        return match


class PeriodStatus(BaseModel):
    limit: Optional[float]
    spent: float
    start: date
    end: date  # last day, inclusive
    alerted: bool  # notification created for this period and limit


class EmailStatus(BaseModel):
    configured: bool
    to: Optional[str]
    last_error: Optional[str]


class CategoryStatus(BaseModel):
    category: str
    weekly: PeriodStatus
    monthly: PeriodStatus


class LimitsSummary(BaseModel):
    currency: str
    weekly: PeriodStatus  # overall
    monthly: PeriodStatus
    categories: list[CategoryStatus] = []  # only categories with a limit
    email: EmailStatus


async def summarize(db: AsyncSession, user: User, rules: list[Rule]) -> LimitsSummary:
    """Every meter, overall and per category, in a single query: the database may be a continent
    away, so each round trip costs far more than the work itself."""
    now = utc_now()
    overall = next((rule for rule in rules if rule.category == ALL), None)
    per_category = [rule for rule in rules if rule.category != ALL]
    currency = overall.currency if overall else "INR"
    windows = {period: period_window(period, now) for period in PERIODS}

    def amounts(rule: Optional[Rule]) -> dict[str, Optional[Decimal]]:
        return {period: getattr(rule, f"{period}_limit") if rule else None for period in PERIODS}

    # (column prefix, category or None for overall, currency, limits)
    meters = [("all", None, currency, amounts(overall))]
    meters += [(f"c{i}", rule.category, rule.currency, amounts(rule)) for i, rule in enumerate(per_category)]
    columns = []
    for prefix, category, meter_currency, limits in meters:
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
    row = (await db.execute(select(*columns))).one()._mapping

    def statuses(prefix: str, limits: dict[str, Optional[Decimal]]) -> dict[str, PeriodStatus]:
        out = {}
        for period in PERIODS:
            start_date, _, end = windows[period]
            limit = limits[period]
            out[period] = PeriodStatus(
                limit=float(limit) if limit else None,
                spent=float(_money(row[f"{prefix}_{period}_spent"])),
                start=start_date,
                end=_last_day(end),
                alerted=bool(limit) and bool(row[f"{prefix}_{period}_alerted"]),
            )
        return out

    overall_limits = meters[0][3]
    # The monthly failure wins over the weekly one, as before.
    last_error = next((row[f"all_{period}_error"] for period in reversed(PERIODS)
                       if overall_limits[period] and row[f"all_{period}_error"] is not None), None)
    overall_status = statuses("all", overall_limits)
    categories = []
    for prefix, category, _, limits in meters[1:]:
        status = statuses(prefix, limits)
        categories.append(CategoryStatus(category=category, weekly=status["weekly"], monthly=status["monthly"]))
    return LimitsSummary(
        currency=currency,
        weekly=overall_status["weekly"],
        monthly=overall_status["monthly"],
        categories=categories,
        email=EmailStatus(configured=mailer.configured(), to=mailer.recipient() or None, last_error=last_error),
    )


@router.get("", response_model=LimitsSummary)
async def get_limits(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await summarize(db, user, await load_rules(db, user.id))


@router.put("", response_model=LimitsSummary, dependencies=[Depends(same_origin)])
async def set_limits(body: LimitsInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Sets the overall limit, or one category's when `category` is given. Leaving both amounts
    empty removes a category's limit."""
    category = body.category
    if category and body.weekly_limit is None and body.monthly_limit is None:
        await db.execute(delete(CategoryLimit).where(CategoryLimit.user_id == user.id, CategoryLimit.category == category))
        await db.commit()
        return await summarize(db, user, await load_rules(db, user.id))

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
        await db.commit()

    try:
        await save()
    except IntegrityError:
        await db.rollback()  # two saves raced to create the row; update the winner
        await save()
    return await summarize(db, user, await load_rules(db, user.id))


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


# ---------- Alerts (run after each webhook insert) ----------

def _html_email(heading: str, body: str) -> str:
    return (
        '<div style="font-family:-apple-system,Segoe UI,Inter,Arial,sans-serif;max-width:520px;margin:0 auto;color:#111113">'
        '<div style="background:linear-gradient(160deg,#8653f3,#b796fb);border-radius:20px;padding:24px;color:#fff">'
        f'<p style="margin:0;font-size:14px;opacity:.85">Folio</p><h1 style="margin:6px 0 0;font-size:22px">{heading}</h1></div>'
        f'<div style="padding:20px 4px;font-size:15px;line-height:1.55">{body}'
        f'<p style="margin-top:20px"><a href="{html.escape(FRONTEND_URL)}" style="background:#141211;color:#fff;'
        'padding:10px 18px;border-radius:999px;text-decoration:none;font-weight:600">Open Folio</a></p></div></div>'
    )


def _limit_name(period: str, category: Optional[str]) -> str:
    """ "Weekly spend limit" overall, "Weekly Food & Dining limit" for a category."""
    label = "Weekly" if period == "weekly" else "Monthly"
    return f"{label} {category or 'spend'} limit"


def compose_alert(period: str, limit: Decimal, spent: Decimal, start: date, end_utc: datetime,
                  txn: Transaction, currency: str, category: Optional[str] = None) -> tuple[str, str, str]:
    """Subject, plain text and HTML for a limit alert."""
    name = _limit_name(period, category)
    on = f" on {category}" if category else ""
    noun = "week" if period == "weekly" else "month"
    state = "reached" if spent == limit else "crossed"
    percent = int(spent * 100 / limit)
    window = _window_text(start, end_utc)
    when = _utc(txn.transaction_time).astimezone(IST)
    latest_where = txn.merchant or txn.category or "a payment"
    account = f" ({txn.account_ref})" if txn.account_ref else ""
    clock = f"{when:%I:%M}".lstrip("0") + (" am" if when.hour < 12 else " pm")
    latest = f"{format_money(txn.amount, txn.currency)} at {latest_where}{account}, {when.day} {when:%b}, {clock}"
    spent_text, limit_text = format_money(spent, currency), format_money(limit, currency)

    subject = f"{name} {state}: {spent_text} of {limit_text}"
    text = (
        f"You've spent {spent_text}{on} this {noun} ({window}), {percent}% of your {limit_text} {name.lower()}.\n\n"
        f"Latest payment: {latest}.\n\n"
        f"Open Folio: {FRONTEND_URL}\n\n"
        f"You get one email per limit each {noun}. Change limits in Folio → Settings.\n"
    )
    body = (
        f"<p>You've spent <strong>{spent_text}</strong>{html.escape(on)} this {noun} ({window}), "
        f"<strong>{percent}%</strong> of your {limit_text} {html.escape(name.lower())}.</p>"
        f"<p style='color:#6e6e78'>Latest payment: {html.escape(latest)}.</p>"
        f"<p style='color:#6e6e78;font-size:13px'>You get one email per limit each {noun}. Change limits in Folio → Settings.</p>"
    )
    return subject, text, _html_email(html.escape(f"{name} {state}"), body)


async def _alert_once(db: AsyncSession, user: User, period: str, start: date, end: datetime,
                      limit: Decimal, spent: Decimal, txn: Transaction, currency: str) -> None:
    alert = await db.scalar(select(SpendAlert).where(*_alert_key(user.id, period, start, limit)))
    if alert is None:
        alert = SpendAlert(user_id=user.id, period=period, period_start=start, limit_amount=limit,
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
                       limit: Decimal, spent: Decimal, currency: str, category: Optional[str] = None) -> bool:
    """True if the notification is new (it's only ever created once per limit and period)."""
    noun = "week" if period == "weekly" else "month"
    state = "reached" if spent == limit else "crossed"
    on = f" on {category}" if category else ""
    return await create_once(
        db, user.id,
        kind="spend_limit",
        title=f"{_limit_name(period, category)} {state}",
        body=(f"{format_money(spent, currency)} of {format_money(limit, currency)}{on} this {noun} "
              f"({_window_text(start, end)}) · {int(spent * 100 / limit)}%"),
        link="/settings#limits",
        dedupe_key=_notification_key(period, start, limit, category),
    )


async def _check(db: AsyncSession, transaction_id: uuid.UUID) -> None:
    if not OWNER_EMAIL:
        return
    txn = await db.get(Transaction, transaction_id)
    if txn is None or not txn.is_parsed or txn.type != "DEBIT":
        return
    owner = await db.scalar(select(User).where(User.email == OWNER_EMAIL))
    if owner is None or txn.user_id not in (None, owner.id):
        return
    now, when = utc_now(), _utc(txn.transaction_time)
    setting = await _setting(db, owner.id)
    if setting is not None and txn.currency == setting.currency:
        for period in PERIODS:
            limit = getattr(setting, f"{period}_limit")
            if limit is None:
                continue
            start_date, start, end = period_window(period, now)
            if not start <= when < end:
                continue  # a late SMS for a past week/month doesn't alert
            spent = await spent_between(db, owner, setting.currency, start, end)
            if spent < limit:
                continue
            await _notify_once(db, owner, period, start_date, end, limit, spent, setting.currency)
            # Email is optional: with no Gmail keys nothing is recorded, so it still goes out once configured.
            if mailer.configured():
                await _alert_once(db, owner, period, start_date, end, limit, spent, txn, setting.currency)
    await _check_category(db, owner, txn, now, when)


async def _check_category(db: AsyncSession, owner: User, txn: Transaction, now: datetime, when: datetime) -> None:
    """The limit on this transaction's category: a notification, plus one best-effort email."""
    if txn.category not in LIMIT_CATEGORIES:
        return
    rule = await db.scalar(select(CategoryLimit).where(CategoryLimit.user_id == owner.id, CategoryLimit.category == txn.category))
    if rule is None or rule.currency != txn.currency:
        return
    for period in PERIODS:
        limit = getattr(rule, f"{period}_limit")
        if limit is None:
            continue
        start_date, start, end = period_window(period, now)
        if not start <= when < end:
            continue
        spent = await spent_between(db, owner, rule.currency, start, end, rule.category)
        if spent < limit:
            continue
        if await _notify_once(db, owner, period, start_date, end, limit, spent, rule.currency, rule.category) and mailer.configured():
            try:
                subject, text, body = compose_alert(period, limit, spent, start_date, end, txn, rule.currency, rule.category)
                await mailer.send_email(mailer.recipient(), subject, text, body)
            except Exception as error:  # noqa: BLE001 - the notification already went out
                logger.warning("Category limit email failed: %s", error)


async def check_spend_limits(transaction_id: uuid.UUID) -> None:
    """Background task after a transaction is recorded, edited or categorised. Never raises:
    alerts must not affect recording."""
    try:
        async with AsyncSessionLocal() as db:
            await _check(db, transaction_id)
    except Exception:
        logger.exception("Spend-limit check failed")
