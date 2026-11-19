"""Lend & borrow: splits and loans with people, and who owes whom.

**Entries** (`ledger_entries`): money between you and a person, either linked to a parsed INR transaction (its share
of that payment or credit) or a cash entry with its own day. Kinds and signs (models.LEDGER_KINDS):

| Kind           | On                     | Sign | Example                                  |
|----------------|------------------------|------|------------------------------------------|
| `lent`         | a payment, or cash     | +1   | Rahul's ₹600 of the ₹1,800 dinner you paid |
| `repaid_by_me` | a payment, or cash     | +1   | you paid back Priya                      |
| `borrowed`     | money received, or cash | −1  | Priya lent you ₹2,000                    |
| `repaid_to_me` | money received, or cash | −1  | Rahul paid you back ₹600                 |

A person's balance is Σ sign × amount; positive means they owe you, negative that you owe them. People are grouped
by `person_key` (spaces collapsed, case folded), shown with the spelling of their newest entry. A new entry for a
person you already have takes their spelling ("rahul " is saved as "Rahul"), so typing a name in another case never
relabels their history; a person's only entries, replaced in one PUT, can still be renamed.

**Counted amount** (spending.py): a transaction counts its amount minus its linked entries, in every total, limit
and alert (`linked_totals()` / `counted_amount()` in SQL; `Transaction.linked_amount` on each row). Lending ₹1,200
of an ₹1,800 dinner counts ₹600 as spending; a ₹600 repayment isn't income. The category isn't touched and nothing
is written to merchant memory.

**Lifecycle**: `PUT /transactions/{id}/ledger` replaces a transaction's entries (0–20, Σ at most its amount) and
bumps its `updated_at` so the app's poll sees the new `linked_amount`; on a payment it re-runs the spend-limit check
(removing a share raises spending). Deleting a linked entry does the same. Deleting the transaction deletes its
entries (`release`, from main.delete_transaction); editing it can't change its type, or bring its amount below its
entries, while it has any (409 in main.edit_transaction). Cash entries are never touched by transactions.

**Races**: PUT and main.edit_transaction first lock the transaction's row (`lock_transaction`), so two saves at once
run in turn and the second replaces the first; Σ ≤ amount is checked under the lock.

**Cost**: GET /ledger is one query; POST adds one query for the person's spelling; PUT locks and loads the
transaction, deletes, looks up spellings and inserts its entries in one commit, then reloads it and its entries.
"""
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import limits
import merchants
from auth import current_user, same_origin
from dashboard import ownership
from database import get_db
from limits import IST, _encodable, _utc
from models import CREDIT_KINDS, DEBIT_KINDS, LEDGER_KINDS, LedgerEntry, Transaction, User

router = APIRouter(prefix="/api/v1", tags=["Lend & borrow"])

MAX_ENTRIES = 20  # per transaction
MAX_PERSON = 60  # String(60)
MAX_NOTE = 200  # String(200)
MAX_AMOUNT = Decimal("9999999999.99")  # Numeric(12, 2)
CENTS = Decimal("0.01")

Kind = Literal["lent", "repaid_by_me", "borrowed", "repaid_to_me"]


def utc_now() -> datetime:
    # Real time (patched in tests): updated_at moves the app's polling cursor, so it must move forward.
    return datetime.now(timezone.utc)


def today_ist() -> date:
    """Today in India: a cash entry can't be dated after it."""
    return limits.utc_now().astimezone(IST).date()


def person_key(name: str) -> str:
    """How people are grouped: spaces collapsed, case folded ("Rahul" and "rahul " are one person). Not
    categorize.merchant_key, which treats "Unknown" as no name."""
    return " ".join(name.split()).casefold()


# ---------- Validation ----------

class EntryIn(BaseModel):
    person: str = Field(description="Who: up to 60 characters")
    kind: Kind
    amount: Decimal = Field(description="More than zero, at most 2 decimals")
    note: Optional[str] = Field(default=None, description="Up to 200 characters; blank = none")

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        return _encodable(data)

    @field_validator("person")
    @classmethod
    def _person(cls, value: str) -> str:
        if merchants.has_invalid_characters(value):
            raise ValueError("Names can't contain invalid characters.")
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter a name.")
        # The key too: case-folding can lengthen a name ("ß" → "ss"), and person_key is String(60) as well.
        if len(value) > MAX_PERSON or len(person_key(value)) > MAX_PERSON:
            raise ValueError(f"Names can be up to {MAX_PERSON} characters.")
        return value

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value) -> Decimal:
        # One message for every bad amount, as rules._min_amount does.
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("Enter an amount above zero.")
        try:
            amount = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError("Enter an amount above zero.")
        if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT or amount != amount.quantize(CENTS):
            raise ValueError("Enter an amount above zero.")
        return amount.quantize(CENTS)

    @field_validator("note")
    @classmethod
    def _note(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if merchants.has_invalid_characters(value):
            raise ValueError("Notes can't contain invalid characters.")
        value = value.strip()
        if not value:
            return None
        if len(value) > MAX_NOTE:
            raise ValueError(f"Notes can be up to {MAX_NOTE} characters.")
        return value


class CashEntryIn(EntryIn):
    day: date = Field(description="IST date, today or earlier")

    @field_validator("day")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > today_ist():
            raise ValueError("The date can't be in the future.")
        return value


class LinkedEntriesIn(BaseModel):
    entries: list[EntryIn] = Field(description="Every entry for the transaction, 0 to 20; [] removes them all")

    @model_validator(mode="before")
    @classmethod
    def _mark_invalid_characters(cls, data):
        # Before the length check, whose 422 echoes the whole list: a lone surrogate in it couldn't be encoded.
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            entries = [merchants.mark_invalid_characters(item) if isinstance(item, str) else _encodable(item)
                       for item in data["entries"]]
            return {**data, "entries": entries}
        return _encodable(data)

    @field_validator("entries", mode="before")
    @classmethod
    def _at_most(cls, value):
        if isinstance(value, list) and len(value) > MAX_ENTRIES:
            raise ValueError(f"Up to {MAX_ENTRIES} entries per payment.")
        return value


class EntryOut(BaseModel):
    id: uuid.UUID
    person: str
    kind: Kind
    amount: float
    day: date  # the cash entry's day, or the linked transaction's IST date
    transaction_id: Optional[uuid.UUID]
    merchant: Optional[str]  # the linked transaction's merchant; None for cash
    note: Optional[str]


class PersonOut(BaseModel):
    person: str  # spelling of the newest entry
    key: str
    balance: float  # positive = they owe you
    entries: list[EntryOut]  # newest first


class LedgerOut(BaseModel):
    people: list[PersonOut]


class LinkedOut(BaseModel):
    linked_amount: float
    updated_at: datetime  # the transaction's, aware UTC
    entries: list[EntryOut]  # oldest first


def _out(entry: LedgerEntry, txn_time: Optional[datetime], merchant: Optional[str]) -> EntryOut:
    day = entry.day if entry.transaction_id is None or txn_time is None else _utc(txn_time).astimezone(IST).date()
    return EntryOut(id=entry.id, person=entry.person, kind=entry.kind, amount=float(entry.amount), day=day,
                    transaction_id=entry.transaction_id, merchant=merchant if entry.transaction_id else None,
                    note=entry.note)


# ---------- Helpers ----------

async def release(db: AsyncSession, transaction_id: uuid.UUID) -> None:
    """Before deleting a transaction (main.delete_transaction, same commit; no commit here): its entries go too.
    Cash entries are never linked, so they stay."""
    await db.execute(delete(LedgerEntry).where(LedgerEntry.transaction_id == transaction_id))


async def _owned_txn(db: AsyncSession, user: User, transaction_id: uuid.UUID) -> Transaction:
    txn = await db.scalar(select(Transaction).where(Transaction.id == transaction_id, ownership(user)))
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found.")
    return txn


async def lock_transaction(db: AsyncSession, user: User, transaction_id: uuid.UUID) -> Optional[Transaction]:
    """Takes the transaction's row lock for the rest of this commit, then loads it as it now is (None when it's
    gone or not yours). Saving its entries and editing it (main.edit_transaction) both start here, so two at once
    (a double-tapped Save, a retry, an amount edit) run one after the other: the second waits, then sees the first's
    entries and amount. The UPDATE is the lock (a row lock on Postgres and TiDB, the write lock on SQLite); the
    SELECT is FOR UPDATE so TiDB and MySQL read the current row rather than the transaction's snapshot."""
    mine = (Transaction.id == transaction_id, ownership(user))
    await db.execute(update(Transaction).where(*mine).values(updated_at=utc_now())
                     .execution_options(synchronize_session=False))
    return await db.scalar(select(Transaction).where(*mine).with_for_update()
                           .execution_options(populate_existing=True))


async def _linked_entries(db: AsyncSession, user: User, txn: Transaction) -> list[EntryOut]:
    rows = await db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_id == txn.id,
                                                      LedgerEntry.user_id == user.id)
                            .order_by(LedgerEntry.created_at, LedgerEntry.id))
    return [_out(entry, txn.transaction_time, txn.merchant) for entry in rows]


async def _spellings(db: AsyncSession, user: User, names: list[str]) -> list[str]:
    """Each name in the spelling its person already has (their newest entry's), else as typed; the first of several
    new spellings for one person in `names` wins. One query."""
    keys = {person_key(name) for name in names}
    known: dict[str, str] = {}
    if keys:
        rows = await db.execute(select(LedgerEntry.person_key, LedgerEntry.person)
                                .where(LedgerEntry.user_id == user.id, LedgerEntry.person_key.in_(keys))
                                .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()))
        for key, person in rows:
            known.setdefault(key, person)
    return [known.setdefault(person_key(name), name) for name in names]


# ---------- API ----------

@router.get("/ledger", response_model=LedgerOut)
async def get_ledger(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Everyone you have entries with. People who owe or are owed come first, the largest amount first (ties by
    name); then settled people, the most recent first. Each person's entries newest first. One query."""
    rows = (await db.execute(
        select(LedgerEntry, Transaction.transaction_time, Transaction.merchant)
        .outerjoin(Transaction, Transaction.id == LedgerEntry.transaction_id)
        .where(LedgerEntry.user_id == user.id)
    )).all()
    groups: dict[str, list[tuple[EntryOut, datetime]]] = defaultdict(list)
    for entry, txn_time, merchant in rows:
        groups[entry.person_key].append((_out(entry, txn_time, merchant), _utc(entry.created_at)))
    people = []
    for key, items in groups.items():
        items.sort(key=lambda item: (item[0].day, item[1]), reverse=True)
        balance = sum((LEDGER_KINDS[out.kind] * Decimal(str(out.amount)) for out, _ in items), Decimal(0))
        newest = (items[0][0].day, items[0][1])
        people.append((PersonOut(person=items[0][0].person, key=key, balance=float(balance),
                                 entries=[out for out, _ in items]), balance, newest))
    open_people = sorted((p for p in people if p[1] != 0), key=lambda p: (-abs(p[1]), p[0].person.casefold(), p[0].key))
    settled = sorted((p for p in people if p[1] == 0), key=lambda p: p[2], reverse=True)
    return LedgerOut(people=[p[0] for p in open_people + settled])


@router.post("/ledger", response_model=EntryOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(same_origin)])
async def add_cash_entry(body: CashEntryIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """A cash entry: money given or received in person, or "Settle up". Any of the four kinds; dated today or
    earlier (IST)."""
    [person] = await _spellings(db, user, [body.person])
    entry = LedgerEntry(user_id=user.id, person=person, person_key=person_key(person), kind=body.kind,
                        amount=body.amount, day=body.day, transaction_id=None, note=body.note, created_at=utc_now())
    db.add(entry)
    await db.commit()
    return _out(entry, None, None)


@router.get("/transactions/{transaction_id}/ledger", response_model=list[EntryOut])
async def get_linked_entries(transaction_id: uuid.UUID, user: User = Depends(current_user),
                             db: AsyncSession = Depends(get_db)):
    """The transaction's lend & borrow entries, oldest first."""
    txn = await _owned_txn(db, user, transaction_id)
    return await _linked_entries(db, user, txn)


@router.put("/transactions/{transaction_id}/ledger", response_model=LinkedOut, dependencies=[Depends(same_origin)])
async def save_linked_entries(transaction_id: uuid.UUID, body: LinkedEntriesIn, background_tasks: BackgroundTasks,
                              user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Replaces the transaction's entries ([] removes them all). Only on a parsed INR transaction; a payment takes
    Lent to / Repaid to, money received takes Repayment from / Borrowed from; together at most its amount. Bumps
    its updated_at; on a payment, the spend-limit check runs again (its counted amount changed)."""
    txn = await lock_transaction(db, user, transaction_id)  # before the checks: a save at once waits for this one
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found.")
    if not txn.is_parsed:
        raise HTTPException(422, "Only payments Folio has read can have lend & borrow entries.")
    if txn.currency != "INR":
        raise HTTPException(422, "Lend & borrow is in rupees only.")
    allowed = DEBIT_KINDS if txn.type == "DEBIT" else CREDIT_KINDS
    if any(entry.kind not in allowed for entry in body.entries):
        raise HTTPException(422, "On a payment, use Lent to or Repaid to." if txn.type == "DEBIT"
                            else "On money received, use Repayment from or Borrowed from.")
    if sum((entry.amount for entry in body.entries), Decimal(0)) > Decimal(str(txn.amount)):
        raise HTTPException(422, "Shares add up to more than the payment.")
    await db.execute(delete(LedgerEntry).where(LedgerEntry.transaction_id == txn.id, LedgerEntry.user_id == user.id))
    people = await _spellings(db, user, [entry.person for entry in body.entries])  # after the delete: see above
    now = utc_now()
    for i, (entry, person) in enumerate(zip(body.entries, people)):
        # A microsecond apart, so "oldest first" keeps the order they were sent in (created_at keeps microseconds
        # on TiDB too: models.LedgerEntry).
        db.add(LedgerEntry(user_id=user.id, person=person, person_key=person_key(person), kind=entry.kind,
                           amount=entry.amount, day=None, transaction_id=txn.id, note=entry.note,
                           created_at=now + timedelta(microseconds=i)))
    txn.updated_at = now
    await db.commit()
    await db.refresh(txn)  # loads the new linked_amount
    if txn.type == "DEBIT":
        background_tasks.add_task(limits.check_spend_limits, txn.id)
    return LinkedOut(linked_amount=float(txn.linked_amount or 0), updated_at=_utc(txn.updated_at),
                     entries=await _linked_entries(db, user, txn))


@router.delete("/ledger/{entry_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(same_origin)])
async def delete_entry(entry_id: uuid.UUID, background_tasks: BackgroundTasks, user: User = Depends(current_user),
                       db: AsyncSession = Depends(get_db)):
    """Removes one entry. A linked one also leaves its transaction (its updated_at moves; a payment's spend-limit
    check runs again, as its counted amount rose)."""
    entry = await db.scalar(select(LedgerEntry).where(LedgerEntry.id == entry_id, LedgerEntry.user_id == user.id))
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found.")
    txn = None
    if entry.transaction_id is not None:
        txn = await db.scalar(select(Transaction).where(Transaction.id == entry.transaction_id))
    await db.delete(entry)
    check = None
    if txn is not None:
        txn.updated_at = utc_now()
        if txn.is_parsed and txn.type == "DEBIT":
            check = txn.id
    await db.commit()
    if check is not None:
        background_tasks.add_task(limits.check_spend_limits, check)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
