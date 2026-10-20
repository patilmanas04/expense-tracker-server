import os
import secrets
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional
import uuid

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, init_db
from models import Transaction, User
from parser import ParsedTransaction, compute_message_hash, parse_sms
from auth import FRONTEND_URL, current_user, router as auth_router
from balance import BalanceSummary, owner_balance, router as balance_router
from limits import check_spend_limits, router as limits_router
from notifications import router as notifications_router
from dashboard import ownership, router as dashboard_router

load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure database tables exist upon server startup
    await init_db()
    print("Database tables initialized successfully.")
    yield


app = FastAPI(
    title="Expense Tracker Server",
    description="Automated bank SMS ingestion, parsing (Regex + Gemini Agent), and transaction API.",
    version="0.1.0",
    lifespan=lifespan,
)

# Enable CORS for Next.js frontend and iPhone requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Webhook-Secret"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48),
    session_cookie="folio_session",
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=FRONTEND_URL.startswith("https://"),
)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(balance_router)
app.include_router(limits_router)
app.include_router(notifications_router)


@app.middleware("http")
async def private_response_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# Security: Verify Webhook Secret via header
def verify_webhook_secret(
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
    authorization: Optional[str] = Header(None),
) -> bool:
    """Verifies that incoming webhook requests provide the matching secret.
    
    Accepts either 'X-Webhook-Secret: <secret>' or 'Authorization: Bearer <secret>'.
    """
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WEBHOOK_SECRET is not configured on the server.",
        )

    token = None
    if x_webhook_secret:
        token = x_webhook_secret
    elif authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()

    if not token or not secrets.compare_digest(token.encode(), WEBHOOK_SECRET.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret.",
        )
    return True


# Pydantic Schemas
class WebhookPayload(BaseModel):
    message: Optional[str] = Field(default="", max_length=10000, description="Raw SMS text received on iPhone")
    sender: Optional[str] = Field(None, max_length=100, description="Sender ID or number, e.g., VK-HDFCBK")
    received_at: Optional[AwareDatetime] = Field(None, description="ISO timestamp with timezone from iPhone shortcut")


class TransactionResponse(BaseModel):
    id: uuid.UUID
    type: str
    amount: float
    currency: str
    merchant: Optional[str]
    category: Optional[str]
    account_ref: Optional[str]
    balance: Optional[float]
    reference_id: Optional[str]
    sender: Optional[str]
    raw_message: str
    is_parsed: bool
    parsed_by: str
    transaction_time: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookResponse(BaseModel):
    status: str
    message: str
    transaction: Optional[TransactionResponse] = None
    balance: Optional[BalanceSummary] = Field(
        default=None, description="Balance left after this SMS, if the owner has set one in Settings"
    )


async def balance_after(db: AsyncSession) -> Optional[BalanceSummary]:
    """The balance is informational; it must never stop an SMS from being recorded."""
    try:
        return await owner_balance(db)
    except Exception:
        logger.exception("Could not compute the running balance")
        return None


@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "Expense Tracker API",
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post(
    "/api/v1/webhook/sms",
    response_model=WebhookResponse,
    dependencies=[Depends(verify_webhook_secret)],
    tags=["Webhook"],
    summary="Receive and parse bank SMS from iPhone Shortcut",
)
async def ingest_sms_webhook(
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Webhook endpoint triggered by iOS Shortcuts automation when a bank SMS arrives.
    
    1. Validates authentication token.
    2. Hashes message to guarantee idempotency and prevent duplicate entries.
    3. Parses SMS with fast Regex rules, falling back to Gemini AI Agent if uncertain
       (full parse when regex fails, category only when regex cannot categorize).
    4. Persists the transaction in TiDB Cloud (PostgreSQL-compatible).
    5. Returns the updated running balance when one is set in Settings.
    6. After responding, emails an alert if a weekly/monthly spend limit was reached.
    """
    raw_text = (payload.message or "").strip()

    if not raw_text:
        logger.info("Ignored empty webhook message")
        return WebhookResponse(
            status="ignored",
            message="Received empty message. If you are testing via the Play button, add a 'Text' action with dummy SMS content before 'Get Contents of URL'.",
            transaction=None,
        )

    # 1. Deduplication check via SHA-256 hash
    msg_hash = compute_message_hash(raw_text)
    existing_txn_query = select(Transaction).where(Transaction.message_hash == msg_hash)
    existing_result = await db.execute(existing_txn_query)
    existing_txn = existing_result.scalars().first()

    if existing_txn:
        return WebhookResponse(
            status="duplicate",
            message="Transaction already processed and recorded.",
            transaction=TransactionResponse.model_validate(existing_txn),
            balance=await balance_after(db),
        )

    # 2. Parse SMS (Regex first -> Gemini AI fallback)
    parsed: ParsedTransaction = await parse_sms(raw_text, payload.sender)
    if parsed.parsed_by == "ignored":
        return WebhookResponse(status="ignored", message="Message is not a completed transaction.")

    # Determine transaction timestamp
    tx_time = payload.received_at or parsed.transaction_time or datetime.now(timezone.utc)
    tx_time = tx_time.astimezone(timezone.utc)

    # 3. Create database record
    new_txn = Transaction(
        type=parsed.type,
        amount=parsed.amount,
        currency=parsed.currency,
        merchant=parsed.merchant,
        category=parsed.category,
        account_ref=parsed.account_ref,
        balance=parsed.balance,
        reference_id=parsed.reference_id,
        sender=payload.sender,
        raw_message=raw_text,
        message_hash=msg_hash,
        is_parsed=parsed.is_parsed,
        parsed_by=parsed.parsed_by,
        transaction_time=tx_time,
        # Stamped after parsing: the DB's now() is the transaction start, which can be
        # ~30s earlier when Gemini is involved and would let a polling cursor skip the row.
        created_at=datetime.now(timezone.utc),
    )

    db.add(new_txn)
    try:
        await db.commit()
    except IntegrityError:
        # A retry can race the pre-insert check; the unique constraint is authoritative.
        await db.rollback()
        existing_txn = await db.scalar(existing_txn_query)
        if not existing_txn:
            raise
        return WebhookResponse(
            status="duplicate", message="Transaction already processed and recorded.",
            transaction=TransactionResponse.model_validate(existing_txn),
            balance=await balance_after(db),
        )
    await db.refresh(new_txn)
    # Runs after the response is sent, so the iPhone Shortcut never waits on Gmail.
    background_tasks.add_task(check_spend_limits, new_txn.id)

    return WebhookResponse(
        status="success",
        message=f"Transaction logged successfully via {parsed.parsed_by}",
        transaction=TransactionResponse.model_validate(new_txn),
        balance=await balance_after(db),
    )


@app.get(
    "/api/v1/transactions",
    response_model=List[TransactionResponse],
    tags=["Transactions"],
    summary="List recent transactions",
)
async def list_transactions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    created_after: Optional[AwareDatetime] = Query(
        None, description="Only rows recorded at or after this time (inclusive), for cheap polling"
    ),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns transactions ordered by transaction time descending."""
    query = select(Transaction).where(ownership(user))
    if created_after is not None:
        query = query.where(Transaction.created_at >= created_after.astimezone(timezone.utc))
    query = query.order_by(desc(Transaction.transaction_time)).offset(offset).limit(limit)
    result = await db.execute(query)
    transactions = result.scalars().all()
    return [TransactionResponse.model_validate(t) for t in transactions]
