"""Low-balance alert: one in-app notification when a payment takes Balance left (Settings → Bank balance, HDFC
and ICICI together) below the level set in `alerts.low_balance` (whole rupees; Off by default).

The rule needs no stored state: the balance before a payment is the balance after it plus what the payment took
out. With `current` = Balance left now (balance.summarize) and `before = current + amount`, the payment alerts
when `before >= level > current`. Landing exactly on the level is not below it. "Once per dip" follows: every
later payment in the dip starts below the level, so it can't cross it. After a credit or a new saved balance
lifts Balance left back to the level or above, the next payment that crosses it alerts again.

Causes (post_save.py): `sms` and `manual` (a payment just saved), and `restored` only for "Not a duplicate",
which brings a debit back into the balance. A held-out row released because its original was deleted is also
`restored`, but there the balance didn't move (the deleted original had the same amount), and deletes never
alert; the released row has no duplicate_flags row left, so it's told apart by its flag's status.

Never alerts: credits, cash entries, card spends, possible duplicates waiting for your decision (they never
reach a check), unreadable SMS, other currencies, a saved balance in another currency, payments at or before
the saved balance's `as_of` (already in it), edits, AI category retries, deletes, saving a balance and changing
the level (none of those run this check).

One notification per payment (dedupe key `low_balance:<id>`), in-app only, opening the balance breakdown.

Queries: none when Off or for a credit; 1 without a saved balance; 2 normally (the saved balance, then
summarize); 1 more for `restored`; create_once's 2 when it alerts.
"""
from decimal import Decimal

from sqlalchemy import select

import balance
from limits import _utc, format_money
from models import DUPLICATE_DISMISSED, BalanceSetting, DuplicateFlag
from notifications import create_once
from post_save import PostSave

KIND = "low_balance"
LINK = "/settings/balance"  # the breakdown (balance_check_view)


async def check(ctx: PostSave) -> None:
    """Registered with post_save for sms, manual and restored."""
    level = ctx.settings["alerts.low_balance"]
    txn = ctx.txn
    if level is None or txn.type != "DEBIT":
        return  # a credit can only raise the balance
    if ctx.cause == "restored":
        status = await ctx.db.scalar(select(DuplicateFlag.status).where(DuplicateFlag.transaction_id == txn.id))
        if status != DUPLICATE_DISMISSED:
            return  # released by deleting its original: the balance didn't move
    setting = await ctx.db.scalar(select(BalanceSetting).where(BalanceSetting.user_id == ctx.owner.id))
    if setting is None or setting.currency != "INR":
        return  # the level is in rupees
    if _utc(txn.transaction_time) <= _utc(setting.as_of):
        return  # before the saved balance: already in it
    # held_out=False: post_save never runs a check for a held-out row.
    if balance.classify(txn.is_parsed, txn.currency, False, txn.account_ref, txn.raw_message, setting.currency):
        return  # unreadable, other currency, cash or card: doesn't move Balance left
    summary = await balance.summarize(ctx.db, ctx.owner, setting)
    current = Decimal(str(summary.current)).quantize(Decimal("0.01"))  # exact for 2-decimal amounts
    before = current + Decimal(str(txn.amount))
    threshold = Decimal(level)
    if not before >= threshold > current:
        return
    await create_once(ctx.db, ctx.owner.id, kind=KIND,
                      title=f"Balance low: {format_money(current, 'INR')} left",
                      body=f"Below your {format_money(threshold, 'INR')[:-3]} alert (HDFC and ICICI together).",
                      link=LINK, dedupe_key=f"low_balance:{txn.id}")
