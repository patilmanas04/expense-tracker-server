"""What counts as spending and income: one rule for the server, matched by the app.

Two category sets; the app's copy is NOT_COUNTED / INVESTED in
expense-tracker-client/lib/categories.ts, and tests/test_spending.py compares them.

* NOT_COUNTED: card bill payments and transfers between your own accounts are neither spending nor income.
* INVESTED: money put into or taken out of investments and deposits is neither; the app shows its debits
  as "Invested".

Every other category, including Cash Withdrawal, Transfer, Uncategorized and no category, counts:
debits as spending, credits as income.

Extension points (later designs change these, not their callers): counts_as_spending() in Python and
spending_category_filter() in SQL; in the app, counts() and isInvested() in lib/analytics.ts.
"""
from typing import Optional

from sqlalchemy import or_

from models import Transaction

NOT_COUNTED = frozenset({"Credit Card Payment", "Self Transfer"})
INVESTED = frozenset({"Investments & Finance"})
EXCLUDED_FROM_SPENDING = NOT_COUNTED | INVESTED


def counts_as_spending(category: Optional[str]) -> bool:
    """True for no category, Uncategorized and every category outside both sets."""
    return category not in EXCLUDED_FROM_SPENDING


def spending_category_filter():
    """SQL condition: the transaction's category counts. NULL counts (NOT IN alone would drop it)."""
    return or_(Transaction.category.is_(None), Transaction.category.not_in(sorted(EXCLUDED_FROM_SPENDING)))
