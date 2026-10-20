import hashlib
import asyncio
import logging
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)
AMOUNT_PATTERN = r"([0-9]+(?:,[0-9]+)*(?:\.[0-9]{1,2})?)(?![0-9.])"


class ParsedTransaction(BaseModel):
    """Normalized transaction structure output by regex or AI parser."""

    type: Literal["DEBIT", "CREDIT"] = Field(description="DEBIT or CREDIT")
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2, description="Transaction amount in positive decimal")
    currency: str = Field(default="INR", pattern="^[A-Z]{3}$", description="Currency ISO code")
    merchant: Optional[str] = Field(default=None, max_length=255, description="Merchant, counterparty, or store name")
    category: Optional[str] = Field(default="Uncategorized", max_length=100, description="Expense category")
    account_ref: Optional[str] = Field(default=None, max_length=100, description="Masked card or account number, e.g., XX1234")
    balance: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2, description="Available balance after transaction if available")
    reference_id: Optional[str] = Field(default=None, max_length=100, description="UPI reference, UTR, or bank transaction reference ID")
    transaction_time: Optional[datetime] = Field(default=None, description="Date and time of transaction")
    is_parsed: bool = Field(default=True)
    parsed_by: str = Field(default="regex", description="'regex' or 'ai_agent'")
    confidence: float = Field(default=1.0, description="Confidence score from 0.0 to 1.0")


# Category keywords mapping for rule-based categorization
CATEGORY_KEYWORDS = {
    "Food & Dining": [
        "swiggy", "zomato", "starbucks", "mcdonald", "dominos", "pizza", "burger",
        "cafe", "restaurant", "eatclub", "chai", "diner", "kfc", "subway", "barbeque",
    ],
    "Groceries": [
        "blinkit", "zepto", "instamart", "bigbasket", "dmart", "spencer",
        "supermarket", "grocery", "nature basket", "dunzo", "milk",
    ],
    "Shopping": [
        "amazon", "flipkart", "myntra", "zara", "h&m", "nykaa", "ajio",
        "tata cliq", "meesho", "apple", "croma", "reliance digital", "ikea",
    ],
    "Travel & Commute": [
        "uber", "ola", "rapido", "metro", "irctc", "makemytrip", "goibibo",
        "easemytrip", "indigo", "air india", "fuel", "petrol", "hpcl", "bpcl",
        "iocil", "shell", "fastag", "toll",
    ],
    "Bills & Utilities": [
        "electricity", "bescom", "tneb", "water", "broadband", "airtel", "jio",
        "vodafone", "vi", "act fibernet", "gas", "rent", "maintenance", "dth",
    ],
    "Entertainment": [
        "netflix", "spotify", "bookmyshow", "prime", "youtube", "hotstar",
        "steam", "playstation", "sony liv", "cinema", "pvr", "inox",
    ],
    "Investments & Finance": [
        "zerodha", "groww", "indmoney", "mutual fund", "sip", "kuvera",
        "upstox", "angel one", "coin", "etmoney",
    ],
    "Health & Fitness": [
        "pharmacy", "apollo", "medplus", "1mg", "pharmeasy", "cult.fit",
        "gym", "hospital", "clinic", "practo",
    ],
}


def compute_message_hash(text: str) -> str:
    """Computes SHA-256 hash of normalized SMS text for deduplication."""
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def detect_category(merchant: Optional[str]) -> str:
    """Detects category based on merchant name keywords."""
    if not merchant:
        return "Uncategorized"

    m_lower = merchant.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(re.search(r"\b" + re.escape(kw) + r"\b", m_lower) for kw in keywords):
            return category

    return "General"


def clean_merchant_name(raw: str) -> str:
    """Cleans up raw merchant strings extracted from SMS."""
    # Split on common stop words like ' on <date>', ' via ', ' ref ', ' upi ', ' avl '
    split_pattern = r"\s+(?:on\s+\d|dated\s+|via\s+|ref\b|upi\b|avl\b|avail\b|bal\b)"
    parts = re.split(split_pattern, raw, flags=re.IGNORECASE)
    cleaned = parts[0]

    # Remove common prefixes like 'VPA', 'INF*', 'UPI/', 'to '
    cleaned = re.sub(r"^(vpa\s*[:\-]?\s*|inf\s*\*\s*|upi\s*/\s*|to\s+)", "", cleaned, flags=re.IGNORECASE)
    # If UPI ID like swiggy@icici, take the username part before @
    if "@" in cleaned:
        cleaned = cleaned.split("@")[0]
    # Remove trailing punctuation or symbols
    cleaned = re.sub(r"[\s\.\,\-\/]+$", "", cleaned).strip()
    # Capitalize words nicely
    return cleaned.title() if len(cleaned) <= 35 else cleaned[:35].title()


def parse_sms_with_regex(sms_text: str) -> Optional[ParsedTransaction]:
    """Attempts to parse a bank SMS using robust regex patterns.
    
    Returns ParsedTransaction if successfully extracted amount and type, else None.
    """
    text = sms_text.strip()
    lower_text = text.lower()

    # 1. Determine Transaction Type
    tx_type: Optional[str] = None
    if re.search(r"\b(credited|deposited|refund(?:ed)?)\b", lower_text):
        tx_type = "CREDIT"
    elif re.search(r"\b(debited|spent|charged|paid|withdrawn|purchased?|transferred to|sent to)\b", lower_text):
        tx_type = "DEBIT"
    elif re.search(r"\b(received|refund|added|credited|deposited|credit)\b", lower_text):
        tx_type = "CREDIT"

    if not tx_type:
        return None

    # 2. Extract Amount & Currency
    # Look for patterns like "INR 450.00", "Rs. 1,200", "Rs 500", "₹350.50"
    amount_match = re.search(
        r"(?:inr|rs\.?|₹|\$)\s*" + AMOUNT_PATTERN,
        text,
        re.IGNORECASE,
    )
    if not amount_match:
        # Fallback: amount followed by INR/Rs
        amount_match = re.search(
            AMOUNT_PATTERN + r"\s*(?:inr|rs\.?|₹)",
            text,
            re.IGNORECASE,
        )

    if not amount_match:
        return None

    amount_str = amount_match.group(1).replace(",", "")
    try:
        amount = Decimal(amount_str)
    except Exception:
        return None

    currency = "INR"
    if "$" in amount_match.group(0):
        currency = "USD"

    # 3. Extract Account / Card reference
    account_ref = None
    acct_match = re.search(
        r"(?:a/c|acct|account|card|ending (?:with|in)?)\s*(?:no\.?)?\s*[*Xx]*([0-9]{3,6})",
        text,
        re.IGNORECASE,
    )
    if acct_match:
        account_ref = f"XX{acct_match.group(1)}"

    # 4. Extract Available Balance
    balance = None
    bal_match = re.search(
        r"(?:avl|avail|available)?\s*(?:balance|bal|lmt|limit)[:\s]*(?:inr|rs\.?|₹|\$)?\s*" + AMOUNT_PATTERN,
        text,
        re.IGNORECASE,
    )
    if bal_match:
        try:
            balance = Decimal(bal_match.group(1).replace(",", ""))
        except Exception:
            balance = None

    # 5. Extract Reference ID / UPI Ref
    reference_id = None
    ref_match = re.search(
        r"(?:upi(?:\s*ref(?:\s*no)?)?|ref\s*(?:no)?|utr|txn(?:\s*id)?)[;:\s-]+([0-9a-zA-Z]{6,20})",
        text,
        re.IGNORECASE,
    )
    if ref_match:
        reference_id = ref_match.group(1).strip()

    # 6. Extract Merchant / Beneficiary
    merchant = None
    # Common formats: "to VPA swiggy@icici", "at ZOMATO", "towards SWIGGY", "Info: AMAZON", "by SALARY"
    merchant_match = re.search(
        r"(?:towards|to\s+vpa|to|at|info\s*:\s*|info\s+|paid\s+to|by)\s+([a-zA-Z0-9@\.\-_& ]{2,35})",
        text,
        re.IGNORECASE,
    )
    if merchant_match:
        candidate = merchant_match.group(1).strip()
        # Avoid matching words like 'your', 'A/C', 'A/c', etc.
        if not re.match(r"^(your|a/c|account|card)\b", candidate, re.IGNORECASE):
            merchant = clean_merchant_name(candidate)

    category = detect_category(merchant)

    return ParsedTransaction(
        type=tx_type,
        amount=amount,
        currency=currency,
        merchant=merchant,
        category=category,
        account_ref=account_ref,
        balance=balance,
        reference_id=reference_id,
        transaction_time=datetime.now(timezone.utc),
        is_parsed=True,
        parsed_by="regex",
        confidence=0.95 if merchant else 0.80,
    )


async def parse_sms_with_gemini(sms_text: str) -> ParsedTransaction:
    """Gemini AI fallback parser for complex, multi-line, or non-standard bank SMS.
    
    Uses google.genai with structured JSON response schema.
    """
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured in .env")

    client = genai.Client(api_key=api_key)

    prompt = f"""
You are a financial transaction analyzer. Parse the following bank SMS notification into structured JSON.

SMS:
\"\"\"{sms_text}\"\"\"

Instructions:
1. type: Must be "DEBIT" or "CREDIT".
2. amount: Number (float/decimal) representing the transaction amount (positive).
3. currency: 3-letter currency code, e.g. "INR" or "USD".
4. merchant: Name of the merchant, person, store, or service involved (clean name like "Swiggy", "Amazon", "Uber"). If unknown, null.
5. category: One of ["Food & Dining", "Groceries", "Shopping", "Travel & Commute", "Bills & Utilities", "Entertainment", "Investments & Finance", "Health & Fitness", "Transfer", "General"].
6. account_ref: Last 4 digits of card/account if present (e.g. "XX1234"), or null.
7. balance: Remaining account balance or credit limit if mentioned, else null.
8. reference_id: UPI reference number, UTR number, or transaction ID if mentioned, else null.
"""

    async with client.aio as async_client:
        response = await asyncio.wait_for(
            async_client.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ParsedTransaction,
                    temperature=0.1,
                ),
            ),
            timeout=20,
        )

    parsed_json = json.loads(response.text)
    parsed_json["parsed_by"] = "ai_agent"
    parsed_json["is_parsed"] = True
    parsed_json["confidence"] = 0.99
    parsed_json["transaction_time"] = datetime.now(timezone.utc)

    return ParsedTransaction(**parsed_json)


async def parse_sms(sms_text: str) -> ParsedTransaction:
    """Master SMS parser: Runs fast regex first; falls back to Gemini if regex fails
    or cannot extract core fields (amount, type).
    """
    # Never interpret OTPs, failed payments or payment requests as completed transactions.
    if re.search(r"\b(otp|one.time password|verification code|declined|failed|unsuccessful|payment request|collect request|will be debited|due date|payment due)\b", sms_text, re.IGNORECASE):
        return ParsedTransaction(type="DEBIT", amount=Decimal(0), is_parsed=False, parsed_by="ignored", confidence=0)
    regex_result = None
    # 1. Try Regex first
    try:
        regex_result = parse_sms_with_regex(sms_text)
        if regex_result and regex_result.amount > 0:
            return regex_result
    except Exception:
        logger.warning("Regex parser could not validate the message")

    # 2. Fallback to Gemini AI Agent
    try:
        ai_result = await parse_sms_with_gemini(sms_text)
        if ai_result.amount <= 0:
            raise ValueError("No positive transaction amount")
        return ai_result
    except Exception:
        logger.warning("AI parsing unavailable; preserving message for review")
        # Return fallback with whatever regex managed, or unparsed placeholder
        if regex_result:
            return regex_result

        return ParsedTransaction(
            type="DEBIT",
            amount=Decimal("0.00"),
            currency="INR",
            merchant="Unknown",
            category="Uncategorized",
            is_parsed=False,
            parsed_by="failed",
            confidence=0.0,
            transaction_time=datetime.now(timezone.utc),
        )
