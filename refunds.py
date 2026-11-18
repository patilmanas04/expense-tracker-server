"""Refund watch: a payment you expect money back for (a return, a cancelled order), and the credit that settles it.

**Watch** (`refund_watches`, one per payment): you mark a parsed debit "Expect a refund" with an amount (more than
zero, at most the payment) and an "expected by" date 1 to 60 days after today (IST). It is open until you confirm a
credit as its refund ("refunded"); "No refund expected" deletes the row. Each transaction lists `refund`
("expected" / "refunded" / null) and, on the confirmed credit, `refund_for` (the payment's id).

**Suggest** (post-save check `suggest`, causes sms / manual / restored): a saved credit is offered as the refund of
one open watch, as a refund_match notification with Yes / No. The credit must be parsed, more than zero, not a card
payment or a transfer between your own accounts (spending.NOT_COUNTED), and not already a watch's refund. A watch
is a candidate when its payment has the same currency and was made from 90 days before the credit up to the credit,
and the credit is exactly the expected amount, or less and shares a word with the payment's merchant
(merchants.share_word on the credit's merchant and SMS text). The best one wins: exact amount first, then the
nearest payment, then the older watch. One notification per (watch, credit), ever (create_once).

**Resolver** (register_actions("refund_match")): Yes / No while the watch is open and the credit still exists;
"Marked as refunded" once this credit closed it; "No longer applies" otherwise (another credit closed it, the
watch was cancelled, either row was deleted). No rejects a suggestion by deleting it, so it can come back only if
the same credit is saved again.

**Overdue** (`daily`, jobs.register("refund_overdue")): one refund_overdue notification per open watch whose date
has passed, once per watch. No email.

**Delete** (`release`, from main.delete_transaction): deleting a watched payment deletes its watch; deleting a
confirmed credit makes its watch open again. Every write that changes `refund` or `refund_for` bumps `updated_at`
on each row it changes, in the same commit, so the app's poll sees it. Edits don't touch watches.

**Cost**: suggest 0 statements for anything but a counted credit, then 1, or 2 when an open watch is in the window,
plus 1 commit when it notifies; the resolver at most 2 per inbox read; the job 1 plus 1 commit per new reminder.
This module never imports main.
"""
import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import limits
from auth import current_user, same_origin
from dashboard import ownership
from database import get_db
from limits import IST, _short_label, _utc
from merchants import clean, share_word, words
from models import Notification, NotificationDismissal, RefundWatch, Transaction, User
from notifications import NotificationAction, Resolution, create_once, register_actions
from post_save import PostSave
from recurring import _amount, _day_text, _money_text
from spending import NOT_COUNTED

logger = logging.getLogger(__name__)

MATCH_DAYS = 90  # a credit up to 90 days after the payment can be its refund
DEFAULT_DUE_DAYS = 10  # the app's default "expected by" date, days after today
MAX_DUE_DAYS = 60  # "expected by" must be 1 to 60 days after today (IST)
MATCH_KIND = "refund_match"
OVERDUE_KIND = "refund_overdue"
EXPECTED, REFUNDED = "expected", "refunded"

NOT_FOUND = "No refund is expected for this payment."
ALREADY_EXPECTED = "A refund is already expected for this payment."
CREDIT_TAKEN = "That credit is already matched to another refund."


def _today() -> dt.date:
    return limits.utc_now().astimezone(IST).date()


def _now() -> dt.datetime:
    # Real time, not limits.utc_now: the app's polling cursor must always move forward.
    return dt.datetime.now(dt.timezone.utc)


def _label(merchant: Optional[str]) -> Optional[str]:
    """The merchant as a notification shows it, or None when it has no words ("Unknown", blank)."""
    return _short_label(clean(merchant)) if words(merchant) else None


def _match_key(debit_id, credit_id) -> str:
    return f"refund:{debit_id}:{credit_id}"


def _parse_match_key(key: Optional[str]) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    parts = (key or "").split(":")
    if len(parts) != 3 or parts[0] != "refund":
        return None
    try:
        return uuid.UUID(parts[1]), uuid.UUID(parts[2])
    except ValueError:
        return None


async def _bump(db: AsyncSession, ids) -> None:
    """Moves updated_at on these transactions, so the app's poll picks them up. No commit."""
    ids = [i for i in ids if i is not None]
    if ids:
        await db.execute(update(Transaction).where(Transaction.id.in_(ids)).values(updated_at=_now()))


def _ist_day(moment: dt.datetime) -> dt.date:
    return _utc(moment).astimezone(IST).date()


# ---------- Suggest (post-save) ----------

async def suggest(ctx: PostSave) -> None:
    """post_save.register("refunds", refunds.suggest, {"sms", "manual", "restored"}). The runner already skipped
    held-out rows, other users' rows and a missing owner, and catches anything raised here."""
    credit = ctx.txn  # detached: read freely
    if not credit.is_parsed or credit.type != "CREDIT" or credit.amount is None or credit.amount <= 0:
        return
    if credit.category in NOT_COUNTED:
        return  # a card payment or a transfer between your own accounts is never a refund
    db, owner_id = ctx.db, ctx.owner.id
    credit_id, currency = credit.id, credit.currency
    when = _utc(credit.transaction_time)
    rows = (await db.execute(
        select(RefundWatch.id, RefundWatch.transaction_id, RefundWatch.expected_amount, RefundWatch.created_at,
               Transaction.merchant, Transaction.transaction_time)
        .join(Transaction, Transaction.id == RefundWatch.transaction_id)
        .where(RefundWatch.user_id == owner_id,
               RefundWatch.credit_id.is_(None),
               Transaction.currency == currency,
               Transaction.transaction_time >= when - dt.timedelta(days=MATCH_DAYS),
               Transaction.transaction_time <= when,
               Transaction.id != credit_id)
    )).all()
    if not rows:
        return
    if await db.scalar(select(RefundWatch.id).where(RefundWatch.credit_id == credit_id)) is not None:
        return  # already the refund of another payment
    amount = Decimal(str(credit.amount))
    text = f"{credit.merchant or ''} {credit.raw_message or ''}"
    best = None
    for watch_id, debit_id, expected, created_at, debit_merchant, debit_time in rows:
        expected = Decimal(str(expected))
        exact = amount == expected
        if not exact and not (amount <= expected and share_word(debit_merchant, text)):
            continue
        key = (0 if exact else 1, when - _utc(debit_time), _utc(created_at), str(watch_id))
        if best is None or key < best[0]:
            best = (key, debit_id, debit_merchant)
    if best is None:
        return
    _, debit_id, debit_merchant = best
    label, credit_label = _label(debit_merchant), _label(credit.merchant)
    money, day = _money_text(amount), _day_text(_ist_day(when))
    await create_once(
        db, owner_id, kind=MATCH_KIND, dedupe_key=_match_key(debit_id, credit_id),
        title=f"Is this your {label} refund?" if label else "Is this a refund?",
        body=f"{money} from {credit_label} on {day}" if credit_label else f"{money} on {day}",
        link=None,
    )


# ---------- Notification buttons ----------

async def refund_actions(db: AsyncSession, rows: list[Notification]) -> dict[uuid.UUID, Resolution]:
    """Yes / No, or the text shown once decided, per refund_match notification. At most 2 statements."""
    keys = {row.id: parsed for row in rows if (parsed := _parse_match_key(row.dedupe_key)) is not None}
    if not keys:
        return {}
    debit_ids = {debit for debit, _ in keys.values()}
    watches = dict((await db.execute(
        select(RefundWatch.transaction_id, RefundWatch.credit_id).where(
            RefundWatch.transaction_id.in_(debit_ids), RefundWatch.user_id.in_({row.user_id for row in rows}))
    )).all())
    waiting = {credit for debit, credit in keys.values() if debit in watches and watches[debit] is None}
    existing = set()
    if waiting:
        existing = set(await db.scalars(select(Transaction.id).where(Transaction.id.in_(waiting))))
    resolutions = {}
    for notification_id, (debit, credit) in keys.items():
        if debit in watches and watches[debit] is None and credit in existing:
            resolutions[notification_id] = Resolution([
                NotificationAction(label="Yes", path=f"/api/v1/refunds/{debit}/accept/{credit}"),
                NotificationAction(label="No", path=f"/api/v1/refunds/{debit}/reject/{credit}"),
            ], None)
        elif debit in watches and watches[debit] == credit:
            resolutions[notification_id] = Resolution([], "Marked as refunded")
        else:
            resolutions[notification_id] = Resolution([], "No longer applies")
    return resolutions


# ---------- Daily reminder ----------

async def daily(db: AsyncSession, owner: User, today: dt.date) -> None:
    """jobs.register("refund_overdue", refunds.daily): one refund_overdue notification per open watch whose
    "expected by" date has passed, once per watch. Never raises; sends no email."""
    try:
        rows = (await db.execute(
            select(RefundWatch.id, RefundWatch.transaction_id, RefundWatch.expected_amount, RefundWatch.due_date,
                   Transaction.merchant)
            .join(Transaction, Transaction.id == RefundWatch.transaction_id)
            .where(RefundWatch.user_id == owner.id, RefundWatch.credit_id.is_(None), RefundWatch.due_date < today)
            .order_by(RefundWatch.due_date, RefundWatch.created_at)
        )).all()
        for watch_id, debit_id, expected, due_date, merchant in rows:
            label = _label(merchant)
            await create_once(
                db, owner.id, kind=OVERDUE_KIND, dedupe_key=f"refund-overdue:{watch_id}",
                title=f"Refund from {label} still pending" if label else "Refund still pending",
                body=f"{_money_text(Decimal(str(expected)))} expected by {_day_text(due_date)}.",
                link=f"/activity?txn={debit_id}",
            )
    except Exception:
        logger.exception("Refund reminder failed")
        await db.rollback()


# ---------- Delete cleanup ----------

async def release(db: AsyncSession, transaction_id: uuid.UUID) -> None:
    """Before deleting a transaction (main.delete_transaction, same commit; no commit here): a watched payment's
    watch goes (its confirmed credit is bumped, as its refund_for clears), and a confirmed credit's watch is open
    again (its payment is bumped)."""
    own = (await db.execute(select(RefundWatch.id, RefundWatch.credit_id)
                            .where(RefundWatch.transaction_id == transaction_id))).first()
    if own is not None:
        await _bump(db, [own.credit_id])
        await db.execute(delete(RefundWatch).where(RefundWatch.id == own.id))
    reopened = list(await db.scalars(select(RefundWatch.transaction_id).where(RefundWatch.credit_id == transaction_id)))
    if reopened:
        await db.execute(update(RefundWatch).where(RefundWatch.credit_id == transaction_id)
                         .values(credit_id=None, closed_at=None))
        await _bump(db, reopened)


# ---------- API ----------

class RefundWatchIn(BaseModel):
    transaction_id: uuid.UUID
    amount: Decimal = Field(max_digits=12, decimal_places=2)  # > 0 and <= the payment, checked in the route
    due_date: dt.date  # IST, 1 to 60 days after today


class RefundWatchOut(BaseModel):
    transaction_id: uuid.UUID
    expected_amount: str  # "2499.00"
    due_date: dt.date  # "2026-11-29"
    credit_id: Optional[uuid.UUID]  # the confirmed refund, once there is one
    state: Literal["expected", "refunded"]


def _out(watch: RefundWatch) -> RefundWatchOut:
    return RefundWatchOut(
        transaction_id=watch.transaction_id, expected_amount=_amount(Decimal(str(watch.expected_amount))),
        due_date=watch.due_date, credit_id=watch.credit_id, state=REFUNDED if watch.credit_id else EXPECTED,
    )


router = APIRouter(prefix="/api/v1/refunds", tags=["Refunds"])


async def _watch(db: AsyncSession, user: User, transaction_id: uuid.UUID) -> RefundWatch:
    watch = await db.scalar(select(RefundWatch).where(RefundWatch.transaction_id == transaction_id,
                                                      RefundWatch.user_id == user.id))
    if watch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return watch


@router.post("", response_model=RefundWatchOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(same_origin)])
async def mark(body: RefundWatchIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """"Expect a refund" on a payment. 404 when it isn't yours, 422 for a credit, an unread SMS, an amount that
    isn't more than zero or is more than the payment, or a date not 1 to 60 days from today; 409 when a refund
    is already expected (or was confirmed) for it."""
    debit = await db.scalar(select(Transaction).where(Transaction.id == body.transaction_id, ownership(user)))
    if debit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found.")
    if not debit.is_parsed or debit.type != "DEBIT":
        raise HTTPException(422, "Only payments can expect a refund.")
    paid = Decimal(str(debit.amount))
    if body.amount <= 0:
        raise HTTPException(422, "Enter a refund amount more than zero.")
    if body.amount > paid:
        raise HTTPException(422,
                            f"The refund can't be more than the payment ({_money_text(paid)}).")
    if not 1 <= (body.due_date - _today()).days <= MAX_DUE_DAYS:
        raise HTTPException(422,
                            f"Pick a date 1 to {MAX_DUE_DAYS} days from today.")
    if await db.scalar(select(RefundWatch.id).where(RefundWatch.transaction_id == debit.id)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_EXPECTED)
    watch = RefundWatch(user_id=user.id, transaction_id=debit.id, expected_amount=body.amount,
                        due_date=body.due_date, credit_id=None, created_at=_now())
    out = _out(watch)
    db.add(watch)
    try:
        await _bump(db, [debit.id])  # flushes the new row first
        await db.commit()
    except IntegrityError:
        await db.rollback()  # a simultaneous tap marked it first
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_EXPECTED)
    return out


@router.get("/{transaction_id}", response_model=RefundWatchOut)
async def get_watch(transaction_id: uuid.UUID, user: User = Depends(current_user),
                    db: AsyncSession = Depends(get_db)):
    """The refund expected for a payment; 404 when none is."""
    return _out(await _watch(db, user, transaction_id))


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def cancel(transaction_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """"No refund expected": deletes the watch, open or refunded; the confirmed credit is a plain credit again."""
    watch = await _watch(db, user, transaction_id)
    credit_id = watch.credit_id
    await db.delete(watch)
    await _bump(db, [transaction_id, credit_id])
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{transaction_id}/accept/{credit_id}", response_model=RefundWatchOut,
             dependencies=[Depends(same_origin)])
async def accept(transaction_id: uuid.UUID, credit_id: uuid.UUID, user: User = Depends(current_user),
                 db: AsyncSession = Depends(get_db)):
    """Yes: this credit is the payment's refund. 404 when no refund is expected or the credit isn't yours; 409
    when the refund is already confirmed or the credit is another payment's refund."""
    watch = await _watch(db, user, transaction_id)
    found = await db.scalar(select(Transaction.id).where(
        Transaction.id == credit_id, ownership(user), Transaction.is_parsed.is_(True), Transaction.type == "CREDIT"))
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credit not found.")
    if watch.credit_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This refund is already confirmed.")
    if await db.scalar(select(RefundWatch.id).where(RefundWatch.credit_id == credit_id)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, CREDIT_TAKEN)
    watch_id = watch.id
    out = _out(watch).model_copy(update={"credit_id": credit_id, "state": REFUNDED})
    try:
        # Only while still open: a Yes or a cancel that landed after the checks above updates nothing here.
        res = await db.execute(
            update(RefundWatch).where(RefundWatch.id == watch_id, RefundWatch.credit_id.is_(None))
            .values(credit_id=credit_id, closed_at=_now()).execution_options(synchronize_session=False))
        if res.rowcount == 0:
            await db.rollback()
            if await db.scalar(select(RefundWatch.id).where(RefundWatch.id == watch_id)) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)  # cancelled a moment ago
            raise HTTPException(status.HTTP_409_CONFLICT, "This refund is already confirmed.")
        await _bump(db, [transaction_id, credit_id])
        await db.commit()
    except IntegrityError:
        await db.rollback()  # the same credit was confirmed for another payment a moment ago
        raise HTTPException(status.HTTP_409_CONFLICT, CREDIT_TAKEN)
    return out


@router.post("/{transaction_id}/reject/{credit_id}", status_code=status.HTTP_204_NO_CONTENT,
             dependencies=[Depends(same_origin)])
async def reject(transaction_id: uuid.UUID, credit_id: uuid.UUID, user: User = Depends(current_user),
                 db: AsyncSession = Depends(get_db)):
    """No: deletes the suggestion, so it isn't shown again. The watch stays open. 204 also when it's gone."""
    ids = list(await db.scalars(select(Notification.id).where(
        Notification.user_id == user.id, Notification.dedupe_key == _match_key(transaction_id, credit_id))))
    if ids:
        await db.execute(delete(NotificationDismissal).where(NotificationDismissal.notification_id.in_(ids)))
        await db.execute(delete(Notification).where(Notification.id.in_(ids)))
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


register_actions(MATCH_KIND, refund_actions)
