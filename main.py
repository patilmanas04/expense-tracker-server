import os
import secrets
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Literal, Optional
import uuid

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, init_db
from models import Transaction, User
import categorize
from parser import CATEGORIES, UNCATEGORIZED, ParsedTransaction, Recalled, compute_message_hash, detect_category, parse_sms
from auth import FRONTEND_URL, current_user, same_origin, router as auth_router
from balance import CASH_ACCOUNT, BalanceSummary, owner_balance, router as balance_router
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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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
    notes: Optional[str]  # your own note, added after the transaction arrives
    transaction_time: datetime
    created_at: datetime
    updated_at: datetime  # moves on every edit; the dashboard polls by it

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

    # 2. Parse SMS (Regex first -> Gemini AI fallback); the category can come from merchant memory
    async def recall(merchant: Optional[str], tx_type: str) -> Optional[Recalled]:
        try:
            return await categorize.recall_category(db, merchant, tx_type)
        except Exception:
            await db.rollback()  # an aborted transaction would stop the SMS being saved
            raise

    parsed: ParsedTransaction = await parse_sms(raw_text, payload.sender, recall=recall)
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
        # earlier when Gemini is involved and would let a polling cursor skip the row.
        created_at=(now := datetime.now(timezone.utc)),
        updated_at=now,
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
    if parsed.ai_category_pending:  # Gemini was too slow; try again with more time
        background_tasks.add_task(categorize.retry_ai_category, new_txn.id)

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
    updated_after: Optional[AwareDatetime] = Query(
        None, description="Only rows added or changed at or after this time (inclusive): new SMS, edits, late categories"
    ),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns transactions ordered by transaction time descending."""
    query = select(Transaction).where(ownership(user))
    if created_after is not None:
        query = query.where(Transaction.created_at >= created_after.astimezone(timezone.utc))
    if updated_after is not None:
        query = query.where(Transaction.updated_at >= updated_after.astimezone(timezone.utc))
    query = query.order_by(desc(Transaction.transaction_time)).offset(offset).limit(limit)
    result = await db.execute(query)
    transactions = result.scalars().all()
    return [TransactionResponse.model_validate(t) for t in transactions]


# ---------- Transactions added by hand (Activity → Add) ----------

MANUAL = "manual"  # parsed_by for transactions added by hand
EDITED = "edited"  # parsed_by for an SMS that needed review, completed by hand
MAX_TRANSACTION_AMOUNT = Decimal("9999999999.99")  # Numeric(12, 2)
MAX_NOTE_LENGTH = 500


class _TransactionChecks(BaseModel):
    """Validation shared by adding and editing a transaction."""

    @field_validator("merchant", check_fields=False)
    @classmethod
    def _merchant(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter who it was paid to or received from.")
        return value

    @field_validator("notes", check_fields=False)
    @classmethod
    def _notes(cls, value: Optional[str]) -> Optional[str]:
        # An empty note clears it (sent as "", since leaving a field out keeps it).
        return None if value is None else value.strip()

    @field_validator("category", check_fields=False)
    @classmethod
    def _category(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        match = next((c for c in CATEGORIES if c.casefold() == value.strip().casefold()), None)
        if match is None:
            raise ValueError("Unknown category.")
        return match

    @field_validator("transaction_time", check_fields=False)
    @classmethod
    def _not_in_future(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        # A few minutes of slack for a phone clock that runs fast.
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("The date and time can't be in the future.")
        return value.astimezone(timezone.utc)


class ManualTransactionInput(_TransactionChecks):
    type: Literal["DEBIT", "CREDIT"]
    amount: Decimal = Field(gt=0, le=MAX_TRANSACTION_AMOUNT, max_digits=12, decimal_places=2)
    merchant: str = Field(max_length=100, description="Who it was paid to or received from")
    category: Optional[str] = Field(default=None, description="One of the app's categories; omit to pick one from the name")
    transaction_time: AwareDatetime
    paid_with: Literal["bank", "cash"] = Field(default="bank", description="Cash never changes the bank balance")


class TransactionEdit(_TransactionChecks):
    """Only the fields you send change."""

    type: Optional[Literal["DEBIT", "CREDIT"]] = None
    amount: Optional[Decimal] = Field(default=None, gt=0, le=MAX_TRANSACTION_AMOUNT, max_digits=12, decimal_places=2)
    merchant: Optional[str] = Field(default=None, max_length=100)
    category: Optional[str] = None
    transaction_time: Optional[AwareDatetime] = None
    paid_with: Optional[Literal["bank", "cash"]] = Field(default=None, description="Only for transactions added by hand")
    notes: Optional[str] = Field(default=None, max_length=MAX_NOTE_LENGTH, description='Your own note; "" removes it')


@app.post(
    "/api/v1/transactions",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(same_origin)],
    tags=["Transactions"],
    summary="Add a transaction by hand",
)
async def add_transaction(
    body: ManualTransactionInput,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """For cash and anything without an SMS. Counts in totals and spend limits like an SMS;
    cash entries don't change the bank balance. A category you pick is remembered for the merchant;
    "Auto" uses that memory, the keyword rules, then Gemini in the background."""
    now = datetime.now(timezone.utc)
    if body.category:
        category = body.category
        await categorize.remember(db, user.id, body.merchant, body.type, category)
    else:
        keyword = detect_category(body.merchant, tx_type=body.type)
        category = await categorize.auto_category(db, user.id, body.merchant, body.type, keyword)
    txn = Transaction(
        user_id=user.id,
        type=body.type,
        amount=body.amount,
        currency="INR",
        merchant=body.merchant,
        category=category,
        account_ref=CASH_ACCOUNT if body.paid_with == "cash" else None,
        raw_message="Added by hand in Folio",
        is_parsed=True,
        parsed_by=MANUAL,
        transaction_time=body.transaction_time,
        created_at=now,
        updated_at=now,
    )
    db.add(txn)
    await db.commit()
    await db.refresh(txn)
    background_tasks.add_task(check_spend_limits, txn.id)
    if category == UNCATEGORIZED and os.getenv("GEMINI_API_KEY"):
        background_tasks.add_task(categorize.retry_ai_category, txn.id)
    return TransactionResponse.model_validate(txn)


@app.patch(
    "/api/v1/transactions/{transaction_id}",
    response_model=TransactionResponse,
    dependencies=[Depends(same_origin)],
    tags=["Transactions"],
    summary="Edit a transaction",
)
async def edit_transaction(
    transaction_id: uuid.UUID,
    body: TransactionEdit,
    background_tasks: BackgroundTasks,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change any detail of an SMS or a hand-added entry, or add your own note ("" removes it).
    A new category is remembered for the merchant, so later SMS from it get the same one. Giving
    an SMS that needed review an amount makes it count."""
    txn = await db.scalar(select(Transaction).where(Transaction.id == transaction_id, ownership(user)))
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found.")
    changes = body.model_dump(exclude_none=True)
    if not changes:
        return TransactionResponse.model_validate(txn)
    if "paid_with" in changes:
        if txn.parsed_by != MANUAL:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Only transactions you added by hand can switch between bank and cash.")
        txn.account_ref = CASH_ACCOUNT if changes.pop("paid_with") == "cash" else None
    if "notes" in changes:
        changes["notes"] = changes["notes"] or None
    for field, value in changes.items():
        setattr(txn, field, value)
    if not txn.is_parsed and txn.amount > 0:
        txn.is_parsed, txn.parsed_by = True, EDITED
    if "category" in changes:
        await categorize.remember(db, user.id, txn.merchant, txn.type, txn.category)
    txn.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(txn)
    if txn.is_parsed and txn.type == "DEBIT" and changes.keys() - {"notes"}:
        background_tasks.add_task(check_spend_limits, txn.id)  # a bigger amount can cross a limit
    return TransactionResponse.model_validate(txn)


@app.delete(
    "/api/v1/transactions/{transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(same_origin)],
    tags=["Transactions"],
    summary="Delete a transaction",
)
async def delete_transaction(
    transaction_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deletes an SMS or a hand-added entry for good. If the Shortcut sends the same SMS again,
    it's recorded again."""
    txn = await db.scalar(select(Transaction).where(Transaction.id == transaction_id, ownership(user)))
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found.")
    await db.delete(txn)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
