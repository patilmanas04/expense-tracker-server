"""Self-transfer suggestions (count_real_spending.md Phase 3): money you moved between two of your own accounts,
offered as a notification with Confirm / Not a self transfer. Nothing changes until you tap Confirm.

**Rule**: a bank-account debit and a credit of the same amount into a different account of yours, within 30
minutes of each other either way (inclusive), look like one transfer. The closest in time wins; a tie gives no
suggestion. Each leg is checked when its SMS is saved (post-save check `check`, cause sms only), so the second leg
finds the first.

**Eligible leg** (`_eligible`, pure Python, and the same conditions in `_partner`'s query):
* parsed, DEBIT or CREDIT, INR, more than zero;
* from an SMS: parsed_by not "manual" (added by hand) or "edited" (an SMS whose amount you typed), so the parser
  supplied amount, type and account;
* an account number that isn't "Cash";
* category not Credit Card Payment, Cash Withdrawal or Investments & Finance (NOT_PAIRABLE). Cash Withdrawal is one
  of two stricter rules from gated_steps_decision.md, step 20: an ATM withdrawal is never a transfer to another of
  your accounts;
* the SMS changes a bank balance (balance.affects_bank_balance) AND names a bank account ("A/c", "Acct", "Acc",
  "Account": parser.BANK_ACCOUNT_RE, outside card wording). That is the other stricter rule: debit-card alerts
  ("Spent Rs.… From HDFC Bank Card x5566") pass affects_bank_balance, but their account number is the card's, so a
  card purchase and a credit to the account behind the card would look like two different accounts;
* not a possible duplicate waiting for your decision (spending.not_held_out; the runner skips a held-out leg);
* not already in a pending or confirmed pair.

**Candidates** (`_partner`, one query): the owner's eligible legs of the opposite type with the same amount, a
different account number and a time within the window, never a (debit, credit) that already has a pair row in any
status (a rejected or expired pair is never suggested again), and not when both legs are already Self Transfer.

**Lifecycle** (`transfer_pairs`): the pair (id set in code) and its notification (kind self_transfer, dedupe
"pair:<id>") are written in one commit through create_once; if the other leg's task wrote the same pair first, the
unique key fails that commit and both roll back. pending → confirmed (Confirm: both legs re-checked, then both
become Self Transfer, updated_at bumped, no merchant memory) / rejected (Not a self transfer: the legs are
unchanged and each is re-checked for another partner in the background) / expired (Confirm's re-check failed).

**Resolver** (register_actions("self_transfer")): Confirm / Not a self transfer while pending; "Marked as self
transfer", "Not a self transfer", or "No longer applies" (expired, or the pair is gone).

**Delete** (`release`, from main.delete_transaction, same commit): deleting either leg deletes its pairs, in any
status; the other leg keeps its category.

**Cost**: 0 statements for an ineligible leg, 1 query for an eligible one, and on a suggestion create_once's select
plus one commit; the resolver 1 statement per inbox read; Confirm at most 5 statements and one commit.

Pairing is live but not yet checked against a real HDFC/ICICI pair (REAL_PAIRS empty in tests/test_transfers.py).
This module never imports main.
"""
import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Literal, NamedTuple, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import case, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import post_save
from auth import current_user, same_origin
from balance import CASH_ACCOUNT, affects_bank_balance
from dashboard import ownership
from database import AsyncSessionLocal, get_db
from limits import IST, _utc, format_money
from models import Notification, Transaction, TransferPair, User
from notifications import NotificationAction, Resolution, create_once, register_actions
from parser import BANK_ACCOUNT_RE, card_text
from post_save import PostSave
from spending import INVESTED, not_held_out

logger = logging.getLogger(__name__)

KIND = "self_transfer"
WINDOW = dt.timedelta(minutes=30)  # inclusive, either way; never wider (gated_steps_decision.md, step 20)
PENDING, CONFIRMED, REJECTED, EXPIRED = "pending", "confirmed", "rejected", "expired"
ACTIVE = (PENDING, CONFIRMED)  # a leg in one of these is never offered again
SELF_TRANSFER = "Self Transfer"
NOT_PAIRABLE = frozenset({"Credit Card Payment", "Cash Withdrawal"}) | INVESTED
HAND_ENTERED = ("manual", "edited")  # main.MANUAL / main.EDITED (not imported: this module never imports main)
RULE_SET, PICKED = "regex+rule", "regex+user"  # parsed_by: a category rule set the category / you picked it
TITLE = "Looks like a transfer between your accounts"
NOT_FOUND = "Suggestion not found."
ANSWERED = "This suggestion was already answered."
NO_LONGER = "This suggestion no longer applies."


class Leg(NamedTuple):
    id: uuid.UUID
    type: str
    amount: Decimal
    account_ref: str
    category: Optional[str]
    transaction_time: dt.datetime  # UTC-aware


def _now() -> dt.datetime:
    # Real time, not limits.utc_now: the app's polling cursor must always move forward.
    return dt.datetime.now(dt.timezone.utc)


def _names_bank_account(raw: Optional[str]) -> bool:
    # Debit-card alerts pass affects_bank_balance, but their account_ref is the card number
    # (gated_steps_decision.md, step 20).
    return bool(raw) and affects_bank_balance(raw) and BANK_ACCOUNT_RE.search(card_text(raw)) is not None


def _category_can_pair(category: Optional[str]) -> bool:
    """None and Uncategorized are fine; card payments, ATM withdrawals and investments never pair."""
    return category not in NOT_PAIRABLE


def _eligible(txn: Transaction) -> bool:
    """Can this saved transaction be one leg of a transfer? Pure Python: no statements."""
    return bool(
        txn.is_parsed
        and txn.type in ("DEBIT", "CREDIT")
        and txn.currency == "INR"
        and txn.amount is not None and txn.amount > 0
        and txn.parsed_by not in HAND_ENTERED
        and txn.account_ref and txn.account_ref != CASH_ACCOUNT
        and _category_can_pair(txn.category)
        and _names_bank_account(txn.raw_message)
    )


def _hhmm(moment: dt.datetime) -> str:
    return _utc(moment).astimezone(IST).strftime("%H:%M")


def _body(debit: Leg, credit: Leg) -> str:
    """"₹10,000.00 from ICICI XX310 at 10:02 to HDFC XX4821 at 10:04"."""
    return (f"{format_money(debit.amount, 'INR')} from {debit.account_ref} at {_hhmm(debit.transaction_time)} "
            f"to {credit.account_ref} at {_hhmm(credit.transaction_time)}")


def _key(pair_id) -> str:
    return f"pair:{pair_id}"


def _parse_key(key: Optional[str]) -> Optional[uuid.UUID]:
    prefix, _, rest = (key or "").partition(":")
    if prefix != "pair" or not rest:
        return None
    try:
        return uuid.UUID(rest)
    except ValueError:
        return None


def _leg(row) -> Leg:
    """One shape for the saved transaction (ctx.txn) and the candidate rows."""
    return Leg(row.id, row.type, Decimal(str(row.amount)), row.account_ref, row.category,
               _utc(row.transaction_time))


# ---------- Suggest (post-save) ----------

async def _partner(db: AsyncSession, owner: User, leg: Leg) -> Optional[Leg]:
    """The one leg this one most likely transferred to or from, or None (none, or a tie). Exactly 1 statement."""
    opposite = "CREDIT" if leg.type == "DEBIT" else "DEBIT"
    leg_in_active = exists().where(TransferPair.status.in_(ACTIVE),
                                   or_(TransferPair.debit_id == leg.id, TransferPair.credit_id == leg.id))
    cand_in_active = exists().where(TransferPair.status.in_(ACTIVE),
                                    or_(TransferPair.debit_id == Transaction.id,
                                        TransferPair.credit_id == Transaction.id))
    debit_col, credit_col = (leg.id, Transaction.id) if leg.type == "DEBIT" else (Transaction.id, leg.id)
    # Any status: a rejected or expired pair is never suggested again.
    already_paired = exists().where(TransferPair.debit_id == debit_col, TransferPair.credit_id == credit_col)
    rows = (await db.execute(
        select(Transaction.id, Transaction.type, Transaction.amount, Transaction.account_ref, Transaction.category,
               Transaction.transaction_time, Transaction.raw_message)
        .where(ownership(owner), Transaction.id != leg.id, Transaction.type == opposite,
               Transaction.is_parsed.is_(True), Transaction.currency == "INR", Transaction.amount == leg.amount,
               Transaction.parsed_by.not_in(HAND_ENTERED),
               Transaction.account_ref.is_not(None), Transaction.account_ref != CASH_ACCOUNT,
               Transaction.account_ref != leg.account_ref,
               or_(Transaction.category.is_(None), Transaction.category.not_in(sorted(NOT_PAIRABLE))),
               Transaction.transaction_time >= leg.transaction_time - WINDOW,
               Transaction.transaction_time <= leg.transaction_time + WINDOW,
               not_held_out(), ~cand_in_active, ~already_paired, ~leg_in_active)
    )).all()
    best: list[tuple[dt.timedelta, Leg]] = []
    for row in rows:
        if not _names_bank_account(row.raw_message):
            continue
        gap = abs(_utc(row.transaction_time) - leg.transaction_time)
        if gap > WINDOW:  # authoritative: SQLite compares datetimes as text
            continue
        if leg.category == SELF_TRANSFER and row.category == SELF_TRANSFER:
            continue
        best.append((gap, _leg(row)))
    if not best:
        return None
    closest = min(gap for gap, _ in best)
    winners = [candidate for gap, candidate in best if gap == closest]
    return winners[0] if len(winners) == 1 else None  # a tie gives no suggestion


async def _suggest(db: AsyncSession, owner_id: uuid.UUID, debit: Leg, credit: Leg) -> bool:
    """The pending pair and its notification, in one commit. False when the same pair was written first."""
    pair_id = uuid.uuid4()
    # Not flushed yet (autoflush is off): create_once's commit writes it with the notification, and a unique-key
    # failure there rolls both back.
    db.add(TransferPair(id=pair_id, user_id=owner_id, debit_id=debit.id, credit_id=credit.id, status=PENDING,
                        created_at=_now()))
    return await create_once(db, owner_id, kind=KIND, title=TITLE, body=_body(debit, credit), link=None,
                             dedupe_key=_key(pair_id))


async def check(ctx: PostSave) -> None:
    """post_save.register("transfers", transfers.check, {"sms"}). The runner already skipped a missing owner,
    another user's row and a held-out row, and catches anything raised here."""
    txn = ctx.txn  # detached: read freely
    if not _eligible(txn):
        return
    leg = _leg(txn)
    partner = await _partner(ctx.db, ctx.owner, leg)
    if partner is None:
        return
    debit, credit = (leg, partner) if leg.type == "DEBIT" else (partner, leg)
    await _suggest(ctx.db, ctx.owner.id, debit, credit)


async def recheck(transaction_id: uuid.UUID) -> None:
    """Background task after Not a self transfer: this leg may pair with another partner. Never raises."""
    try:
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, transaction_id, "sms")
            if ctx is not None:
                await check(ctx)
    except Exception:
        logger.exception("Self-transfer re-check failed")


# ---------- Delete cleanup ----------

async def release(db: AsyncSession, transaction_id: uuid.UUID) -> None:
    """Before deleting a transaction (main.delete_transaction, same commit; no commit here): every pair it is a leg
    of goes, in any status. The other leg keeps its category."""
    await db.execute(delete(TransferPair).where(
        or_(TransferPair.debit_id == transaction_id, TransferPair.credit_id == transaction_id)))


# ---------- Notification buttons ----------

RESOLVED = {CONFIRMED: "Marked as self transfer", REJECTED: "Not a self transfer"}
GONE = "No longer applies"


async def pair_actions(db: AsyncSession, rows: list[Notification]) -> dict[uuid.UUID, Resolution]:
    """Confirm / Not a self transfer, or the text shown once decided, per self_transfer notification. At most 1
    statement."""
    keys = {row.id: pair_id for row in rows if (pair_id := _parse_key(row.dedupe_key)) is not None}
    if not keys:
        return {}
    statuses = dict((await db.execute(
        select(TransferPair.id, TransferPair.status).where(
            TransferPair.id.in_(set(keys.values())), TransferPair.user_id.in_({row.user_id for row in rows}))
    )).all())
    resolutions = {}
    for notification_id, pair_id in keys.items():
        state = statuses.get(pair_id)
        if state == PENDING:
            resolutions[notification_id] = Resolution([
                NotificationAction(label="Confirm", path=f"/api/v1/transfers/pairs/{pair_id}/confirm"),
                NotificationAction(label="Not a self transfer", path=f"/api/v1/transfers/pairs/{pair_id}/reject"),
            ], None)
        else:
            resolutions[notification_id] = Resolution([], RESOLVED.get(state, GONE))
    return resolutions


# ---------- API ----------

class TransferPairOut(BaseModel):
    id: uuid.UUID
    status: Literal["pending", "confirmed", "rejected", "expired"]
    debit_id: uuid.UUID
    credit_id: uuid.UUID
    decided_at: Optional[dt.datetime]  # UTC


router = APIRouter(prefix="/api/v1/transfers", tags=["Transfers"])


async def _pair(db: AsyncSession, user: User, pair_id: uuid.UUID) -> TransferPair:
    pair = await db.scalar(select(TransferPair).where(TransferPair.id == pair_id, TransferPair.user_id == user.id))
    if pair is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return pair


async def _still_applies(db: AsyncSession, user: User, pair_id: uuid.UUID, debit_id: uuid.UUID,
                         credit_id: uuid.UUID) -> bool:
    """Confirm's re-check: both legs are still yours, parsed, a DEBIT and a CREDIT of the same amount and currency,
    neither in a category that never pairs, and neither in another confirmed pair. At most 2 statements."""
    legs = {row.id: row for row in (await db.execute(
        select(Transaction.id, Transaction.type, Transaction.amount, Transaction.currency, Transaction.is_parsed,
               Transaction.category)
        .where(Transaction.id.in_([debit_id, credit_id]), ownership(user))
    )).all()}
    debit, credit = legs.get(debit_id), legs.get(credit_id)
    if debit is None or credit is None:
        return False
    if not (debit.is_parsed and credit.is_parsed) or debit.type != "DEBIT" or credit.type != "CREDIT":
        return False
    if debit.amount is None or credit.amount is None:
        return False
    if Decimal(str(debit.amount)) != Decimal(str(credit.amount)) or debit.currency != credit.currency:
        return False
    if not (_category_can_pair(debit.category) and _category_can_pair(credit.category)):
        return False
    other = await db.scalar(select(TransferPair.id).where(
        TransferPair.status == CONFIRMED, TransferPair.id != pair_id,
        or_(TransferPair.debit_id.in_([debit_id, credit_id]), TransferPair.credit_id.in_([debit_id, credit_id])))
        .limit(1))
    return other is None


def _picked_by_you():
    """parsed_by for a row whose category you just set: a rule's "regex+rule" becomes "regex+user"; others keep theirs."""
    return case((Transaction.parsed_by == RULE_SET, PICKED), else_=Transaction.parsed_by)


def _decide(pair_id: uuid.UUID, new_status: str, now: dt.datetime):
    """Moves a pair on, only while it is still pending: a decision that landed first wins."""
    return (update(TransferPair).where(TransferPair.id == pair_id, TransferPair.status == PENDING)
            .values(status=new_status, decided_at=now).execution_options(synchronize_session=False))


@router.post("/pairs/{pair_id}/confirm", response_model=TransferPairOut, dependencies=[Depends(same_origin)])
async def confirm(pair_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Confirm: both legs become Self Transfer and stop counting. 404 when the suggestion isn't yours; 409 when it
    was already answered, or when a leg changed so it no longer applies (the suggestion is then expired)."""
    pair = await _pair(db, user, pair_id)
    pair_id, debit_id, credit_id, state = pair.id, pair.debit_id, pair.credit_id, pair.status
    if state != PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, ANSWERED)
    now = _now()
    if not await _still_applies(db, user, pair_id, debit_id, credit_id):
        res = await db.execute(_decide(pair_id, EXPIRED, now))
        await db.commit()  # before raising: get_db rolls back on an exception
        raise HTTPException(status.HTTP_409_CONFLICT, NO_LONGER if res.rowcount else ANSWERED)
    res = await db.execute(_decide(pair_id, CONFIRMED, now))
    if res.rowcount == 0:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, ANSWERED)
    # No merchant memory: a transfer between your own accounts says nothing about the next payment to that name.
    # A leg a category rule set is now your choice, not the rule's ("regex+rule" becomes "regex+user", as on an edit).
    await db.execute(update(Transaction).where(Transaction.id.in_([debit_id, credit_id]))
                     .values(category=SELF_TRANSFER, parsed_by=_picked_by_you(), updated_at=now)
                     .execution_options(synchronize_session=False))
    await db.commit()
    return TransferPairOut(id=pair_id, status=CONFIRMED, debit_id=debit_id, credit_id=credit_id, decided_at=now)


@router.post("/pairs/{pair_id}/reject", response_model=TransferPairOut, dependencies=[Depends(same_origin)])
async def reject(pair_id: uuid.UUID, background_tasks: BackgroundTasks, user: User = Depends(current_user),
                 db: AsyncSession = Depends(get_db)):
    """Not a self transfer: both legs keep counting, this pair is never suggested again, and each leg is checked
    again for another partner. 404 when the suggestion isn't yours; 409 when it was already answered."""
    pair = await _pair(db, user, pair_id)
    pair_id, debit_id, credit_id, state = pair.id, pair.debit_id, pair.credit_id, pair.status
    if state != PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, ANSWERED)
    now = _now()
    res = await db.execute(_decide(pair_id, REJECTED, now))
    if res.rowcount == 0:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, ANSWERED)
    await db.commit()
    background_tasks.add_task(recheck, debit_id)
    background_tasks.add_task(recheck, credit_id)
    return TransferPairOut(id=pair_id, status=REJECTED, debit_id=debit_id, credit_id=credit_id, decided_at=now)


register_actions(KIND, pair_actions)
