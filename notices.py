"""Notices: SMS about money that will leave later, read from an SMS the parser ignored. Pure; no database.

Two kinds:
* **auto_debit**: a pre-debit / AutoPay notice ("AutoPay for NETFLIX, debit of Rs 649.00 is scheduled on
  12-Dec-26"): future debit wording with a rupee amount, a payee and a date.
* **card_bill**: a card statement or bill reminder ("Your HDFC Bank Credit Card XX9012 bill of Rs 4,500 is due on
  05-Dec-26"): the word "card", a card reference, a labelled total due (and a minimum, if given) and a due date.
Requests, OTPs and failed payments are never notices.

The rules follow docs/designs/upcoming_payments.md → Notice rules and are **not yet checked against real HDFC/ICICI
SMS**. STORE_NOTICES stays False until real masked samples are in tests/test_notices.py::REAL_NOTICES and the rules
here are amended in the same commit (docs/designs/gated_steps_decision.md, step 21). While it is off the webhook
stores nothing and logs only the kind and bank of a recognised notice.
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Optional

import limits
from categorize import merchant_key
from limits import IST
from parser import (CANDIDATE, CURRENCY_PATTERN, FAILED_RE, MONEY_RE, REQUEST_RE, _is_balance_context, _to_decimal,
                    clean_merchant_name, detect_bank, extract_account_ref)

# A code constant, not an env var or a setting: turned on only in the commit that adds real samples.
STORE_NOTICES = False

AUTO_DEBIT, CARD_BILL = "auto_debit", "card_bill"
INR_SYMBOLS = {"RS", "RS.", "INR", "₹"}
MAX_AMOUNT = Decimal("9999999999.99")  # Numeric(12, 2)
NO_YEAR_BACK = timedelta(days=60)  # "05 Jan" received 20 Dec → next January

FUTURE_DEBIT_RE = re.compile(
    r"\bwill be (?:debited|deducted|charged)\b|\bto be debited\b|\b(?:is|are) scheduled (?:on|for)\b", re.I)
CARD_WORD_RE = re.compile(r"\bcard\b", re.I)
SEP_RE = re.compile(r"\s*(?:[:\-]|\bis\b|\bof\b)?\s*", re.I)  # between a label and its value
TOTAL_LABEL_RE = re.compile(
    r"\b(?:total\s+(?:(?:amount|amt)\s+)?due|amount\s+due|amt\s+due|bill\s+of|bill\s+amount)\b", re.I)
MIN_LABEL_RE = re.compile(r"\bmin(?:imum)?\s+(?:(?:amount|amt)\s+)?due\b", re.I)  # the design's labels, no "amt."
# "Minimum Amount Due" (or "Min. Amount Due", which is no label) is not a total; the "." here only rejects more.
MIN_BEFORE_RE = re.compile(r"\bmin(?:imum)?\.?\s+$", re.I)
DUE_LABEL_RE = re.compile(r"\bdue\s+(?:on|by|date)\b|\bpay(?:ment)?\s+by\b", re.I)
MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DATE_RE = re.compile(
    r"(?<![\w.,/:-])(?:"
    r"(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2})(?!\d)"                                  # 2026-12-12
    r"|(?P<nd>\d{1,2})(?P<ns>[-/.])(?P<nm>\d{1,2})(?P=ns)(?P<ny>\d{4}|\d{2})(?![\d:])"  # 12-12-26, 12/12/2026, 12.12.26
    r"|(?P<md>\d{1,2})(?:-|\s+)?(?P<mm>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(?![a-z])"
    r"(?:(?:-|\s+)?(?P<my>\d{4}|\d{2})(?![\d:]))?"                                      # 12-Dec-26, 12 Dec 2026, 12Dec26, 12 Dec
    r")", re.I)
DAY_WORD_RE = re.compile(r"\b(today|tomorrow)\b", re.I)
PAYEE_LABELS = ("towards", "for", r"to(?!\s+be\b)", "by", "at")  # design order; "is to be debited" is not a payee
SKIP_FIRST_WORDS = {"mandate", "autopay", "auto", "e-mandate", "standing", "your", "a/c", "the", "upi"}
CURRENCY_START_RE = re.compile(CURRENCY_PATTERN, re.I)  # matched at the candidate's start


@dataclass(frozen=True)
class Notice:
    kind: Literal["auto_debit", "card_bill"]
    payee: str  # "Netflix" / "HDFC card XX9012"
    account_ref: Optional[str]  # "HDFC XX9012" (card bills: the card; auto-debits: the account, display only)
    amount: Decimal  # auto-debit amount / card bill total due
    min_amount: Optional[Decimal]  # card bills only, when given
    due_date: date  # IST calendar date


def _received_day(received_at: Optional[datetime]) -> date:
    return (received_at or limits.utc_now()).astimezone(IST).date()


def _build(year_text: Optional[str], day: int, month: int, received: date) -> Optional[date]:
    try:
        if year_text is None:
            found = date(received.year, month, day)
            if found < received - NO_YEAR_BACK:
                found = date(received.year + 1, month, day)
            return found
        year = int(year_text)
        return date(2000 + year if len(year_text) == 2 else year, month, day)
    except ValueError:
        return None


def _date_from(match: re.Match, received: date) -> Optional[date]:
    if match.group("iy"):
        try:
            return date(int(match.group("iy")), int(match.group("im")), int(match.group("id")))
        except ValueError:
            return None
    if match.group("nd"):
        return _build(match.group("ny"), int(match.group("nd")), int(match.group("nm")), received)
    return _build(match.group("my"), int(match.group("md")), MONTHS[match.group("mm").lower()], received)


def parse_date(text: str, received: date) -> Optional[date]:
    """The first date in the text, day first. No year: the received year, or the next one when that date is more
    than 60 days before the received date. Two-digit years are 20yy. An impossible date gives None."""
    match = DATE_RE.search(text)
    return _date_from(match, received) if match else None


def _date_after(text: str, pos: int, received: date) -> Optional[date]:
    """The date directly after a label (only a separator between); nothing further on. A ":" or "-" separator may
    touch the date ("Due Date:05/12/2026"), as it may touch an amount."""
    sep = SEP_RE.match(text, pos)
    start = sep.end()
    if start > sep.start() and text[start - 1] in ":-":  # DATE_RE's lookbehind would reject the separator itself
        text, start = text[start:], 0
    match = DATE_RE.match(text, start)
    return _date_from(match, received) if match else None


def _inr(match: re.Match) -> Optional[Decimal]:
    """A positive rupee amount that fits the table; anything else (another currency, 0, too large) is None."""
    if match.group(1).upper() not in INR_SYMBOLS:
        return None
    amount = _to_decimal(match.group(2))
    return amount if amount is not None and 0 < amount <= MAX_AMOUNT else None


def _labelled(text: str, label_re: re.Pattern, skip_min: bool) -> Optional[Decimal]:
    """The amount directly after the first label that has one."""
    for label in label_re.finditer(text):
        if skip_min and MIN_BEFORE_RE.search(text[max(0, label.start() - 10):label.start()]):
            continue
        money = MONEY_RE.match(text, SEP_RE.match(text, label.end()).end())
        if money:
            return _inr(money)
    return None


def _payee(text: str) -> Optional[str]:
    for label in PAYEE_LABELS:
        for match in re.finditer(r"\b" + label + r"\s+" + CANDIDATE, text, re.I):
            raw = match.group(1).strip()
            if (not raw or raw[0].isdigit() or CURRENCY_START_RE.match(raw)
                    or raw.split()[0].casefold() in SKIP_FIRST_WORDS):
                continue
            name = clean_merchant_name(raw)
            if name is not None and merchant_key(name) is not None:
                return name
    return None


def _first_amount(text: str) -> Optional[Decimal]:
    """The first amount that is not a balance or limit; it must be rupees."""
    for money in MONEY_RE.finditer(text):
        if _is_balance_context(text[max(0, money.start() - 30):money.start()], include_due=False):
            continue
        return _inr(money)
    return None


def parse_notice(text: str, sender: Optional[str], received_at: Optional[datetime]) -> Optional[Notice]:
    """The auto-debit or card-bill notice in an SMS the parser ignored, or None (upcoming_payments.md → Notice
    rules). received_at missing means now; due dates before the received IST date give None."""
    received = _received_day(received_at)
    if REQUEST_RE.search(text) or FAILED_RE.search(text):
        return None
    bank = detect_bank(text, sender)
    ref = extract_account_ref(text, bank)

    if CARD_WORD_RE.search(text) and ref and (total := _labelled(text, TOTAL_LABEL_RE, skip_min=True)) is not None:
        minimum = _labelled(text, MIN_LABEL_RE, skip_min=False)
        label = DUE_LABEL_RE.search(text)  # only the first due label counts
        due = _date_after(text, label.end(), received) if label else None
        if due is None or due < received:
            return None  # "due date IMMEDIATE" or a past bill: not stored, and not an auto-debit either
        digits = ref.split()[-1]
        payee = f"{bank} card {digits}" if bank else f"Card {digits}"
        return Notice(CARD_BILL, payee, ref, total, minimum, due)

    if not FUTURE_DEBIT_RE.search(text):
        return None
    amount = _first_amount(text)
    payee = _payee(text)
    day_word = DAY_WORD_RE.search(text)
    if day_word:
        due = received + timedelta(days=1) if day_word.group(1).lower() == "tomorrow" else received
    else:
        due = parse_date(text, received)
    if amount is None or payee is None or due is None or due < received:
        return None
    return Notice(AUTO_DEBIT, payee, ref, amount, None, due)


def notice_key(notice: Notice) -> str:
    """One row per bill or debit: kind | card (card bills) or payee (auto-debits) | due date, as SHA-256 hex."""
    ref = notice.account_ref if notice.kind == CARD_BILL else merchant_key(notice.payee)
    return hashlib.sha256(f"{notice.kind}|{ref}|{notice.due_date.isoformat()}".encode("utf-8")).hexdigest()
