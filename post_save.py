"""What runs after a payment is saved: one background task for every check that follows a saved, edited or
re-categorised transaction (spend limits, payment alerts, and later designs' checks).

Each call names its cause, and each check is registered for the causes it cares about:

| Cause      | When                                                                               |
|------------|------------------------------------------------------------------------------------|
| `sms`      | the webhook saved an SMS                                                           |
| `manual`   | you added a payment by hand                                                        |
| `edit`     | you edited a parsed debit and changed more than its note                           |
| `ai_retry` | Gemini filled in the category later (categorize.retry_ai_category)                 |
| `restored` | "Not a duplicate", or deleting the original of a held-out row made it count again   |

The runner resolves the owner, loads the transaction and your settings once, and gives every check the same
`PostSave`. A possible duplicate waiting for your decision (spending.is_held_out) counts nowhere, so it never
reaches a check; it runs again with cause `restored` once it counts.

Registration lives in one block in main.py, right after the imports, so the order checks run in is visible.
Each check runs in its own try/except: one failing never stops the others, and run() never raises.

Cost after all designs ship, per saved SMS, after the reply is sent and one check at a time: the runner's 4
queries (owner, transaction, held-out test, settings), limits 6–8 (one more with 'Month starts on payday' on),
payment alerts 1, low balance 2, recurring about 5, refunds 1–2 for credits, transfers 1–2 for parsed SMS: about
20 round trips, 4–6 s on TiDB, which nothing waits for. A restart drops queued tasks, as with any background task.
"""
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import settings_store
import spending
from auth import OWNER_EMAIL
from database import AsyncSessionLocal
from models import Transaction, User

logger = logging.getLogger(__name__)

CAUSES = ("sms", "manual", "edit", "ai_retry", "restored")


@dataclass(frozen=True)
class PostSave:
    """What every check gets. `owner` and `txn` are detached: read them freely, even after a rollback."""

    db: AsyncSession
    owner: User
    txn: Transaction
    settings: dict[str, Any]  # settings_store.load()
    cause: str


Check = Callable[[PostSave], Awaitable[None]]
_CHECKS: list[tuple[str, Check, frozenset[str]]] = []


def register(name: str, check: Check, causes: Iterable[str]) -> None:
    """Adds a check, run after the ones registered before it, for the given causes."""
    causes = frozenset(causes)
    unknown = causes - set(CAUSES)
    if unknown:
        raise ValueError(f"Unknown post-save causes for {name}: {sorted(unknown)}")
    if any(existing == name for existing, _, _ in _CHECKS):
        raise ValueError(f"Post-save check {name} is already registered")
    _CHECKS.append((name, check, causes))


async def load(db: AsyncSession, transaction_id: uuid.UUID, cause: str) -> Optional[PostSave]:
    """The owner, the transaction and the settings, in 4 statements; None when there's nothing to check: no
    owner configured or found, no such transaction, another user's, or a possible duplicate held out."""
    if not OWNER_EMAIL:
        return None
    owner = await db.scalar(select(User).where(User.email == OWNER_EMAIL))
    if owner is None:
        return None
    txn = await db.get(Transaction, transaction_id)
    if txn is None or txn.user_id not in (None, owner.id):
        return None
    if await spending.is_held_out(db, txn.id):
        return None  # a possible duplicate counts nowhere until you decide
    settings = await settings_store.load(db, owner.id)
    # When create_once loses a race to a simultaneous SMS it rolls back, which expires every object
    # in the session, and reading an expired one fails under asyncio (no lazy loads). From here on
    # these two are only read, so they're detached.
    db.expunge(txn)
    db.expunge(owner)
    return PostSave(db, owner, txn, settings, cause)


async def run(transaction_id: uuid.UUID, cause: str) -> None:
    """Background task: every check registered for `cause`, in registration order, in one session. Never
    raises: checks must not affect recording."""
    try:
        async with AsyncSessionLocal() as db:
            ctx = await load(db, transaction_id, cause)
            if ctx is None:
                return
            for name, check, causes in list(_CHECKS):
                if cause not in causes:
                    continue
                try:
                    await check(ctx)
                except Exception:
                    logger.exception("Post-save check %s failed", name)
                    try:
                        await db.rollback()  # the next check gets a usable session
                    except Exception:
                        logger.exception("Rollback after post-save check %s failed", name)
    except Exception:
        logger.exception("Post-save checks failed")
