"""Read-only analytics; existing unassigned SMS records belong to the configured owner."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_user
from database import get_db
from models import Transaction, User

router = APIRouter(prefix="/api/v1", tags=["Dashboard"])
IST = ZoneInfo("Asia/Kolkata")


def ownership(user):
    return or_(Transaction.user_id == user.id, Transaction.user_id.is_(None))


@router.get("/dashboard")
async def dashboard(
    start: date, end: date,
    currency: str = Query("INR", pattern="^[A-Z]{3}$"),
    search: str = Query("", max_length=100),
    kind: Literal["ALL", "DEBIT", "CREDIT"] = "ALL",
    category: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    if end < start or (end - start).days > 366:
        raise HTTPException(422, "Choose a date range of up to 367 days.")
    query = select(Transaction).where(
        ownership(user), Transaction.currency == currency,
        Transaction.transaction_time >= datetime.combine(start, time.min, IST).astimezone(timezone.utc),
        Transaction.transaction_time < datetime.combine(end + timedelta(days=1), time.min, IST).astimezone(timezone.utc),
    ).order_by(Transaction.transaction_time.desc(), Transaction.id.desc())
    rows = (await db.scalars(query)).all()
    currencies = (await db.scalars(select(Transaction.currency).where(ownership(user)).distinct())).all()
    valid = [r for r in rows if r.is_parsed]
    income = sum((r.amount for r in valid if r.type == "CREDIT"), Decimal(0))
    expense = sum((r.amount for r in valid if r.type == "DEBIT"), Decimal(0))
    categories = defaultdict(Decimal)
    daily = {}
    for i in range((end - start).days + 1):
        daily[(start + timedelta(days=i)).isoformat()] = {"income": Decimal(0), "expense": Decimal(0)}
    for row in valid:
        if row.type not in ("DEBIT", "CREDIT"):
            continue
        if row.type == "DEBIT":
            categories[row.category or "Uncategorized"] += row.amount
        when = row.transaction_time
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        key = when.astimezone(IST).date().isoformat()
        if key in daily:
            daily[key]["expense" if row.type == "DEBIT" else "income"] += row.amount
    filtered = [r for r in rows if
        (kind == "ALL" or r.type == kind) and
        (not category or (r.category or "Uncategorized") == category) and
        (not search or search.casefold() in " ".join([r.merchant or "", r.category or "", r.account_ref or ""]).casefold())]
    return {
        "income": income, "expense": expense, "net": income - expense,
        "count": len(rows), "review_count": len(rows) - len(valid),
        "daily_average": expense / Decimal(len(daily)),
        "currencies": sorted(set(currencies) | {currency}),
        "categories": [{"name": k, "value": v} for k, v in sorted(categories.items(), key=lambda x: x[1], reverse=True)],
        "category_options": sorted({r.category or "Uncategorized" for r in rows}),
        "trend": [{"date": k, **v} for k, v in daily.items()],
        "total": len(filtered), "page": page, "page_size": 10,
        "transactions": [{
            "id": str(r.id), "type": r.type, "amount": r.amount, "currency": r.currency,
            "merchant": r.merchant, "category": r.category, "account_ref": r.account_ref,
            "transaction_time": r.transaction_time, "is_parsed": r.is_parsed,
            "reference_id": r.reference_id, "sender": r.sender, "parsed_by": r.parsed_by,
        } for r in filtered[(page - 1) * 10:page * 10]],
    }
