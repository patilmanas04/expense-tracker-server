"""Categories that stick: merchant memory, the background AI retry, and editing transactions.
Isolated in-memory SQLite; Gemini is always faked, so nothing leaves the machine."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import itertools
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

import categorize
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import MerchantCategory, Transaction, User
from parser import detect_category

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
GEMINI_ON = {"GEMINI_API_KEY": "test-key"}
_refs = itertools.count(666000000001)


def upi_debit(amount, merchant):
    return (f"Sent Rs.{amount}\nFrom HDFC Bank A/C *3304\nTo {merchant}\nOn 22/10/26\nRef {next(_refs)}\n"
            "Not You?\nCall 18002586161/SMS BLOCK UPI to 7308080808")


def upi_credit(amount, handle):
    return f"Credit Alert!\nRs.{amount} credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA {handle}@okaxis (UPI {next(_refs)})"


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class HungerBoxKeywordTests(unittest.TestCase):
    def test_hungerbox_is_food(self):
        for name in ["HungerBox", "Hungerbox Pvt Ltd", "HUNGER BOX"]:
            with self.subTest(name=name):
                self.assertEqual(detect_category(name), "Food & Dining")


class DbTestCase(unittest.IsolatedAsyncioTestCase):
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
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(app.dependency_overrides.clear)

    def sign_in(self, user=None):
        app.dependency_overrides[current_user] = lambda: user or self.owner

    async def sms(self, message):
        response = await self.client.post("/api/v1/webhook/sms", json={"message": message}, headers=HOOK)
        body = response.json()
        self.assertEqual(body["status"], "success", response.text)
        return body["transaction"]

    async def seed(self, merchant, category, kind="DEBIT", user_id=None, parsed_by="regex+ai"):
        async with AsyncSessionLocal() as db:
            db.add(Transaction(type=kind, amount=Decimal("50"), merchant=merchant, category=category, user_id=user_id,
                               raw_message="fixture", message_hash=uuid.uuid4().hex, is_parsed=True, parsed_by=parsed_by,
                               transaction_time=datetime.now(timezone.utc) - timedelta(days=1)))
            await db.commit()

    async def row(self, transaction_id):
        async with AsyncSessionLocal() as db:
            return await db.get(Transaction, uuid.UUID(transaction_id))

    async def edit(self, transaction_id, **fields):
        return await self.client.patch(f"/api/v1/transactions/{transaction_id}", json=fields, headers=ORIGIN)


class MerchantMemoryTests(DbTestCase):
    async def test_earlier_payments_decide_the_category_without_ai(self):
        await self.seed("Qwikly Hub", "Food & Dining")
        with patch.dict(os.environ, GEMINI_ON), patch("parser.categorize_with_gemini", new_callable=AsyncMock) as ai:
            txn = await self.sms(upi_debit("40.00", "Qwikly Hub"))
        ai.assert_not_called()
        self.assertEqual((txn["category"], txn["parsed_by"]), ("Food & Dining", "regex+history"))

    async def test_the_latest_earlier_payment_wins_and_unknowns_are_skipped(self):
        await self.seed("Qwikly Hub", "Shopping")
        await self.seed("Qwikly Hub", "Food & Dining")  # written later
        await self.seed("Qwikly Hub", "Uncategorized")  # never a memory
        txn = await self.sms(upi_debit("40.00", "QWIKLY  hub"))
        self.assertEqual(txn["category"], "Food & Dining")

    async def test_memory_is_kept_per_direction(self):
        await self.seed("Qwikly Hub", "Food & Dining", kind="DEBIT")
        with patch.dict(os.environ, GEMINI_ON), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, return_value="Transfer") as ai:
            txn = await self.sms(upi_credit("500.00", "qwikly.hub"))
        ai.assert_awaited_once()
        self.assertEqual((txn["type"], txn["category"]), ("CREDIT", "Transfer"))

    async def test_another_users_history_is_not_used(self):
        await self.seed("Qwikly Hub", "Food & Dining", user_id=self.other.id)
        txn = await self.sms(upi_debit("40.00", "Qwikly Hub"))
        self.assertEqual(txn["category"], "Uncategorized")

    async def test_your_choice_beats_the_keyword_rules(self):
        self.sign_in()
        first = await self.sms(upi_debit("300.00", "Swiggy"))
        self.assertEqual(first["category"], "Food & Dining")
        response = await self.edit(first["id"], category="Groceries")
        self.assertEqual(response.status_code, 200, response.text)
        later = await self.sms(upi_debit("120.00", "Swiggy"))
        self.assertEqual((later["category"], later["parsed_by"]), ("Groceries", "regex+user"))
        async with AsyncSessionLocal() as db:
            rule = await db.scalar(select(MerchantCategory))
        self.assertEqual((rule.merchant_key, rule.type, rule.category, rule.user_id), ("swiggy", "DEBIT", "Groceries", self.owner.id))

    async def test_choosing_uncategorized_forgets_the_merchant(self):
        self.sign_in()
        first = await self.sms(upi_debit("300.00", "Swiggy"))
        await self.edit(first["id"], category="Groceries")
        await self.edit(first["id"], category="Uncategorized")
        later = await self.sms(upi_debit("120.00", "Swiggy"))
        self.assertEqual((later["category"], later["parsed_by"]), ("Food & Dining", "regex"))

    async def test_a_memory_failure_never_loses_an_sms(self):
        with patch("categorize.recall_category", new_callable=AsyncMock, side_effect=RuntimeError("db hiccup")):
            txn = await self.sms(upi_debit("40.00", "Qwikly Hub"))
        self.assertEqual((txn["amount"], txn["category"]), (40, "Uncategorized"))

    async def test_added_by_hand_uses_memory_and_remembers_explicit_choices(self):
        self.sign_in()
        await self.seed("Qwikly Hub", "Food & Dining")
        auto = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": "DEBIT", "amount": "80", "merchant": "Qwikly Hub", "transaction_time": ago(minutes=2)})
        self.assertEqual(auto.json()["category"], "Food & Dining")
        chosen = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": "DEBIT", "amount": "80", "merchant": "Orbit Bazaar", "category": "Shopping", "transaction_time": ago(minutes=2)})
        self.assertEqual(chosen.json()["category"], "Shopping")
        txn = await self.sms(upi_debit("99.00", "Orbit Bazaar"))
        self.assertEqual((txn["category"], txn["parsed_by"]), ("Shopping", "regex+user"))


class BackgroundRetryTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        delays = patch("categorize.RETRY_DELAYS", (0, 0, 0))
        delays.start()
        self.addCleanup(delays.stop)

    async def test_slow_gemini_saves_the_sms_then_fills_in_the_category(self):
        self.sign_in()
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        with patch.dict(os.environ, GEMINI_ON), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, side_effect=TimeoutError), \
                patch("categorize.categorize_with_gemini", new_callable=AsyncMock, return_value="Food & Dining") as retry:
            txn = await self.sms(upi_debit("25.00", "Qwikly Hub"))
        self.assertEqual(txn["category"], "Uncategorized")  # what the iPhone got back, without waiting
        retry.assert_awaited_once()
        self.assertEqual(retry.await_args.kwargs["timeout"], categorize.RETRY_TIMEOUT)
        stored = await self.row(txn["id"])
        self.assertEqual((stored.category, stored.parsed_by), ("Food & Dining", "regex+ai"))
        # The dashboard's 5-second poll sees the change
        polled = (await self.client.get("/api/v1/transactions", params={"updated_after": before.isoformat()})).json()
        self.assertEqual([(t["id"], t["category"]) for t in polled], [(txn["id"], "Food & Dining")])

    async def test_retries_then_gives_up_quietly(self):
        with patch.dict(os.environ, GEMINI_ON), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, side_effect=TimeoutError), \
                patch("categorize.categorize_with_gemini", new_callable=AsyncMock, side_effect=TimeoutError) as retry:
            txn = await self.sms(upi_debit("25.00", "Qwikly Hub"))
        self.assertEqual(retry.await_count, 3)
        self.assertEqual((await self.row(txn["id"])).category, "Uncategorized")

    async def test_an_unsure_answer_is_final(self):
        with patch.dict(os.environ, GEMINI_ON), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, side_effect=TimeoutError), \
                patch("categorize.categorize_with_gemini", new_callable=AsyncMock, return_value="Uncategorized") as retry:
            await self.sms(upi_debit("25.00", "Qwikly Hub"))
        self.assertEqual(retry.await_count, 1)

    async def test_no_retry_when_gemini_answered_inline(self):
        with patch.dict(os.environ, GEMINI_ON), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, return_value="Uncategorized"), \
                patch("categorize.categorize_with_gemini", new_callable=AsyncMock) as retry:
            await self.sms(upi_debit("25.00", "Qwikly Hub"))
        retry.assert_not_called()

    async def test_your_edit_while_retrying_wins(self):
        async def slow_answer(sms_text, merchant, tx_type, timeout):
            async with AsyncSessionLocal() as db:  # you edit the transaction while Gemini thinks
                await db.execute(update(Transaction).values(category="Shopping", updated_at=datetime.now(timezone.utc) + timedelta(seconds=1)))
                await db.commit()
            return "Food & Dining"
        with patch.dict(os.environ, GEMINI_ON), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, side_effect=TimeoutError), \
                patch("categorize.categorize_with_gemini", side_effect=slow_answer):
            txn = await self.sms(upi_debit("25.00", "Qwikly Hub"))
        self.assertEqual((await self.row(txn["id"])).category, "Shopping")

    async def test_added_by_hand_is_categorised_in_the_background_too(self):
        self.sign_in()
        with patch.dict(os.environ, GEMINI_ON), \
                patch("categorize.categorize_with_gemini", new_callable=AsyncMock, return_value="Food & Dining"):
            response = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
                "type": "DEBIT", "amount": "60", "merchant": "Qwikly Hub", "transaction_time": ago(minutes=1)})
        stored = await self.row(response.json()["id"])
        self.assertEqual((stored.category, stored.parsed_by), ("Food & Dining", "manual"))

    async def test_without_a_key_nothing_is_retried(self):
        with patch("parser.categorize_with_gemini", new_callable=AsyncMock) as inline, \
                patch("categorize.categorize_with_gemini", new_callable=AsyncMock) as retry:
            await self.sms(upi_debit("25.00", "Qwikly Hub"))
        inline.assert_not_called()
        retry.assert_not_called()


class EditTransactionTests(DbTestCase):
    async def test_requires_login_and_same_origin(self):
        txn = await self.sms(upi_debit("450.00", "Swiggy"))
        self.assertEqual((await self.edit(txn["id"], category="Groceries")).status_code, 401)
        self.sign_in()
        foreign = await self.client.patch(f"/api/v1/transactions/{txn['id']}", json={"category": "Groceries"},
                                          headers={"Origin": "https://attacker.example"})
        self.assertEqual(foreign.status_code, 403)

    async def test_edit_every_field_of_an_sms(self):
        self.sign_in()
        await self.client.put("/api/v1/balance", json={"amount": "10000", "as_of": ago(days=2)}, headers=ORIGIN)
        txn = await self.sms(upi_debit("450.00", "Swiggy"))
        when = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0)
        response = await self.edit(txn["id"], amount="500.50", merchant="  Swiggy   Genie ", category="Travel & Commute",
                                   transaction_time=when.isoformat())
        self.assertEqual(response.status_code, 200, response.text)
        edited = response.json()
        self.assertEqual((edited["amount"], edited["merchant"], edited["category"], edited["parsed_by"]),
                         (500.5, "Swiggy Genie", "Travel & Commute", "regex"))
        self.assertEqual(datetime.fromisoformat(edited["transaction_time"]).replace(tzinfo=timezone.utc), when)
        self.assertGreater(edited["updated_at"], txn["updated_at"])
        self.assertEqual((await self.client.get("/api/v1/balance")).json()["current"], 9499.5)
        flipped = (await self.edit(txn["id"], type="CREDIT")).json()
        self.assertEqual(flipped["type"], "CREDIT")
        self.assertEqual((await self.client.get("/api/v1/balance")).json()["current"], 10500.5)

    async def test_validation(self):
        self.sign_in()
        txn = await self.sms(upi_debit("450.00", "Swiggy"))
        for fields in [{"merchant": "  "}, {"merchant": "x" * 101}, {"category": "Snacks"}, {"amount": "0"},
                       {"amount": "1.234"}, {"type": "REFUND"}, {"transaction_time": "2026-10-21T10:00:00"},
                       {"transaction_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                       {"paid_with": "cash"}]:  # bank SMS can't become cash
            with self.subTest(fields=fields):
                self.assertEqual((await self.edit(txn["id"], **fields)).status_code, 422)
        self.assertEqual((await self.edit(str(uuid.uuid4()), category="Groceries")).status_code, 404)
        self.sign_in(self.other)
        theirs = (await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": "DEBIT", "amount": "5", "merchant": "Not yours", "transaction_time": ago(minutes=1)})).json()
        self.sign_in()
        self.assertEqual((await self.edit(theirs["id"], category="Groceries")).status_code, 404)

    async def test_switch_a_hand_added_entry_between_bank_and_cash(self):
        self.sign_in()
        await self.client.put("/api/v1/balance", json={"amount": "1000", "as_of": ago(hours=1)}, headers=ORIGIN)
        txn = (await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": "DEBIT", "amount": "100", "merchant": "Chai Point", "transaction_time": ago(minutes=1)})).json()
        self.assertEqual((await self.client.get("/api/v1/balance")).json()["current"], 900)
        cash = (await self.edit(txn["id"], paid_with="cash")).json()
        self.assertEqual(cash["account_ref"], "Cash")
        self.assertEqual((await self.client.get("/api/v1/balance")).json()["current"], 1000)
        bank = (await self.edit(txn["id"], paid_with="bank")).json()
        self.assertIsNone(bank["account_ref"])

    async def test_completing_an_sms_that_needs_review(self):
        self.sign_in()
        response = await self.client.post("/api/v1/webhook/sms", headers=HOOK,
                                          json={"message": "HDFC Bank: A/c XX4821 debited towards merchant ZX payment"})
        txn = response.json()["transaction"]
        self.assertFalse(txn["is_parsed"])
        just_named = (await self.edit(txn["id"], merchant="Corner Store")).json()
        self.assertFalse(just_named["is_parsed"])  # still no amount, so still needs review
        done = (await self.edit(txn["id"], amount="120", category="Groceries")).json()
        self.assertEqual((done["is_parsed"], done["parsed_by"], done["amount"], done["category"]), (True, "edited", 120, "Groceries"))

    async def test_an_edit_that_crosses_a_limit_notifies(self):
        self.sign_in()
        await self.client.put("/api/v1/limits", json={"weekly_limit": "1000", "monthly_limit": None}, headers=ORIGIN)
        txn = await self.sms(upi_debit("450.00", "Swiggy"))
        self.assertEqual((await self.client.get("/api/v1/notifications")).json()["unread"], 0)
        await self.edit(txn["id"], amount="1200")
        self.assertEqual((await self.client.get("/api/v1/notifications")).json()["unread"], 1)

    async def test_an_empty_edit_changes_nothing(self):
        self.sign_in()
        txn = await self.sms(upi_debit("450.00", "Swiggy"))
        same = await self.edit(txn["id"])
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.json(), txn)

    async def test_poll_by_update_time(self):
        self.sign_in()
        old = await self.sms(upi_debit("450.00", "Swiggy"))
        await self.sms(upi_debit("10.00", "Zomato"))
        mark = datetime.now(timezone.utc)
        await self.edit(old["id"], category="Groceries")
        polled = (await self.client.get("/api/v1/transactions", params={"updated_after": mark.isoformat()})).json()
        self.assertEqual([(t["id"], t["category"]) for t in polled], [(old["id"], "Groceries")])
        naive = await self.client.get("/api/v1/transactions", params={"updated_after": "2026-10-21T10:00:00"})
        self.assertEqual(naive.status_code, 422)


if __name__ == "__main__":
    unittest.main()
