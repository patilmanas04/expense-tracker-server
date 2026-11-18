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

Extension points (later designs change these, not their callers): counts_as_spending() and is_held_out() in
Python; spending_category_filter(), not_held_out() and counted_filter() in SQL; in the app, counts(),
isInvested() and needsReview() in lib/analytics.ts.
"""
import uuid
from typing import Optional

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import DUPLICATE_PENDING, DuplicateFlag, Transaction

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
