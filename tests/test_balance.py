"""Running-balance tests. Isolated in-memory SQLite; never touches the real database or network."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import unittest
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from auth import current_user
from balance import affects_bank_balance
from database import AsyncSessionLocal, Base, engine
from main import app
from models import Transaction, User

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}

HDFC_UPI_DEBIT = "Sent Rs.450.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629412345678\nNot You?\nCall 18002586161/SMS BLOCK UPI to 7308080808"
HDFC_UPI_CREDIT = "Credit Alert!\nRs.2,500.00 credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA rahul.k@okaxis (UPI 629498765432)"
HDFC_CARD_SPEND = "Spent Rs.1,249 On HDFC Bank Card 9012 At AMAZON PAY INDIA On 2026-10-21:13:05:44.Not You? To Block+Reissue Call 18002586161/SMS BLOCK CC 9012 to 7308080808"
HDFC_CARD_BILL_PAID = "Rs.1249.00 debited from HDFC Bank A/c XX4821 on 21-10-26 towards HDFC Bank Credit Card XX9012. Ref 629400000555"


class BankBalanceRuleTests(unittest.TestCase):
    def test_credit_card_alerts_do_not_touch_the_bank_balance(self):
        for sms in [
            HDFC_CARD_SPEND,
            "INR 1,604.00 spent using ICICI Bank Card XX7001 on 01-Oct-26 on ZOMATO. Avl Limit: INR 2,48,396.00.",
            "Rs 649.00 spent on ICICI Bank Card XX7001 on 16-Oct-26 at APOLLO PHARMACY. Avl Lmt: Rs 2,47,747.00.",
            "Txn Rs.199.00\nOn HDFC Bank Card 9012\nAt netflix.upi@hdfcbank\nby UPI 629477778888\nOn 21-10\nNot You?\nCall 18002586161/SMS BLOCK CC 9012 to 7308080808",
            "HDFC Bank Cardmember, Online Payment of Rs.1000 vide Ref# 000XXXX was credited to your card ending 9012 On 08/OCT/2026",
            "Dear Customer, Payment of INR 1,234.00 has been received on your ICICI Bank Credit Card Account 4xxx7001 on 06-OCT-26.",
            "UBER INDIA refund of Rs 120.00 credited to ICICI Bank Credit Card XX7001 on 06-OCT-26. Revised total due Rs 9,999.00",
            "Dear Smart Pay Customer,We have successfully debited your HDFC Bank Credit card ending 9012 to pay your SpayBBPS BESCOM bill for the amount Rs 1450",
        ]:
            with self.subTest(sms=sms[:50]):
                self.assertFalse(affects_bank_balance(sms))

    def test_bank_account_and_debit_card_alerts_count(self):
        for sms in [
            HDFC_UPI_DEBIT,
            HDFC_UPI_CREDIT,
            HDFC_CARD_BILL_PAID,
            "ICICI Bank Acct XX310 debited for Rs 60.00 on 21-Oct-26; Bangalore Metro credited. UPI:629411112222.",
            "Spent Rs.2000 From HDFC Bank Card x5566 At HPCL On 2026-10-20:18:01:12 Bal Rs.8,142.26 Not You? Call 18002586161/SMS BLOCK DC 5566 to 7308080808",
            "Rs 2,340.00 debited via POS from HDFC Bank DC 5566 at DECATHLON on 21Oct26. Avl bal Rs 10,000.00",
        ]:
            with self.subTest(sms=sms[:50]):
                self.assertTrue(affects_bank_balance(sms))


class BalanceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSessionLocal() as db:
            self.owner = User(email="owner@example.com", name="Owner")
            self.other = User(email="other@example.com", name="Other")
            db.add_all([self.owner, self.other])
            await db.commit()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.client.aclose()

    def sign_in(self):
        app.dependency_overrides[current_user] = lambda: self.owner

    async def put_balance(self, amount, **extra):
        return await self.client.put("/api/v1/balance", json={"amount": amount, **extra}, headers=ORIGIN)

    async def sms(self, message):
        response = await self.client.post("/api/v1/webhook/sms", json={"message": message}, headers=HOOK)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_requires_login_and_same_origin(self):
        self.assertEqual((await self.client.get("/api/v1/balance")).status_code, 401)
        self.assertEqual((await self.put_balance(100)).status_code, 401)
        self.sign_in()
        no_origin = await self.client.put("/api/v1/balance", json={"amount": 100})
        foreign = await self.client.put("/api/v1/balance", json={"amount": 100}, headers={"Origin": "https://attacker.example"})
        delete_foreign = await self.client.delete("/api/v1/balance", headers={"Origin": "https://attacker.example"})
        self.assertEqual([no_origin.status_code, foreign.status_code, delete_foreign.status_code], [403, 403, 403])

    async def test_not_configured_returns_null_everywhere(self):
        self.sign_in()
        response = await self.client.get("/api/v1/balance")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json())
        self.assertIsNone((await self.sms(HDFC_UPI_DEBIT))["balance"])

    async def test_set_and_read_balance(self):
        self.sign_in()
        response = await self.put_balance("50000.50")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data["opening_balance"], data["current"], data["currency"], data["counted"]), (50000.5, 50000.5, "INR", 0))
        self.assertEqual((await self.client.get("/api/v1/balance")).json()["current"], 50000.5)

    async def test_webhook_debits_and_credits_move_the_balance_once(self):
        self.sign_in()
        await self.put_balance(50000)
        after_debit = await self.sms(HDFC_UPI_DEBIT)
        self.assertEqual(after_debit["status"], "success")
        self.assertEqual(after_debit["balance"]["current"], 49550)
        after_credit = await self.sms(HDFC_UPI_CREDIT)
        self.assertEqual(after_credit["balance"]["current"], 52050)
        duplicate = await self.sms(HDFC_UPI_DEBIT)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["balance"]["current"], 52050)
        ignored = await self.sms("OTP 123456 for INR 999 purchase")
        self.assertEqual(ignored["status"], "ignored")
        summary = (await self.client.get("/api/v1/balance")).json()
        self.assertEqual((summary["spent"], summary["received"], summary["counted"], summary["current"]), (450, 2500, 2, 52050))

    async def test_card_spend_counts_only_when_the_bill_is_paid(self):
        self.sign_in()
        await self.put_balance(10000)
        spend = await self.sms(HDFC_CARD_SPEND)
        self.assertEqual(spend["status"], "success")
        self.assertEqual((spend["balance"]["current"], spend["balance"]["excluded_card"]), (10000, 1))
        bill = await self.sms(HDFC_CARD_BILL_PAID)
        self.assertEqual(bill["balance"]["current"], 8751)

    async def test_counts_only_owner_parsed_same_currency_after_start(self):
        self.sign_in()
        now = datetime.now(timezone.utc)
        await self.put_balance(1000, as_of=(now - timedelta(minutes=5)).isoformat())
        async with AsyncSessionLocal() as db:
            for i, (kind, amount, minutes_ago, parsed, currency, owner) in enumerate([
                ("DEBIT", 100, 10, True, "INR", self.owner.id),  # before the starting point
                ("DEBIT", 200, 1, True, "INR", None),  # unassigned SMS belong to the owner
                ("CREDIT", 300, 2, True, "INR", self.owner.id),
                ("DEBIT", 999, 1, False, "INR", None),  # needs review
                ("DEBIT", 50, 1, True, "USD", None),  # other currency
                ("DEBIT", 777, 1, True, "INR", self.other.id),  # someone else's
            ]):
                db.add(Transaction(type=kind, amount=amount, currency=currency, is_parsed=parsed, user_id=owner,
                                   raw_message=f"Rs {amount} via a/c XX1234", message_hash=f"seed-{i}",
                                   transaction_time=now - timedelta(minutes=minutes_ago)))
            await db.commit()
        summary = (await self.client.get("/api/v1/balance")).json()
        self.assertEqual((summary["current"], summary["spent"], summary["received"], summary["counted"]), (1100, 200, 300, 2))

    async def test_saving_again_resets_the_starting_point_and_delete_clears(self):
        self.sign_in()
        await self.put_balance(1000)
        self.assertEqual((await self.sms(HDFC_UPI_DEBIT))["balance"]["current"], 550)
        reset = (await self.put_balance(5000)).json()
        self.assertEqual((reset["current"], reset["counted"]), (5000, 0))
        cleared = await self.client.delete("/api/v1/balance", headers=ORIGIN)
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone((await self.client.get("/api/v1/balance")).json())
        self.assertIsNone((await self.sms(HDFC_UPI_CREDIT))["balance"])

    async def test_validation(self):
        self.sign_in()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for body in [
            {"amount": "12.345"},
            {"amount": "1e15"},
            {"amount": 100, "currency": "inr"},
            {"amount": 100, "as_of": future},
            {"amount": 100, "as_of": "2026-10-20T10:00:00"},
        ]:
            with self.subTest(body=body):
                response = await self.client.put("/api/v1/balance", json=body, headers=ORIGIN)
                self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
