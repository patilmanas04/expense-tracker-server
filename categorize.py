"""Where a transaction's category comes from, in order:

1. the category you picked for the merchant (editing or adding a transaction remembers it),
2. the bank-SMS keyword rules (parser.py),
3. the category of your latest earlier payment to the same merchant,
4. Gemini. If Gemini is too slow while the SMS is being saved, it's saved Uncategorized and
   `retry_ai_category` fills the category in the background with more time.

Memory is per direction: paying a merchant and a refund from it are different categories.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import OWNER_EMAIL
from database import AsyncSessionLocal
from limits import check_spend_limits
from models import MerchantCategory, Transaction, User
from parser import UNCATEGORIZED, Recalled, categorize_with_gemini

logger = logging.getLogger(__name__)

# Seconds before each background attempt. A slow Gemini usually recovers within minutes.
RETRY_DELAYS = (0, 60, 300)
# Nobody waits on a background attempt, so Gemini gets far longer than the inline 5 s.
RETRY_TIMEOUT = 30.0


def merchant_key(merchant: Optional[str]) -> Optional[str]:
    key = " ".join((merchant or "").split()).casefold()
    return key if key and key != "unknown" else None


def _owner_id(user_id: Optional[uuid.UUID]):
    # SMS arrive without a session; they belong to the dashboard owner.
    return user_id if user_id is not None else select(User.id).where(User.email == OWNER_EMAIL).scalar_subquery()


async def recall_category(db: AsyncSession, merchant: Optional[str], tx_type: str,
                          user_id: Optional[uuid.UUID] = None) -> Optional[Recalled]:
    """Your category for the merchant, else the one from your latest payment to it. One round trip."""
    key = merchant_key(merchant)
    if key is None:
        return None
    owner = _owner_id(user_id)
    chosen = select(MerchantCategory.category).where(
        MerchantCategory.user_id == owner,
        MerchantCategory.merchant_key == key,
        MerchantCategory.type == tx_type,
    ).limit(1).scalar_subquery()
    earlier = select(Transaction.category).where(
        or_(Transaction.user_id == owner, Transaction.user_id.is_(None)),
        func.lower(Transaction.merchant) == key,
        Transaction.type == tx_type,
        Transaction.is_parsed.is_(True),
        Transaction.category.is_not(None),
        Transaction.category != UNCATEGORIZED,
    ).order_by(Transaction.updated_at.desc(), Transaction.transaction_time.desc()).limit(1).scalar_subquery()
    row = (await db.execute(select(chosen.label("chosen"), earlier.label("earlier")))).one()
    if row.chosen:
        return Recalled(row.chosen, "user")
    if row.earlier:
        return Recalled(row.earlier, "history")
    return None


async def remember(db: AsyncSession, user_id: uuid.UUID, merchant: Optional[str], tx_type: str, category: str) -> None:
    """Saves your category for the merchant; choosing Uncategorized forgets it. The caller commits."""
    key = merchant_key(merchant)
    if key is None:
        return
    where = (MerchantCategory.user_id == user_id, MerchantCategory.merchant_key == key, MerchantCategory.type == tx_type)
    if category == UNCATEGORIZED:
        await db.execute(delete(MerchantCategory).where(*where))
        return
    rule = await db.scalar(select(MerchantCategory).where(*where))
    if rule is None:
        db.add(MerchantCategory(user_id=user_id, merchant_key=key, type=tx_type, category=category))
    else:
        rule.category = category
        rule.updated_at = datetime.now(timezone.utc)


async def auto_category(db: AsyncSession, user_id: uuid.UUID, merchant: str, tx_type: str, keyword_category: str) -> str:
    """For a transaction added by hand with Category "Auto": the same order as an SMS, minus Gemini."""
    remembered = await recall_category(db, merchant, tx_type, user_id)
    if remembered and (remembered.source == "user" or keyword_category == UNCATEGORIZED):
        return remembered.category
    return keyword_category


async def _attempt(transaction_id: uuid.UUID) -> bool:
    """One try. True when finished (categorised, already handled, or Gemini is unsure); False to retry."""
    async with AsyncSessionLocal() as db:
        txn = await db.get(Transaction, transaction_id)
        if txn is None or not txn.is_parsed or (txn.category or UNCATEGORIZED) != UNCATEGORIZED:
            return True  # deleted, needs review, or categorised meanwhile
        seen, merchant, tx_type, sms_text, parsed_by = txn.updated_at, txn.merchant, txn.type, txn.raw_message, txn.parsed_by
        remembered = await recall_category(db, merchant, tx_type, txn.user_id)
    if remembered:  # e.g. you categorised another payment to the same merchant meanwhile
        category, source = remembered
    else:
        try:
            category, source = await categorize_with_gemini(sms_text, merchant, tx_type, timeout=RETRY_TIMEOUT), "ai"
        except Exception as error:  # noqa: BLE001 - retried; a failure must never escape a background task
            logger.warning("Background categorisation attempt failed: %s", error or type(error).__name__)
            return False
        if category == UNCATEGORIZED:
            return True  # Gemini answered; it just isn't sure
    async with AsyncSessionLocal() as db:
        # Only if nothing changed since we looked: an edit you made meanwhile always wins.
        result = await db.execute(update(Transaction).where(Transaction.id == transaction_id, Transaction.updated_at == seen).values(
            category=category,
            parsed_by=f"regex+{source}" if parsed_by == "regex" else parsed_by,
            updated_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    if result.rowcount:
        await check_spend_limits(transaction_id)  # it may now count toward a category limit
    return True


async def retry_ai_category(transaction_id: uuid.UUID) -> None:
    """Background task after an SMS (or "Auto" entry) was saved Uncategorized because Gemini was slow."""
    for delay in RETRY_DELAYS:
        await asyncio.sleep(delay)
        try:
            if await _attempt(transaction_id):
                return
        except Exception:  # database hiccup: try again later
            logger.exception("Background categorisation failed")
    logger.warning("Gave up categorising transaction %s; it stays Uncategorized", transaction_id)
