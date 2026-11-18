"""One-time fix after "Count only real spending" (Phases 1-2): bank-side card-bill debits saved before the
card-bill check get "Credit Card Payment", and category limits on categories that no longer count are
deleted. Dry run by default; idempotent.

    uv run python scripts/recategorise_card_payments.py                    # lists what would change
    uv run python scripts/recategorise_card_payments.py --apply            # changes every listed row
    uv run python scripts/recategorise_card_payments.py --apply --ids a,b  # changes only these rows
    uv run python scripts/recategorise_card_payments.py --email you@example.com

--apply on a database that isn't local asks you to type "yes" first.
"""
import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from dashboard import ownership  # noqa: E402
from database import DATABASE_URL, AsyncSessionLocal, engine  # noqa: E402
from limits import format_money  # noqa: E402
from models import CategoryLimit, Transaction, User  # noqa: E402
from parser import is_card_bill_debit  # noqa: E402
from spending import EXCLUDED_FROM_SPENDING  # noqa: E402

CARD_PAYMENT = "Credit Card Payment"
IST = timezone(timedelta(hours=5, minutes=30))
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
BATCH = 200


def _host(url: str) -> Optional[str]:
    plain = url.replace("+asyncpg", "").replace("+aiomysql", "").replace("+aiosqlite", "")
    return None if plain.startswith("sqlite") else urlparse(plain).hostname


def is_local(url: str) -> bool:
    """SQLite, or a server on this machine."""
    return url.startswith("sqlite") or _host(url) in LOCAL_HOSTS


async def load_owner(db: AsyncSession, email: str) -> Optional[User]:
    return await db.scalar(select(User).where(User.email == email.strip().lower()))


def _parsed_debits(owner: User):
    return select(Transaction).where(
        ownership(owner), Transaction.is_parsed.is_(True), Transaction.type == "DEBIT",
    ).order_by(Transaction.transaction_time, Transaction.id)


async def bill_debits_to_fix(db: AsyncSession, owner: User) -> list[Transaction]:
    """Debits whose SMS names a credit card and a bank account (a card bill paid from the bank) that
    aren't "Credit Card Payment" yet. Checked in Python with the parser's own test."""
    rows = (await db.scalars(_parsed_debits(owner))).all()
    return [t for t in rows if t.category != CARD_PAYMENT and is_card_bill_debit(t.raw_message or "")]


async def suspicious_card_payments(db: AsyncSession, owner: User) -> list[Transaction]:
    """"Credit Card Payment" debits that don't look like a bank-side card bill (e.g. a card autopay the
    old rule filed there). Listed for information only; the owner fixes any by hand."""
    rows = (await db.scalars(_parsed_debits(owner).where(Transaction.category == CARD_PAYMENT))).all()
    return [t for t in rows if not is_card_bill_debit(t.raw_message or "")]


async def excluded_limits(db: AsyncSession, owner: User) -> list[CategoryLimit]:
    """Category limits on categories that aren't spending any more; the app hides them."""
    return list((await db.scalars(select(CategoryLimit).where(
        CategoryLimit.user_id == owner.id, CategoryLimit.category.in_(sorted(EXCLUDED_FROM_SPENDING)),
    ).order_by(CategoryLimit.category))).all())


async def apply_changes(db: AsyncSession, txn_ids: list[uuid.UUID], limit_ids: list[uuid.UUID]) -> tuple[int, int]:
    """Sets the transactions to "Credit Card Payment" and deletes the limits, in one commit."""
    changed = deleted = 0
    now = datetime.now(timezone.utc)
    for start in range(0, len(txn_ids), BATCH):
        batch = txn_ids[start:start + BATCH]
        result = await db.execute(
            update(Transaction).where(Transaction.id.in_(batch)).values(category=CARD_PAYMENT, updated_at=now)
        )
        changed += result.rowcount or 0
    if limit_ids:
        result = await db.execute(delete(CategoryLimit).where(CategoryLimit.id.in_(limit_ids)))
        deleted += result.rowcount or 0
    await db.commit()
    return changed, deleted


def _date(value: datetime) -> str:
    # SQLite drops the offset; values are always stored in UTC.
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return value.astimezone(IST).strftime("%d %b %Y")


def _amount(value: Optional[Decimal], currency: str) -> str:
    return "—" if value is None else format_money(Decimal(value), currency)


def _section(title: str, lines: list[str], note: str = "") -> None:
    print(f"{title} ({note}{len(lines)}):")
    for line in lines or ["none"]:
        print(f"  {line}")


def _ids(text: str) -> list[uuid.UUID]:
    try:
        return [uuid.UUID(part.strip()) for part in text.split(",") if part.strip()]
    except ValueError as error:
        sys.exit(f"--ids takes comma-separated transaction ids: {error}.")


async def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", default=os.getenv("DASHBOARD_OWNER_EMAIL"),
                        help="whose transactions to check (default: DASHBOARD_OWNER_EMAIL)")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: only list them)")
    parser.add_argument("--ids", help="with --apply: change only these listed transactions (comma-separated)")
    opts = parser.parse_args(argv)
    if opts.ids is not None and not opts.apply:
        sys.exit("--ids needs --apply.")
    if not opts.email:
        sys.exit("Pass --email or set DASHBOARD_OWNER_EMAIL.")
    wanted = _ids(opts.ids) if opts.ids is not None else None

    async with AsyncSessionLocal() as db:
        owner = await load_owner(db, opts.email)
        if owner is None:
            sys.exit("No user with that email; sign in to Folio once first.")
        to_fix = await bill_debits_to_fix(db, owner)
        suspicious = await suspicious_card_payments(db, owner)
        limits = await excluded_limits(db, owner)

        print(f"Owner: {owner.email} · database host: {_host(DATABASE_URL) or 'sqlite'}")
        _section(f'Card-bill debits to change to "{CARD_PAYMENT}"', [
            f"{t.id}  {_date(t.transaction_time)}  {_amount(t.amount, t.currency)}  {t.category or 'none'}  {t.parsed_by}"
            for t in to_fix
        ])
        _section(f"\"{CARD_PAYMENT}\" debits that don't look like a bank-side card bill", [
            f"{t.id}  {_date(t.transaction_time)}  {_amount(t.amount, t.currency)}  {t.parsed_by}" for t in suspicious
        ], note="information only, ")
        _section("Category limits on categories that no longer count", [
            f"{limit.id}  {limit.category}  weekly {_amount(limit.weekly_limit, limit.currency)}  "
            f"monthly {_amount(limit.monthly_limit, limit.currency)}"
            for limit in limits
        ])

        if not opts.apply:
            print("Dry run: nothing changed. Re-run with --apply (or --apply --ids id1,id2) to make these changes.")
            return 0

        listed = [t.id for t in to_fix]
        if wanted is not None:
            unknown = [str(i) for i in wanted if i not in set(listed)]
            if unknown:
                sys.exit(f"Not in the list above, so nothing changed: {', '.join(unknown)}.")
            listed = [i for i in listed if i in set(wanted)]
        if not is_local(DATABASE_URL):
            print(f"This changes the database on {_host(DATABASE_URL)}.")
            if input('Type "yes" to change the production database: ').strip() != "yes":
                sys.exit("Nothing changed.")
        changed, deleted = await apply_changes(db, listed, [limit.id for limit in limits])
        print(f"Changed {changed} transactions to {CARD_PAYMENT} and deleted {deleted} category limits.")
    return 0


async def _cli() -> None:
    try:
        await main()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_cli())
