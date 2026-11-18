"""Possible duplicates: a transaction that may be a second record of one already saved.

When a transaction is saved (an SMS or one added by hand), it's compared with your transactions saved
before it whose time is within 3 days, with the same amount, direction and currency:

1. "reference": the same bank reference number (UPI ref / UTR).
2. "same_details": within 10 minutes and the same merchant (any case or spacing); when either has no
   merchant, the same account instead.
3. "manual_entry": within 24 hours, one added by hand (bank, not cash) and the other not (the bank SMS).

A cash entry added by hand only ever matches another cash entry: against a bank record (an SMS or a bank
entry added by hand) rules 2 and 3 never apply, since cash can't be the same payment as a bank debit.

The newer one (saved later) is flagged and held out of every total (spending.not_held_out) until you
decide: delete it, or "Not a duplicate", after which it counts and is never flagged again. Deleting the
earlier one releases the newer one, which counts again.

Only rows saved earlier are candidates, so two SMS saved at the same moment can't flag each other: a race
can leave both counted, never both held out. Nothing is ever deleted automatically, and edits don't run
detection again.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import OWNER_EMAIL
from balance import CASH_ACCOUNT
from categorize import merchant_key
from dashboard import ownership
from limits import IST, _short_label, _utc, format_money
from models import DUPLICATE_DISMISSED, DUPLICATE_PENDING, DuplicateFlag, Transaction, User
from notifications import create_once
from spending import not_held_out

logger = logging.getLogger(__name__)

MANUAL = "manual"  # matches main.MANUAL (main imports this module)

LOOKBACK = timedelta(days=3)  # how far apart two transactions' times can be and still be compared
SAME_DETAILS = timedelta(minutes=10)  # rule 2
MANUAL_ENTRY = timedelta(hours=24)  # rule 3

REFERENCE = "reference"
SAME_DETAILS_REASON = "same_details"
MANUAL_ENTRY_REASON = "manual_entry"
_REASONS = {1: REFERENCE, 2: SAME_DETAILS_REASON, 3: MANUAL_ENTRY_REASON}


async def owner_for_webhook(db: AsyncSession) -> Optional[User]:
    """The dashboard owner, for SMS (they arrive without a session). None when none is configured."""
    if not OWNER_EMAIL:
        return None
    return await db.scalar(select(User).where(User.email == OWNER_EMAIL))


def _text(value: Optional[str]) -> str:
    return (value or "").strip()


def _hand_added_bank(parsed_by: Optional[str], account_ref: Optional[str]) -> bool:
    return parsed_by == MANUAL and account_ref != CASH_ACCOUNT


def _cash(parsed_by: Optional[str], account_ref: Optional[str]) -> bool:
    return parsed_by == MANUAL and account_ref == CASH_ACCOUNT


def _rule(txn: Transaction, row, delta: timedelta) -> Optional[int]:
    """1, 2 or 3 per the module docstring, or None."""
    reference = _text(txn.reference_id)
    if reference and reference == _text(row.reference_id):
        return 1
    if _cash(txn.parsed_by, txn.account_ref) != _cash(row.parsed_by, row.account_ref):
        return None  # cash against a bank record: never the same payment
    if delta <= SAME_DETAILS:
        mine, theirs = merchant_key(txn.merchant), merchant_key(row.merchant)
        if mine and theirs:
            if mine == theirs:
                return 2
        else:  # a merchant is missing: the same account instead
            account = _text(txn.account_ref)
            if account and account == _text(row.account_ref):
                return 2
    if delta <= MANUAL_ENTRY:
        # Exactly one side added by hand (bank, not cash), the other not: two hand entries never match.
        if ((_hand_added_bank(txn.parsed_by, txn.account_ref) and row.parsed_by != MANUAL)
                or (_hand_added_bank(row.parsed_by, row.account_ref) and txn.parsed_by != MANUAL)):
            return 3
    return None


async def find_original(db: AsyncSession, owner: User, txn: Transaction) -> Optional[tuple[uuid.UUID, str]]:
    """(original id, reason) when `txn` looks like a second record of a transaction saved before it.
    Call it after a flush, so `txn` has its id."""
    if not txn.is_parsed or txn.amount is None or txn.amount <= 0:
        return None  # unreadable SMS are already in Needs review
    if await db.get(DuplicateFlag, txn.id) is not None:
        return None  # flagged already, or "Not a duplicate": never flagged again
    when = _utc(txn.transaction_time)
    saved = _utc(txn.created_at) if txn.created_at is not None else datetime.now(timezone.utc)
    rows = (await db.execute(
        select(Transaction.id, Transaction.merchant, Transaction.account_ref, Transaction.reference_id,
               Transaction.parsed_by, Transaction.transaction_time, Transaction.created_at).where(
            ownership(owner),
            Transaction.is_parsed.is_(True),
            Transaction.currency == txn.currency,
            Transaction.type == txn.type,
            Transaction.amount == txn.amount,
            Transaction.created_at < saved,
            Transaction.transaction_time >= when - LOOKBACK,
            Transaction.transaction_time <= when + LOOKBACK,
            Transaction.id != txn.id,
            not_held_out(),
        )
    )).all()
    best = None
    for row in rows:
        delta = abs(when - _utc(row.transaction_time))
        rule = _rule(txn, row, delta)
        if rule is None:
            continue
        # The lowest rule, then the closest in time, then the most recently saved.
        key = (rule, delta, -_utc(row.created_at).timestamp())
        if best is None or key < best[0]:
            best = (key, row.id, _REASONS[rule])
    return (best[1], best[2]) if best else None


async def flag_if_duplicate(db: AsyncSession, owner: User, txn: Transaction) -> Optional[tuple[uuid.UUID, str]]:
    """After `db.flush()` and before the commit, so the flag is saved with the transaction. A bug in the
    check never loses the transaction: it's logged and the transaction is saved unflagged."""
    try:
        found = await find_original(db, owner, txn)
    except Exception:
        logger.exception("Duplicate check failed")
        return None
    if found:
        db.add(DuplicateFlag(transaction_id=txn.id, original_id=found[0], user_id=owner.id, reason=found[1],
                             status=DUPLICATE_PENDING, created_at=datetime.now(timezone.utc)))
    return found


def _clock(moment: datetime, relative_to: datetime) -> str:
    """IST "10:02 am", plus " on 20 Oct" when it's on a different IST day from `relative_to`."""
    when = _utc(moment).astimezone(IST)
    clock = f"{when:%I:%M}".lstrip("0") + (" am" if when.hour < 12 else " pm")
    if when.date() != _utc(relative_to).astimezone(IST).date():
        clock += f" on {when.day} {when:%b}"
    return clock


async def notify(db: AsyncSession, owner_id: uuid.UUID, txn: Transaction, found: tuple[uuid.UUID, str]) -> None:
    """The bell notification for a flagged transaction. After the commit; never raises."""
    try:
        txn_id, amount, currency, kind = txn.id, txn.amount, txn.currency, txn.type
        merchant, when = txn.merchant, txn.transaction_time
        original_id, reason = found
        original = (await db.execute(
            select(Transaction.amount, Transaction.merchant, Transaction.transaction_time, Transaction.parsed_by)
            .where(Transaction.id == original_id)
        )).first()
        if original is None:
            return
        credit = kind == "CREDIT"
        noun = "credit" if credit else "payment"
        clock = _clock(original.transaction_time, when)
        title = f"Possible duplicate: {format_money(amount, currency)}"
        if merchant_key(merchant):
            title += f" {'from' if credit else 'at'} {_short_label(merchant)}"
        if reason == REFERENCE:
            body = f"Same reference number as the {noun} at {clock}"
        elif reason == SAME_DETAILS_REASON:
            what = "merchant" if merchant_key(merchant) and merchant_key(original.merchant) else "account"
            body = f"Same amount and {what} as the {noun} at {clock}"
        elif original.parsed_by == MANUAL:
            body = f"Matches the {format_money(original.amount, currency)} you added by hand at {clock}"
        else:
            body = f"Matches the bank SMS for {format_money(original.amount, currency)} at {clock}"
        await create_once(db, owner_id, kind="duplicate", title=title, body=body, link="/activity?kind=review",
                          dedupe_key=f"dup:{txn_id}")
    except Exception:
        logger.exception("Duplicate notification failed")
        await db.rollback()


async def release(db: AsyncSession, transaction_id: uuid.UUID) -> list[uuid.UUID]:
    """Before deleting a transaction: the transactions held out as its duplicates count again (their
    `updated_at` moves, so the app sees it), and every flag row naming it goes. No commit; returns the
    released ids, for their limit checks."""
    released = list(await db.scalars(select(DuplicateFlag.transaction_id).where(
        DuplicateFlag.original_id == transaction_id, DuplicateFlag.status == DUPLICATE_PENDING)))
    if released:
        await db.execute(update(Transaction).where(Transaction.id.in_(released))
                         .values(updated_at=datetime.now(timezone.utc)))
    await db.execute(delete(DuplicateFlag).where(
        or_(DuplicateFlag.transaction_id == transaction_id, DuplicateFlag.original_id == transaction_id)))
    return released


async def dismiss(db: AsyncSession, user: User, transaction_id: uuid.UUID) -> Transaction:
    """"Not a duplicate": the transaction counts again and is never flagged again. 404 when it isn't
    yours, 409 when it isn't a pending duplicate (also for a second tap)."""
    txn = await db.scalar(select(Transaction).where(Transaction.id == transaction_id, ownership(user)))
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found.")
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(DuplicateFlag)
        .where(DuplicateFlag.transaction_id == transaction_id, DuplicateFlag.status == DUPLICATE_PENDING)
        .values(status=DUPLICATE_DISMISSED, decided_at=now)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Not a pending duplicate.")
    txn.updated_at = now
    await db.commit()
    return txn
