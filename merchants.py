"""How a merchant limit decides which payments are its own. Pure functions, no database: the
server is the only place matching happens, so every screen and alert agrees.

A limit counts a payment when the payment's merchant name contains the limit's name as the same
whole words, with the same upper and lower case: "Swiggy" counts "Swiggy" and "Swiggy Ltd", but not
"SWIGGY", "swiggy" or "Swiggyone". Matching runs in Python so no database's text comparison rules
(MySQL compares without case by default) can change the answer. Names are compared in Unicode NFC,
so an accented letter typed as one character or as a letter plus a combining accent is the same.

"share_word" is the loose name rule for matching a notice or a refund to a payment: two texts match when
they share one meaningful word, in any case.
"""
import hashlib
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Optional


def clean(name: str) -> str:
    """In NFC, trimmed, with runs of spaces collapsed: how a typed name is stored and compared."""
    return " ".join(unicodedata.normalize("NFC", name).split())


def _invalid(c: str) -> bool:
    # A control character (such as NUL, which Postgres can't store in text), half of a UTF-16
    # surrogate pair (which can't be encoded as UTF-8), or U+FFFD: what a decoder leaves for bytes
    # it couldn't read, and what `mark_invalid_characters` puts in place of the others. Spaces,
    # tabs and newlines are fine: `clean` turns them into single spaces.
    return c == "\ufffd" or (not c.isspace() and unicodedata.category(c) in ("Cc", "Cs"))


def has_invalid_characters(name: str) -> bool:
    """True when the name can't be a merchant's: see `_invalid`."""
    return any(_invalid(c) for c in name)


def mark_invalid_characters(name: str) -> str:
    """The name with each invalid character replaced by U+FFFD, so it can always be encoded."""
    return "".join("\ufffd" if _invalid(c) else c for c in name)


def _word_character(c: str) -> bool:
    # Combining marks (category M) are part of their letter: the vowel signs and virama in
    # "स्विगी", or a decomposed accent.
    return c.isalnum() or unicodedata.category(c).startswith("M")


def words(name: Optional[str]) -> list[str]:
    """The name's words, in NFC with case kept; anything that isn't a letter, digit or combining
    mark (in any script) splits them, and a word needs at least one letter or digit. "Unknown" is
    the placeholder for an SMS that couldn't be read, so it has none, as in
    `categorize.merchant_key`."""
    if not name:
        return []
    name = unicodedata.normalize("NFC", name)
    if " ".join(name.split()).casefold() == "unknown":
        return []
    found = "".join(c if _word_character(c) else " " for c in name).split()
    return [word for word in found if any(c.isalnum() for c in word)]


def matches(limit_name: str, name: Optional[str]) -> bool:
    """True when the limit's words appear, in order and next to each other, among the name's."""
    wanted, have = words(limit_name), words(name)
    if not wanted:
        return False
    size = len(wanted)
    return any(have[i:i + size] == wanted for i in range(len(have) - size + 1))


def name_hash(name: str) -> str:
    """SHA-256 of the name in NFC, otherwise exact. Unique keys use it because a hash keeps case on
    every database, where a text column under a case-insensitive collation would treat "Swiggy"
    and "SWIGGY" as one. Callers hash a `clean` name, so it's the same however the name was typed."""
    return hashlib.sha256(unicodedata.normalize("NFC", name).encode("utf-8")).hexdigest()


def _by_payments(payments: Mapping[str, int]) -> list[tuple[str, int]]:
    # Most payments first, then by name with case, so the order is the same on every run.
    return sorted(payments.items(), key=lambda item: (-item[1], item[0]))


def known_names(rows: Iterable[tuple], typed: Iterable[str]) -> list[tuple[str, int]]:
    """[(name, payments)] from `(merchant, …)` payment rows plus typed names (0 payments if never
    paid). Names are cleaned, so "Amazon  Pay" (as the AI parser may store it) is listed, and
    counted, as "Amazon Pay"; names that differ in case stay separate; names with no words or
    with invalid characters are left out."""
    # A name with invalid characters (say, from a badly decoded SMS) couldn't be saved as a limit.
    payments = Counter(clean(row[0]) for row in rows if words(row[0]) and not has_invalid_characters(row[0]))
    for name in typed:
        if words(name) and not has_invalid_characters(name):
            payments.setdefault(clean(name), 0)
    return _by_payments(payments)


def meter(limit_name: str, rows: Iterable[tuple[Optional[str], Decimal, datetime]],
          windows: Mapping[str, tuple[datetime, datetime]]) -> tuple[Decimal, Decimal, list[str]]:
    """(weekly spent, monthly spent, counted names) for one limit. Rows are `(merchant, amount,
    aware UTC time)`; windows map "weekly" and "monthly" to their UTC (start, exclusive end).
    The names are every matching name in the rows, cleaned as in `known_names`, most payments first."""
    spent = {period: Decimal(0) for period in ("weekly", "monthly")}
    counted = Counter()
    for merchant, amount, when in rows:
        if not matches(limit_name, merchant):
            continue
        counted[clean(merchant)] += 1
        for period in spent:
            start, end = windows[period]
            if start <= when < end:
                spent[period] += Decimal(str(amount))
    cents = Decimal("0.01")
    return (spent["weekly"].quantize(cents), spent["monthly"].quantize(cents),
            [name for name, _ in _by_payments(counted)])


# Words that say nothing about who was paid: banks, company suffixes, payment and SMS boilerplate, the
# text of a transaction added by hand ("Added by hand in Folio") and the placeholder for an unread SMS.
COMMON_WORDS = frozenset({
    "bank", "hdfc", "icici", "sbi", "axis", "kotak", "india", "pvt", "ltd", "limited", "private",
    "payment", "payments", "services", "service", "the", "and", "for", "upi", "autopay", "mandate",
    "card", "credit", "debit", "debited", "account", "towards", "from", "your", "dear", "customer",
    "added", "hand", "folio", "com", "www", "inr", "unknown",
})


def _meaningful_words(text: Optional[str]) -> set[str]:
    # Words of 3+ characters with a letter, compared without case, common words left out.
    return {word.casefold() for word in words(text) if len(word) >= 3 and any(c.isalpha() for c in word)} - COMMON_WORDS


def share_word(a: Optional[str], b: Optional[str]) -> bool:
    """True when the two texts share a meaningful word: 3+ characters with a letter, compared without case,
    not in COMMON_WORDS. "NETFLIX" and "Netflix Upi" match; "HDFC Bank" and "HDFC Bank Credit Card" don't.
    Callers may pass a merchant joined with its SMS text, so a payment whose merchant wasn't read still
    matches on the SMS."""
    return bool(_meaningful_words(a) & _meaningful_words(b))
