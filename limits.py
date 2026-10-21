"""Weekly and monthly spend limits. The first time each is reached, an in-app notification
is created (bell on Home), plus a Gmail alert if GMAIL_* keys are configured (optional).

"Spent" matches the dashboard: parsed debits in the limit's currency, card spends included.
Weeks run Monday to Sunday and months follow the calendar, both in India Standard Time.
"""
import html
import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import mailer
from auth import FRONTEND_URL, OWNER_EMAIL, current_user, same_origin
from dashboard import ownership
from database import AsyncSessionLocal, get_db
from models import Notification, SpendAlert, SpendLimit, Transaction, User
from notifications import create_once

router = APIRouter(prefix="/api/v1/limits", tags=["Spend limits"])
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
PERIODS = ("weekly", "monthly")
MAX_ATTEMPTS = 3  # a failed email is retried on the next payments, up to this many tries
MAX_AMOUNT = Decimal("999999999999.99")  # Numeric(14, 2)


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


def _spent_query(user: User, currency: str, start: datetime, end: datetime):
    return select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        ownership(user),
        Transaction.is_parsed.is_(True),
        Transaction.type == "DEBIT",
        Transaction.currency == currency,
        Transaction.transaction_time >= start,
        Transaction.transaction_time < end,
    )


def _money(total) -> Decimal:
    return Decimal(str(total)).quantize(Decimal("0.01"))


async def spent_between(db: AsyncSession, user: User, currency: str, start: datetime, end: datetime) -> Decimal:
    return _money(await db.scalar(_spent_query(user, currency, start, end)))


async def _setting(db: AsyncSession, user_id: uuid.UUID) -> Optional[SpendLimit]:
    return await db.scalar(select(SpendLimit).where(SpendLimit.user_id == user_id))


def _notification_key(period: str, start: date, limit: Decimal) -> str:
    # The limit is part of the key, so raising a limit mid-period notifies again.
    return f"spend_limit:{period}:{start.isoformat()}:{Decimal(limit):.2f}"


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
    weekly_limit: Optional[Decimal] = _limit_field()  # null = no limit
    monthly_limit: Optional[Decimal] = _limit_field()


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


class LimitsSummary(BaseModel):
    currency: str
    weekly: PeriodStatus
    monthly: PeriodStatus
    email: EmailStatus


async def summarize(db: AsyncSession, user: User, setting: Optional[SpendLimit]) -> LimitsSummary:
    """Both meters in a single query: the database may be a continent away, so each
    round trip costs far more than the work itself."""
    now = utc_now()
    currency = setting.currency if setting else "INR"
    windows = {period: period_window(period, now) for period in PERIODS}
    amounts = {period: getattr(setting, f"{period}_limit") if setting else None for period in PERIODS}

    columns = []
    for period in PERIODS:
        start_date, start, end = windows[period]
        limit = amounts[period]
        columns.append(_spent_query(user, currency, start, end).scalar_subquery().label(f"{period}_spent"))
        if limit:
            columns.append(exists().where(
                Notification.user_id == user.id,
                Notification.dedupe_key == _notification_key(period, start_date, limit),
            ).label(f"{period}_alerted"))
            columns.append(select(SpendAlert.error).where(
                *_alert_key(user.id, period, start_date, limit), SpendAlert.status == "failed",
            ).scalar_subquery().label(f"{period}_error"))
    row = (await db.execute(select(*columns))).one()._mapping

    periods: dict[str, PeriodStatus] = {}
    last_error = None
    for period in PERIODS:
        start_date, _, end = windows[period]
        limit = amounts[period]
        if limit and row[f"{period}_error"] is not None:
            last_error = row[f"{period}_error"]
        periods[period] = PeriodStatus(
            limit=float(limit) if limit else None,
            spent=float(_money(row[f"{period}_spent"])),
            start=start_date,
            end=_last_day(end),
            alerted=bool(limit) and bool(row[f"{period}_alerted"]),
        )
    return LimitsSummary(
        currency=currency,
        weekly=periods["weekly"],
        monthly=periods["monthly"],
        email=EmailStatus(configured=mailer.configured(), to=mailer.recipient() or None, last_error=last_error),
    )


@router.get("", response_model=LimitsSummary)
async def get_limits(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await summarize(db, user, await _setting(db, user.id))


@router.put("", response_model=LimitsSummary, dependencies=[Depends(same_origin)])
async def set_limits(body: LimitsInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    async def save() -> SpendLimit:
        setting = await _setting(db, user.id)
        if setting is None:
            setting = SpendLimit(user_id=user.id, currency="INR")
            db.add(setting)
        setting.weekly_limit = body.weekly_limit
        setting.monthly_limit = body.monthly_limit
        await db.commit()
        return setting

    try:
        setting = await save()
    except IntegrityError:
        await db.rollback()  # two saves raced to create the row; update the winner
        setting = await save()
    await db.refresh(setting)
    return await summarize(db, user, setting)


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


def compose_alert(period: str, limit: Decimal, spent: Decimal, start: date, end_utc: datetime,
                  txn: Transaction, currency: str) -> tuple[str, str, str]:
    """Subject, plain text and HTML for a limit alert."""
    label = "Weekly" if period == "weekly" else "Monthly"
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

    subject = f"{label} spend limit {state}: {spent_text} of {limit_text}"
    text = (
        f"You've spent {spent_text} this {noun} ({window}), {percent}% of your {limit_text} {noun}ly limit.\n\n"
        f"Latest payment: {latest}.\n\n"
        f"Open Folio: {FRONTEND_URL}\n\n"
        f"You get one email per limit each {noun}. Change limits in Folio → Settings.\n"
    )
    body = (
        f"<p>You've spent <strong>{spent_text}</strong> this {noun} ({window}), "
        f"<strong>{percent}%</strong> of your {limit_text} {noun}ly limit.</p>"
        f"<p style='color:#6e6e78'>Latest payment: {html.escape(latest)}.</p>"
        f"<p style='color:#6e6e78;font-size:13px'>You get one email per limit each {noun}. Change limits in Folio → Settings.</p>"
    )
    return subject, text, _html_email(html.escape(f"{label} spend limit {state}"), body)


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
                       limit: Decimal, spent: Decimal, currency: str) -> None:
    label, noun = ("Weekly", "week") if period == "weekly" else ("Monthly", "month")
    state = "reached" if spent == limit else "crossed"
    await create_once(
        db, user.id,
        kind="spend_limit",
        title=f"{label} spend limit {state}",
        body=(f"{format_money(spent, currency)} of {format_money(limit, currency)} this {noun} "
              f"({_window_text(start, end)}) · {int(spent * 100 / limit)}%"),
        link="/settings#limits",
        dedupe_key=_notification_key(period, start, limit),
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
    setting = await _setting(db, owner.id)
    if setting is None or txn.currency != setting.currency:
        return
    now, when = utc_now(), _utc(txn.transaction_time)
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


async def check_spend_limits(transaction_id: uuid.UUID) -> None:
    """Background task after a webhook insert. Never raises: alerts must not affect recording."""
    try:
        async with AsyncSessionLocal() as db:
            await _check(db, transaction_id)
    except Exception:
        logger.exception("Spend-limit check failed")
