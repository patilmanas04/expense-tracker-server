"""Payment alerts: one in-app notification when a saved payment is large, unusual for its merchant, or both.

* **Large**: the payment is over the amount set in Settings → Alerts (`alerts.large_amount`, whole rupees; Off
  by default). Equal is not over.
* **Unusual for the merchant** (`alerts.unusual`, On by default): the payment is at least UNUSUAL_FACTOR times
  what you usually spend at that merchant, and at least UNUSUAL_FLOOR. "Usually" is the median of your earlier
  counted INR debits to the same merchant (categorize.merchant_key: case and extra spaces ignored) in the
  HISTORY_DAYS before it, needing at least MIN_HISTORY of them. Full amounts: what left the account.

The constants are first guesses, to be tuned after a month of use.

One notification per payment (dedupe key `payment_alert:<id>`), naming both rules when both match; it opens
the payment in Activity. Only real spending: parsed INR debits in categories that count (spending.py); a
possible duplicate waiting for your decision never reaches this check (post_save.py skips it) and is checked
once it counts again. Registered with post_save for every cause except `edit`: an edit is your own change.

Named payment_alerts (kind "payment_alert"), not spend_alerts, because models.SpendAlert and the spend_alerts
table already track limit emails.
"""
import statistics
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from categorize import merchant_key
from dashboard import ownership
from limits import _utc, format_money
from models import Transaction, User
from notifications import create_once
from post_save import PostSave
from spending import counted_filter, counts_as_spending

UNUSUAL_FACTOR = 3  # at least this many times the usual amount
UNUSUAL_FLOOR = Decimal("500")  # and at least this much, in rupees
HISTORY_DAYS = 180  # earlier payments looked at
MIN_HISTORY = 3  # fewer earlier payments than this: no usual amount yet

TITLES = {
    (True, False): "Large payment",
    (False, True): "Unusual payment",
    (True, True): "Large and unusual payment",
}


async def usual_amount(db: AsyncSession, owner: User, txn: Transaction, key: str) -> Optional[Decimal]:
    """The median of your counted INR debits to this merchant in the HISTORY_DAYS before the payment (not at
    the same moment or after it), or None with fewer than MIN_HISTORY of them. One query; the merchant is
    matched here by its key, which SQL's lower() would miss for double spaces."""
    when = _utc(txn.transaction_time)
    rows = (await db.execute(select(Transaction.merchant, Transaction.amount).where(
        ownership(owner),
        Transaction.is_parsed.is_(True),
        Transaction.type == "DEBIT",
        Transaction.currency == "INR",
        counted_filter(),
        Transaction.transaction_time >= when - timedelta(days=HISTORY_DAYS),
        Transaction.transaction_time < when,
        Transaction.id != txn.id,
    ))).all()
    amounts = [Decimal(str(amount)) for merchant, amount in rows if merchant_key(merchant) == key]
    if len(amounts) < MIN_HISTORY:
        return None
    return Decimal(str(statistics.median(amounts)))


async def check(ctx: PostSave) -> None:
    """Registered with post_save for sms, manual, ai_retry and restored."""
    txn = ctx.txn
    if not txn.is_parsed or txn.type != "DEBIT" or txn.currency != "INR" or not counts_as_spending(txn.category):
        return
    amount = Decimal(str(txn.amount))
    threshold = ctx.settings["alerts.large_amount"]
    large = threshold is not None and amount > Decimal(threshold)
    key = merchant_key(txn.merchant)
    usual = None
    if ctx.settings["alerts.unusual"] and amount >= UNUSUAL_FLOOR and key:
        usual = await usual_amount(ctx.db, ctx.owner, txn, key)
    unusual = usual is not None and usual > 0 and amount >= UNUSUAL_FACTOR * usual
    if not (large or unusual):
        return
    money = format_money(amount, "INR")
    body = f"{money} at {' '.join(txn.merchant.split())}" if key else f"{money} payment"
    if unusual:
        times = int((amount / usual).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        body += f" — about {times}× what you usually spend there ({format_money(usual, 'INR')})"
    await create_once(ctx.db, ctx.owner.id, kind="payment_alert", title=TITLES[(large, unusual)], body=body,
                      link=f"/activity?txn={txn.id}", dedupe_key=f"payment_alert:{txn.id}")
