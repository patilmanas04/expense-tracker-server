"""Notices (notices.py, upcoming.store_notice, the webhook's ignored branch): the notice rules on synthetic text,
storing, the webhook with storing on and off, and the Phase 0 gate (gated_steps_decision.md, step 21). Every
fixture named SYNTHETIC_* is invented text shaped like the design's examples, not real bank wording. Isolated
in-memory SQLite; email stays off (no Gmail keys); Gemini stays off."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import logging
import re
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

import main
import notices
import parser
import upcoming
from database import AsyncSessionLocal
from limits import IST, _utc
from main import IGNORED_MESSAGE
from models import Transaction, UpcomingPayment
from notices import Notice, notice_key, parse_date, parse_notice
from test_parser import AUTOPAY_DEBIT, AUTOPAY_NOTICE, CARD_STATEMENT, NOT_TRANSACTIONS
from test_post_save import HOOK, NOW, PostSaveTestCase

SYNTHETIC_AUTOPAY = AUTOPAY_NOTICE  # CUJ 1, from test_parser
SYNTHETIC_AUTOPAY_TOMORROW = "Rs 649 will be debited from your HDFC Bank A/c XX1234 tomorrow towards NETFLIX"
SYNTHETIC_CARD_BILL = "Your HDFC Bank Credit Card XX9012 bill of Rs 4,500 is due on 05-Dec-26"  # CUJ 2
SYNTHETIC_STATEMENT = CARD_STATEMENT  # ICICI XX7001, total 4,500.00, min 500.00, due 05-Dec-26
SYNTHETIC_STATEMENT_LABELLED = ("ICICI Bank Credit Card XX7001: Total Amount Due INR 4,500; Minimum Amount Due "
                                "INR 500; Payment due date 05-Dec-26")
SYNTHETIC_MIN_FIRST = ("ICICI Bank Credit Card XX7001: Minimum Amount Due INR 500; Total Amount Due INR 4,500; "
                       "Payment due date 05-Dec-26")
SYNTHETIC_IMMEDIATE = "ICICI Bank Credit Card XX7001: Total Amount Due INR 4,500. Payment due date IMMEDIATE"
SYNTHETIC_BILL_DUE_ICICI = "Your ICICI Bank Credit Card XX7001 bill of Rs 4,500.00 is due on 05-Dec-26"
SYNTHETIC_CARD_AUTOPAY = ("AutoPay for NETFLIX, Rs 649.00 will be charged on 12-12-26 to your HDFC Bank Credit "
                          "Card XX9012")
SYNTHETIC_USD = "AutoPay for NETFLIX, debit of USD 9.99 is scheduled on 12-Dec-26"
SYNTHETIC_OTP = "OTP 123456 for INR 500 purchase"
SYNTHETIC_REQUEST = "PRIYA has requested money of Rs 300 from you on Google Pay. On approving, your a/c will be debited"
DEC_11 = datetime(2026, 12, 11, 12, 0, tzinfo=IST)  # CUJ 1 received
NOV_20 = datetime(2026, 11, 20, 12, 0, tzinfo=IST)  # CUJ 2 received
OCT_20 = datetime(2026, 10, 20, 12, 0, tzinfo=IST)  # before every NOT_TRANSACTIONS date
DEC_12, DEC_5 = date(2026, 12, 12), date(2026, 12, 5)
DAY = timedelta(days=1)

# Masked REAL notice SMS (Phase 0, gated_steps_decision.md step 21), keyed "auto_debit" / "card_bill":
# (text, the Notice it must parse to). Empty until the owner adds them. Each is parsed with sender=None and
# received the day before its due date at 09:00 IST (the due date itself when the text says "today"), and must
# also be ignored by parser.is_non_transaction. Mask account numbers, mandate IDs and names. Turn
# notices.STORE_NOTICES on only in the same commit, after amending the rules in notices.py and the design.
REAL_NOTICES: dict[str, list[tuple[str, Notice]]] = {"auto_debit": [], "card_bill": []}

NETFLIX = Notice("auto_debit", "Netflix", None, Decimal("649.00"), None, DEC_12)


class AutoDebitTests(unittest.TestCase):
    def test_cuj1_scheduled(self):
        self.assertEqual(parse_notice(SYNTHETIC_AUTOPAY, None, DEC_11), NETFLIX)

    def test_tomorrow_towards(self):
        self.assertEqual(parse_notice(SYNTHETIC_AUTOPAY_TOMORROW, None, DEC_11),
                         Notice("auto_debit", "Netflix", "HDFC XX1234", Decimal("649"), None, DEC_12))

    def test_charged_to_card_is_auto_debit(self):
        self.assertEqual(parse_notice(SYNTHETIC_CARD_AUTOPAY, None, DEC_11),
                         Notice("auto_debit", "Netflix", "HDFC XX9012", Decimal("649.00"), None, DEC_12))

    def test_needs_amount_payee_and_date(self):
        for text in ("INR 500 will be debited tomorrow",  # no payee
                     "will be debited on 12-Dec-26 towards NETFLIX",  # no amount
                     "Rs 649 will be debited towards NETFLIX"):  # no date
            with self.subTest(text=text):
                self.assertIsNone(parse_notice(text, None, DEC_11))

    def test_payee_skips(self):
        notice = parse_notice("AutoPay mandate for Rs 649.00 is scheduled for 12-Dec-26 for NETFLIX", None, DEC_11)
        self.assertEqual((notice.payee, notice.amount, notice.due_date), ("Netflix", Decimal("649.00"), DEC_12))
        notice = parse_notice("Rs 500 is to be debited on 12-Dec-26 to ACME", None, DEC_11)  # not "be debited"
        self.assertEqual((notice.payee, notice.amount, notice.due_date), ("Acme", Decimal("500"), DEC_12))

    def test_balance_amount_skipped(self):
        notice = parse_notice("Avl Bal Rs 10,000.00. Rs 649.00 will be debited on 12-Dec-26 for NETFLIX", None, DEC_11)
        self.assertEqual(notice.amount, Decimal("649.00"))

    def test_rupees_only(self):
        self.assertIsNone(parse_notice(SYNTHETIC_USD, None, DEC_11))
        self.assertIsNone(parse_notice("Rs 0.00 will be debited on 12-Dec-26 for NETFLIX", None, DEC_11))

    def test_due_before_received(self):
        self.assertIsNone(parse_notice(SYNTHETIC_AUTOPAY.replace("12-Dec-26", "01-Dec-26"), None, DEC_11))
        notice = parse_notice("Rs 649.00 will be debited today for NETFLIX", None, DEC_11)
        self.assertEqual(notice.due_date, date(2026, 12, 11))

    def test_received_none_uses_clock(self):
        with patch("limits.utc_now", return_value=NOW):  # 21 Oct 2026, 12:00 IST
            notice = parse_notice(SYNTHETIC_AUTOPAY_TOMORROW, None, None)
        self.assertEqual(notice.due_date, date(2026, 10, 22))


class CardBillTests(unittest.TestCase):
    def test_cuj2_bill_of(self):
        self.assertEqual(parse_notice(SYNTHETIC_CARD_BILL, None, NOV_20),
                         Notice("card_bill", "HDFC card XX9012", "HDFC XX9012", Decimal("4500"), None, DEC_5))

    def test_statement_total_and_min(self):
        self.assertEqual(parse_notice(SYNTHETIC_STATEMENT, None, NOV_20),
                         Notice("card_bill", "ICICI card XX7001", "ICICI XX7001", Decimal("4500.00"),
                                Decimal("500.00"), DEC_5))

    def test_labelled_total_and_minimum(self):
        expected = Notice("card_bill", "ICICI card XX7001", "ICICI XX7001", Decimal("4500"), Decimal("500"), DEC_5)
        for text in (SYNTHETIC_STATEMENT_LABELLED, SYNTHETIC_MIN_FIRST):
            with self.subTest(text=text[:50]):
                self.assertEqual(parse_notice(text, None, NOV_20), expected)

    def test_separator_touching_the_date(self):
        # A ":" or "-" label separator may touch the date, as it may touch an amount ("Total Amount Due:Rs 4,500").
        expected = Notice("card_bill", "HDFC card XX9012", "HDFC XX9012", Decimal("4500"), None, DEC_5)
        start = "HDFC Bank Credit Card XX9012 Total Amount Due Rs 4,500. "
        for tail in ("Payment due date:05-Dec-26", "Due Date-05/12/2026", "Due Date: 05-Dec-26",
                     "Due Date - 05/12/2026", "Due Date:2026-12-05", "Due Date-5 Dec"):
            with self.subTest(tail=tail):
                self.assertEqual(parse_notice(start + tail, None, NOV_20), expected)
        self.assertEqual(parse_notice("HDFC Bank Credit Card XX9012 Total Amount Due:Rs 4,500. Due Date:05/12/2026",
                                      None, NOV_20), expected)
        # Still only a separator: a date glued to a letter, a "." or a "/" is not read.
        for tail in ("Due Date.05-Dec-26", "Due Date/05-Dec-26", "Due Date:x05-Dec-26"):
            with self.subTest(tail=tail):
                self.assertIsNone(parse_notice(start + tail, None, NOV_20))

    def test_labels_as_designed(self):
        # The design's labels with no "." after amt/min: "Amt. due", "Total amt. due" and "Min. due" are no labels.
        for text in ("HDFC Bank Credit Card XX9012 Amt. due Rs 4,500 due on 05-Dec-26",
                     "HDFC Bank Credit Card XX9012 Total amt. due Rs 4,500 due on 05-Dec-26"):
            with self.subTest(text=text[29:]):
                self.assertIsNone(parse_notice(text, None, NOV_20))
        notice = parse_notice("HDFC Bank Credit Card XX9012 Amt due Rs 4,500; Min. due Rs 500; due on 05-Dec-26",
                              None, NOV_20)
        self.assertEqual((notice.amount, notice.min_amount), (Decimal("4500"), None))
        notice = parse_notice("HDFC Bank Credit Card XX9012 Total amt due Rs 4,500; Min amt due Rs 500; due on "
                              "05-Dec-26", None, NOV_20)
        self.assertEqual((notice.amount, notice.min_amount), (Decimal("4500"), Decimal("500")))
        # "Min. Amount Due" is no minimum label, and its "Amount Due" is still not read as the total.
        notice = parse_notice("HDFC Bank Credit Card XX9012 Min. Amount Due Rs 500; Total Amount Due Rs 4,500; due "
                              "on 05-Dec-26", None, NOV_20)
        self.assertEqual((notice.amount, notice.min_amount), (Decimal("4500"), None))

    def test_immediate_is_not_stored(self):
        self.assertIsNone(parse_notice(SYNTHETIC_IMMEDIATE, None, NOV_20))
        # A labelled total with no due date: None, with no fall-through to the auto-debit rules.
        self.assertIsNone(parse_notice("Your HDFC Bank Credit Card XX9012 bill of Rs 4,500", None, NOV_20))

    def test_needs_a_card_reference(self):
        self.assertIsNone(parse_notice("Your credit card bill of Rs 4,500 is due on 05-Dec-26", None, NOV_20))

    def test_sender_names_the_bank(self):
        text = "Your Credit Card XX9012 bill of Rs 4,500 is due on 05-Dec-26"
        notice = parse_notice(text, "VK-HDFCBK", NOV_20)
        self.assertEqual((notice.payee, notice.account_ref), ("HDFC card XX9012", "HDFC XX9012"))
        notice = parse_notice(text, None, NOV_20)
        self.assertEqual((notice.payee, notice.account_ref), ("Card XX9012", "XX9012"))


class OutcomeTests(unittest.TestCase):
    # Every test_parser.NOT_TRANSACTIONS entry, classified: a new entry fails until it is listed here.
    EXPECTED = {
        "OTP 123456 for INR 500 purchase": None,
        "INR 500 payment failed": None,
        "INR 500 will be debited tomorrow": None,
        "Rs.250 will be credited to your HDFC Bank A/c within 5 working days": None,
        SYNTHETIC_REQUEST: None,
        "Your HDFC Bank Credit Card XX9012 bill of Rs 4,500 is due on 05-Nov-26": ("card_bill", date(2026, 11, 5)),
        "Congratulations! You are pre-approved for a Personal Loan of Rs 5,00,000. Apply now": None,
        "ICICI BANK NEFT Transaction with reference number IN0000123 for Rs. 2,500.00 has been credited to the "
        "beneficiary account on 21-10-2026": None,
        "RTGS Money Deposited~INR 2,00,000.00~To SAMPLE NAME~Txn No: HDFCR000111~On 21-10-2026~-HDFC Bank": None,
        AUTOPAY_NOTICE: ("auto_debit", DEC_12),
        CARD_STATEMENT: ("card_bill", DEC_5),
    }

    def test_not_transactions(self):
        self.assertEqual(set(NOT_TRANSACTIONS), set(self.EXPECTED))
        for text in NOT_TRANSACTIONS:
            with self.subTest(text=text[:50]):
                notice = parse_notice(text, None, OCT_20)
                self.assertEqual(notice and (notice.kind, notice.due_date), self.EXPECTED[text])

    def test_requests_and_failures_rejected(self):
        self.assertIsNone(parse_notice(SYNTHETIC_REQUEST, None, OCT_20))
        self.assertIsNone(parse_notice("Rs 649.00 AutoPay for NETFLIX scheduled on 12-Dec-26 failed", None, DEC_11))


class DateTests(unittest.TestCase):
    def test_no_year_rolls_forward(self):
        self.assertEqual(parse_date("due 05 Jan", date(2026, 12, 20)), date(2027, 1, 5))

    def test_formats(self):
        for text in ("12/12/26", "12-Dec-2026", "12Dec26", "2026-12-12", "12.12.2026", "12 Dec 2026", "12 dec 26",
                     "12-12-26", "12.12.26", "12-Dec-26", "12 Dec 10:30"):
            with self.subTest(text=text):
                self.assertEqual(parse_date(text, date(2026, 12, 1)), DEC_12)

    def test_no_year_this_year(self):
        self.assertEqual(parse_date("on 5 Dec", date(2026, 12, 1)), date(2026, 12, 5))

    def test_invalid_date(self):
        self.assertIsNone(parse_date("31-02-26", date(2026, 12, 1)))
        self.assertIsNone(parse_date("no date here", date(2026, 12, 1)))

    def test_key_statement_and_reminder_match(self):
        statement = parse_notice(SYNTHETIC_STATEMENT, None, NOV_20)
        reminder = parse_notice(SYNTHETIC_BILL_DUE_ICICI, None, NOV_20)
        self.assertEqual(notice_key(statement), notice_key(reminder))
        later = parse_notice(SYNTHETIC_BILL_DUE_ICICI.replace("05-Dec-26", "06-Dec-26"), None, NOV_20)
        self.assertNotEqual(notice_key(later), notice_key(reminder))
        self.assertRegex(notice_key(statement), r"^[0-9a-f]{64}$")

    def test_key_auto_debit_uses_merchant_key(self):
        upper = Notice("auto_debit", "NETFLIX", "HDFC XX1234", Decimal("649"), None, DEC_12)
        self.assertEqual(notice_key(NETFLIX), notice_key(upper))
        spotify = Notice("auto_debit", "Spotify", None, Decimal("649.00"), None, DEC_12)
        self.assertNotEqual(notice_key(NETFLIX), notice_key(spotify))


class NoticeTestCase(PostSaveTestCase):
    async def rows(self) -> list[UpcomingPayment]:
        async with AsyncSessionLocal() as db:
            return list((await db.scalars(select(UpcomingPayment))).all())

    async def transactions(self) -> int:
        async with AsyncSessionLocal() as db:
            return await db.scalar(select(func.count()).select_from(Transaction))

    async def post(self, text: str, received_at: datetime, sender: Optional[str] = None) -> dict:
        body = {"message": text, "received_at": received_at.isoformat()}
        if sender:
            body["sender"] = sender
        response = await self.client.post("/api/v1/webhook/sms", json=body, headers=HOOK)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class StoreTests(NoticeTestCase):
    async def store(self, notice: Notice, raw: str = "raw", received_at: Optional[datetime] = DEC_11) -> bool:
        async with AsyncSessionLocal() as db:
            return await upcoming.store_notice(db, notice, raw, received_at)

    async def test_insert_then_same_key(self):
        self.assertTrue(await self.store(NETFLIX, SYNTHETIC_AUTOPAY))
        [row] = await self.rows()
        self.assertEqual((row.kind, row.payee, row.account_ref, row.amount, row.min_amount, row.due_date,
                          row.notice_key, row.raw_message, _utc(row.created_at), row.dismissed_at),
                         ("auto_debit", "Netflix", None, Decimal("649.00"), None, DEC_12, notice_key(NETFLIX),
                          SYNTHETIC_AUTOPAY, DEC_11, None))
        self.assertFalse(await self.store(NETFLIX, SYNTHETIC_AUTOPAY))
        self.assertEqual(len(await self.rows()), 1)

    async def test_min_filled_once(self):
        reminder = parse_notice(SYNTHETIC_BILL_DUE_ICICI, None, NOV_20)
        self.assertIsNone(reminder.min_amount)
        self.assertTrue(await self.store(reminder))
        self.assertFalse(await self.store(parse_notice(SYNTHETIC_STATEMENT, None, NOV_20)))
        [row] = await self.rows()
        self.assertEqual((row.amount, row.min_amount), (Decimal("4500.00"), Decimal("500.00")))
        other = Notice("card_bill", "ICICI card XX7001", "ICICI XX7001", Decimal("4600"), Decimal("600"), DEC_5)
        self.assertFalse(await self.store(other))
        [row] = await self.rows()
        self.assertEqual((row.amount, row.min_amount), (Decimal("4500.00"), Decimal("500.00")))

    async def test_received_none_uses_clock(self):
        self.assertTrue(await self.store(NETFLIX, received_at=None))
        [row] = await self.rows()
        self.assertEqual(_utc(row.created_at), NOW)

    async def test_insert_race(self):
        self.assertTrue(await self.store(NETFLIX))
        async with AsyncSessionLocal() as db:
            with patch.object(upcoming, "_by_key", AsyncMock(return_value=None)):
                self.assertFalse(await upcoming.store_notice(db, NETFLIX, "raw", DEC_11))
            self.assertEqual(await db.scalar(select(func.count()).select_from(UpcomingPayment)), 1)  # still usable
        self.assertEqual(len(await self.rows()), 1)


class WebhookOnTests(NoticeTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        switch = patch.object(notices, "STORE_NOTICES", True)
        switch.start()
        self.addCleanup(switch.stop)

    async def test_cuj1_saved(self):
        body = await self.post(SYNTHETIC_AUTOPAY, DEC_11)
        self.assertEqual((body["status"], body["message"], body["transaction"]),
                         ("ignored", "Saved as an upcoming payment.", None))
        self.assertEqual(await self.transactions(), 0)
        [row] = await self.rows()
        self.assertEqual((row.kind, row.payee, row.amount, row.due_date, row.raw_message),
                         ("auto_debit", "Netflix", Decimal("649.00"), DEC_12, SYNTHETIC_AUTOPAY))
        body = await self.post(SYNTHETIC_AUTOPAY, DEC_11)
        self.assertEqual((body["status"], body["message"]), ("ignored", "Upcoming payment already saved."))
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(await self.transactions(), 0)

    async def test_bill_then_statement_one_row(self):
        self.assertEqual((await self.post(SYNTHETIC_BILL_DUE_ICICI, NOV_20))["message"],
                         "Saved as an upcoming payment.")
        self.assertEqual((await self.post(SYNTHETIC_STATEMENT, NOV_20))["message"], "Upcoming payment already saved.")
        [row] = await self.rows()
        self.assertEqual((row.payee, row.amount, row.min_amount), ("ICICI card XX7001", Decimal("4500.00"),
                                                                   Decimal("500.00")))

    async def test_store_failure_is_plain_ignored(self):
        with patch("upcoming.store_notice", AsyncMock(side_effect=RuntimeError("boom"))), \
                self.assertLogs("main", "ERROR") as logs:
            body = await self.post(SYNTHETIC_AUTOPAY, DEC_11)
        self.assertEqual(body, {"status": "ignored", "message": IGNORED_MESSAGE, "transaction": None,
                                "balance": None})
        self.assertIn("Could not handle a notice SMS", logs.output[0])
        self.assertEqual(await self.rows(), [])

    async def test_parse_failure_is_plain_ignored(self):
        with patch("notices.parse_notice", side_effect=RuntimeError("boom")), self.assertLogs("main", "ERROR"):
            body = await self.post(SYNTHETIC_AUTOPAY, DEC_11)
        self.assertEqual((body["status"], body["message"]), ("ignored", IGNORED_MESSAGE))
        self.assertEqual(await self.rows(), [])

    async def test_non_notices_store_nothing(self):
        for text in (SYNTHETIC_OTP, SYNTHETIC_REQUEST):
            with self.subTest(text=text[:30]):
                body = await self.post(text, DEC_11)
                self.assertEqual((body["status"], body["message"]), ("ignored", IGNORED_MESSAGE))
        self.assertEqual(await self.rows(), [])
        body = await self.post(AUTOPAY_DEBIT, DEC_11)
        self.assertEqual(body["status"], "success", body)
        self.assertEqual(await self.transactions(), 1)
        self.assertEqual(await self.rows(), [])

    async def test_stored_notice_is_listed(self):
        body = await self.post(SYNTHETIC_AUTOPAY_TOMORROW, NOW)
        self.assertEqual(body["message"], "Saved as an upcoming payment.")
        response = await self.client.get("/api/v1/upcoming")
        self.assertEqual(response.status_code, 200, response.text)
        [item] = response.json()["items"]
        self.assertEqual((item["kind"], item["payee"], item["amount"], item["date"], item["status"]),
                         ("auto_debit", "Netflix", "649.00", "2026-10-22", "upcoming"))
        body = await self.post("Rs 649.00 debited via UPI AutoPay from HDFC Bank A/c XX1234 to NETFLIX", NOW + DAY)
        self.assertEqual(body["status"], "success", body)
        [item] = (await self.client.get("/api/v1/upcoming")).json()["items"]
        self.assertEqual((item["payee"], item["status"]), ("Netflix", "paid"))


class WebhookOffTests(NoticeTestCase):
    FORBIDDEN = ("649", "4,500", "4500", "500.00", "NETFLIX", "Netflix", "XX7001", "7001", "12-Dec", "05-Dec",
                 "2026-12", "Mandate")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        switch = patch.object(notices, "STORE_NOTICES", False)
        switch.start()
        self.addCleanup(switch.stop)

    async def test_cuj1_off_unchanged(self):
        body = await self.post(SYNTHETIC_AUTOPAY, DEC_11)
        self.assertEqual(body, {"status": "ignored", "message": "Message is not a completed transaction.",
                                "transaction": None, "balance": None})
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.transactions(), 0)

    async def test_off_logs_kind_and_bank_only(self):
        with self.assertLogs("main", "INFO") as logs:
            await self.post(SYNTHETIC_AUTOPAY, DEC_11)
            await self.post(SYNTHETIC_STATEMENT, NOV_20)
        lines = [record.getMessage() for record in logs.records]
        self.assertEqual([line for line in lines if line.startswith("Notice recognised")], [
            "Notice recognised, not stored (STORE_NOTICES off): kind=auto_debit bank=HDFC",
            "Notice recognised, not stored (STORE_NOTICES off): kind=card_bill bank=ICICI",
        ])
        for line in lines:
            for word in self.FORBIDDEN:
                self.assertNotIn(word, line)
        self.assertEqual(await self.rows(), [])

    def test_off_line_reaches_the_server_log(self):
        # Uvicorn sets up only its own loggers; without a handler here the INFO line would be dropped.
        self.assertTrue(main.logger.handlers)
        self.assertTrue(main.logger.isEnabledFor(logging.INFO))

    async def test_off_other_ignored_sms_log_nothing(self):
        with self.assertNoLogs("main", "INFO"):
            for text in (SYNTHETIC_OTP, "INR 500 will be debited tomorrow"):
                body = await self.post(text, DEC_11)
                self.assertEqual((body["status"], body["message"]), ("ignored", IGNORED_MESSAGE))

    async def test_off_does_not_call_store(self):
        with patch("upcoming.store_notice", AsyncMock()) as store:
            body = await self.post(SYNTHETIC_CARD_BILL, NOV_20)
        self.assertEqual(body["message"], IGNORED_MESSAGE)
        store.assert_not_awaited()


class GateTests(unittest.TestCase):
    def _check_real(self):
        for kind, samples in REAL_NOTICES.items():
            for text, expected in samples:
                with self.subTest(kind=kind, text=text[:50]):
                    self.assertEqual(expected.kind, kind)
                    self.assertTrue(parser.is_non_transaction(text), "the webhook would save it as a transaction")
                    back = 0 if re.search(r"\btoday\b", text, re.I) else 1
                    received = datetime.combine(expected.due_date - timedelta(days=back), time(9), tzinfo=IST)
                    self.assertEqual(parse_notice(text, None, received), expected)

    def test_store_notices_needs_real_samples(self):
        if not notices.STORE_NOTICES:
            return  # storing is off: nothing reaches Home, Safe to spend or the reminder
        for kind in ("auto_debit", "card_bill"):
            self.assertTrue(REAL_NOTICES[kind], f"STORE_NOTICES is on but REAL_NOTICES[{kind!r}] is empty: add "
                            "masked real HDFC/ICICI samples first (gated_steps_decision.md, step 21)")
        self._check_real()

    @unittest.skipUnless(any(REAL_NOTICES.values()), "pending real HDFC/ICICI notice SMS (Phase 0)")
    def test_real_notices_parse(self):
        self._check_real()


if __name__ == "__main__":
    unittest.main()
