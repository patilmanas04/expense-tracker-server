"""What counts as spending and income: one rule for the server, matched by the app.

Two category sets; the app's copy is NOT_COUNTED / INVESTED in
expense-tracker-client/lib/categories.ts, and tests/test_spending.py compares them.

* NOT_COUNTED: card bill payments and transfers between your own accounts are neither spending nor income.
* INVESTED: money put into or taken out of investments and deposits is neither; the app shows its debits
  as "Invested".

Every other category, including Cash Withdrawal, Transfer, Uncategorized and no category, counts:
debits as spending, credits as income.

A possible duplicate (duplicates.py) counts nowhere while it waits for your decision: not_held_out() /
counted_filter() in SQL, is_held_out() for one transaction; in the app isHeldOut() inside counts().

Lend & borrow (ledger.py): a payment or credit counts its amount minus its linked lend & borrow entries (the
"counted amount"): lending ₹1,200 of a ₹1,800 dinner counts ₹600 as spending; a ₹600 repayment from a friend isn't
income. linked_totals() / counted_amount() in SQL; t.counted in the app.

Extension points (later designs change these, not their callers): counts_as_spending() and is_held_out() in
Python; spending_category_filter(), not_held_out() and counted_filter() in SQL; in the app, counts(),
isInvested() and needsReview() in lib/analytics.ts.
"""
import uuid
from typing import Optional

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import DUPLICATE_PENDING, DuplicateFlag, LedgerEntry, Transaction

NOT_COUNTED = frozenset({"Credit Card Payment", "Self Transfer"})
INVESTED = frozenset({"Investments & Finance"})
EXCLUDED_FROM_SPENDING = NOT_COUNTED | INVESTED


def counts_as_spending(category: Optional[str]) -> bool:
    """True for no category, Uncategorized and every category outside both sets."""
    return category not in EXCLUDED_FROM_SPENDING


def spending_category_filter():
    """SQL condition: the transaction's category counts. NULL counts (NOT IN alone would drop it)."""
    return or_(Transaction.category.is_(None), Transaction.category.not_in(sorted(EXCLUDED_FROM_SPENDING)))


def not_held_out():
    """SQL condition: not a possible duplicate waiting for your decision (duplicates.py)."""
    return ~exists().where(DuplicateFlag.transaction_id == Transaction.id, DuplicateFlag.status == DUPLICATE_PENDING)


def counted_filter():
    """SQL condition for a spending sum: the category counts and the row isn't held out."""
    return and_(spending_category_filter(), not_held_out())


async def is_held_out(db: AsyncSession, transaction_id: uuid.UUID) -> bool:
    """True while the transaction is a possible duplicate waiting for your decision."""
    return await db.scalar(select(DuplicateFlag.transaction_id).where(
        DuplicateFlag.transaction_id == transaction_id, DuplicateFlag.status == DUPLICATE_PENDING)) is not None


def linked_totals():
    """Subquery (transaction_id, total): each transaction's lend & borrow entries. Outer-join it on
    `linked.c.transaction_id == Transaction.id` and sum counted_amount(linked)."""
    return (select(LedgerEntry.transaction_id, func.sum(LedgerEntry.amount).label("total"))
            .where(LedgerEntry.transaction_id.is_not(None))
            .group_by(LedgerEntry.transaction_id)
            .subquery("linked"))


def counted_amount(linked):
    """SQL: the counted amount = amount − linked lend & borrow entries (the whole amount when there are none)."""
    return Transaction.amount - func.coalesce(linked.c.total, 0)
