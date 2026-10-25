"""Transactions added by hand from the Activity page. Isolated in-memory SQLite; no network."""
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
import uuid
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import Transaction, User

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
HDFC_UPI_DEBIT = "Sent Rs.450.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629412345678"


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class ManualTransactionTests(unittest.IsolatedAsyncioTestCase):
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

    def sign_in(self, user=None):
        app.dependency_overrides[current_user] = lambda: user or self.owner

    async def add(self, **fields):
        body = {"type": "DEBIT", "amount": "250.00", "merchant": "Chai Point", "transaction_time": ago(minutes=5), **fields}
        return await self.client.post("/api/v1/transactions", json=body, headers=ORIGIN)

    async def test_requires_login_and_same_origin(self):
        self.assertEqual((await self.add()).status_code, 401)
        self.sign_in()
        foreign = await self.client.post("/api/v1/transactions", headers={"Origin": "https://attacker.example"},
                                         json={"type": "DEBIT", "amount": "1", "merchant": "x", "transaction_time": ago(minutes=1)})
        self.assertEqual(foreign.status_code, 403)

    async def test_adds_an_expense_that_shows_up_like_any_other(self):
        self.sign_in()
        response = await self.add(amount="1249.50", merchant="  Blinkit   order  ")
        self.assertEqual(response.status_code, 201, response.text)
        txn = response.json()
        self.assertEqual(
            (txn["type"], txn["amount"], txn["merchant"], txn["parsed_by"], txn["is_parsed"], txn["currency"], txn["account_ref"]),
            ("DEBIT", 1249.5, "Blinkit order", "manual", True, "INR", None),
        )
        self.assertEqual(txn["category"], "Groceries")  # picked from the name, like an SMS
        listed = (await self.client.get("/api/v1/transactions")).json()
        self.assertEqual([t["id"] for t in listed], [txn["id"]])
        # The 5-second poll picks it up too
        polled = (await self.client.get("/api/v1/transactions", params={"created_after": ago(minutes=1)})).json()
        self.assertEqual([t["id"] for t in polled], [txn["id"]])
        async with AsyncSessionLocal() as db:
            stored = await db.get(Transaction, uuid.UUID(txn["id"]))
        self.assertEqual(stored.user_id, self.owner.id)

    async def test_category_choice_and_auto(self):
        self.sign_in()
        chosen = (await self.add(merchant="Swiggy", category="Entertainment")).json()
        self.assertEqual(chosen["category"], "Entertainment")
        income = (await self.add(type="CREDIT", merchant="Freelance project", amount="5000")).json()
        self.assertEqual((income["type"], income["category"]), ("CREDIT", "Uncategorized"))
        cased = (await self.add(category="food & dining")).json()
        self.assertEqual(cased["category"], "Food & Dining")

    async def test_validation(self):
        self.sign_in()
        cases = [
            {"amount": "0"}, {"amount": "-5"}, {"amount": "10.555"}, {"amount": "1e11"},
            {"merchant": "   "}, {"merchant": "x" * 101},
            {"type": "REFUND"}, {"category": "Snacks"},
            {"transaction_time": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()},
            {"transaction_time": "2026-10-21T10:00:00"},  # no timezone
            {"paid_with": "crypto"},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                response = await self.add(**fields)
                self.assertEqual(response.status_code, 422, response.text)
        async with AsyncSessionLocal() as db:
            self.assertEqual(await db.scalar(select(Transaction.id)), None)

    async def test_bank_entries_move_the_balance_and_cash_does_not(self):
        self.sign_in()
        await self.client.put("/api/v1/balance", json={"amount": "10000", "as_of": ago(hours=1)}, headers=ORIGIN)
        await self.add(amount="500", paid_with="bank")
        await self.add(amount="300", paid_with="cash", merchant="Auto rickshaw")
        await self.add(type="CREDIT", amount="1000", paid_with="bank", merchant="Rent share from Priya")
        cash = (await self.add(type="CREDIT", amount="200", paid_with="cash", merchant="Sold old books")).json()
        self.assertEqual(cash["account_ref"], "Cash")
        balance = (await self.client.get("/api/v1/balance")).json()
        self.assertEqual((balance["current"], balance["spent"], balance["received"]), (10500, 500, 1000))

    async def test_expenses_count_toward_spend_limits(self):
        self.sign_in()
        await self.client.put("/api/v1/limits", json={"weekly_limit": "1000", "monthly_limit": None}, headers=ORIGIN)
        await self.add(amount="1200", paid_with="cash")
        limits = (await self.client.get("/api/v1/limits")).json()
        self.assertEqual(limits["weekly"]["spent"], 1200)
        self.assertTrue(limits["weekly"]["alerted"])
        inbox = (await self.client.get("/api/v1/notifications")).json()
        self.assertEqual(inbox["unread"], 1)

    async def test_delete_any_of_your_transactions(self):
        self.sign_in()
        mine = (await self.add()).json()
        sms = (await self.client.post("/api/v1/webhook/sms", json={"message": HDFC_UPI_DEBIT}, headers=HOOK)).json()["transaction"]
        self.sign_in(self.other)
        theirs = (await self.add(merchant="Not yours")).json()
        self.sign_in()

        foreign = await self.client.delete(f"/api/v1/transactions/{mine['id']}", headers={"Origin": "https://attacker.example"})
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual((await self.client.delete(f"/api/v1/transactions/{theirs['id']}", headers=ORIGIN)).status_code, 404)
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertEqual((await self.client.delete(f"/api/v1/transactions/{missing}", headers=ORIGIN)).status_code, 404)

        self.assertEqual((await self.client.delete(f"/api/v1/transactions/{mine['id']}", headers=ORIGIN)).status_code, 204)
        self.assertEqual((await self.client.delete(f"/api/v1/transactions/{sms['id']}", headers=ORIGIN)).status_code, 204)
        self.assertEqual((await self.client.get("/api/v1/transactions")).json(), [])
        self.assertEqual((await self.client.delete(f"/api/v1/transactions/{sms['id']}", headers=ORIGIN)).status_code, 404)
        # A deleted SMS can be recorded again if the Shortcut resends it
        again = (await self.client.post("/api/v1/webhook/sms", json={"message": HDFC_UPI_DEBIT}, headers=HOOK)).json()
        self.assertEqual(again["status"], "success")


if __name__ == "__main__":
    unittest.main()
