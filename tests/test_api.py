"""Isolated integration tests. Never connect to or modify the configured real database."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ.pop("GEMINI_API_KEY", None)

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from database import Base, engine, AsyncSessionLocal
from models import Transaction, User
from main import app
from auth import current_user
from authlib.integrations.base_client import OAuthError
from sqlalchemy.exc import IntegrityError
from database import get_db
from parser import ParsedTransaction, parse_sms, parse_sms_with_regex


class ParserTests(unittest.IsolatedAsyncioTestCase):
    def test_full_amounts_and_balances(self):
        for amount, expected in [("1234.56", "1234.56"), ("1,23,456.78", "123456.78"), ("12,345", "12345")]:
            p = parse_sms_with_regex(f"INR {amount} debited from a/c XX1234 at ZOMATO. Avl Bal INR 54321.50")
            self.assertEqual(p.amount, Decimal(expected))
            self.assertEqual(p.balance, Decimal("54321.50"))

    def test_credit_card_is_not_income(self):
        p = parse_sms_with_regex("INR 2500 spent on your credit card XX1234 at AMAZON")
        self.assertEqual(p.type, "DEBIT")

    def test_refund_to_credit_card(self):
        p = parse_sms_with_regex("INR 2500 credited to your credit card XX1234 refund from AMAZON")
        self.assertEqual(p.type, "CREDIT")

    async def test_otp_failed_and_scheduled_messages_are_ignored(self):
        for message in ["OTP 123456 for INR 500 purchase", "INR 500 payment failed", "INR 500 will be debited tomorrow"]:
            parsed = await parse_sms(message)
            self.assertEqual(parsed.parsed_by, "ignored")

    async def test_parser_exception_preserves_review_record(self):
        with patch("parser.parse_sms_with_regex", side_effect=ValueError), patch("parser.parse_sms_with_gemini", side_effect=RuntimeError):
            parsed = await parse_sms("Unknown format")
            self.assertFalse(parsed.is_parsed)

    async def test_regex_without_merchant_avoids_ai(self):
        with patch("parser.parse_sms_with_gemini", new_callable=AsyncMock) as ai:
            parsed = await parse_sms("INR 1234 debited from your account XX1234")
            self.assertEqual(parsed.amount, Decimal(1234))
            ai.assert_not_called()


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSessionLocal() as db:
            self.owner = User(email="owner@example.com", name="Owner")
            self.other = User(email="other@example.com", name="Other")
            db.add_all([self.owner, self.other]); await db.commit()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.client.aclose()

    async def seed(self):
        async with AsyncSessionLocal() as db:
            for i, (kind, amount, currency, parsed, owner) in enumerate([
                ("DEBIT", 250, "INR", True, None), ("CREDIT", 1000, "INR", True, self.owner.id),
                ("DEBIT", 999, "INR", False, None), ("DEBIT", 100, "USD", True, None),
                ("DEBIT", 777, "INR", True, self.other.id),
            ]):
                db.add(Transaction(type=kind, amount=amount, currency=currency, merchant=f"Merchant {i}", category="Food & Dining", user_id=owner, is_parsed=parsed, raw_message="fixture", message_hash=str(i), transaction_time=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)))
            await db.commit()

    async def test_reads_require_login(self):
        for endpoint in ["/api/v1/transactions", "/api/v1/dashboard?start=2026-10-01&end=2026-10-20", "/api/v1/auth/me"]:
            response = await self.client.get(endpoint)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_totals_scope_currency_review_and_ist(self):
        await self.seed()
        app.dependency_overrides[current_user] = lambda: self.owner
        response = await self.client.get("/api/v1/dashboard?start=2026-10-20&end=2026-10-20")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(float(data["expense"]), 250)
        self.assertEqual(float(data["income"]), 1000)
        self.assertEqual(float(data["net"]), 750)
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["review_count"], 1)
        self.assertEqual(float(data["trend"][0]["expense"]), 250)
        self.assertEqual(data["currencies"], ["INR", "USD"])
        self.assertNotIn("raw_message", data["transactions"][0])
        old = await self.client.get("/api/v1/transactions")
        self.assertEqual(len(old.json()), 4)

    async def test_filters_do_not_change_overall_totals(self):
        await self.seed(); app.dependency_overrides[current_user] = lambda: self.owner
        response = await self.client.get("/api/v1/dashboard?start=2026-10-01&end=2026-10-20&search=Merchant%201&kind=CREDIT")
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["transactions"][0]["merchant"], "Merchant 1")
        self.assertEqual(float(data["expense"]), 250)

    async def test_invalid_date_range(self):
        app.dependency_overrides[current_user] = lambda: self.owner
        response = await self.client.get("/api/v1/dashboard?start=2026-10-20&end=2026-10-01")
        self.assertEqual(response.status_code, 422)

    async def test_webhook_auth_validation_and_duplicate(self):
        payload = {"message": "INR 1234.56 debited from a/c XX1234 at ZOMATO", "received_at": "2026-10-20T10:00:00+05:30"}
        response = await self.client.post("/api/v1/webhook/sms", json=payload)
        self.assertEqual(response.status_code, 401)
        headers = {"X-Webhook-Secret": "test-webhook-secret"}
        response = await self.client.post("/api/v1/webhook/sms", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["transaction"]["amount"], 1234.56)
        second = await self.client.post("/api/v1/webhook/sms", json=payload, headers=headers)
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(second.json()["transaction"]["id"], response.json()["transaction"]["id"])
        bad = await self.client.post("/api/v1/webhook/sms", json={"message": "x" * 10001}, headers=headers)
        self.assertEqual(bad.status_code, 422)
        naive = await self.client.post("/api/v1/webhook/sms", json={**payload, "received_at": "2026-10-20T10:00:00"}, headers=headers)
        self.assertEqual(naive.status_code, 422)

    async def test_empty_and_otp_do_not_write_transactions(self):
        for message in ["", "OTP 123456 for INR 100 purchase"]:
            response = await self.client.post("/api/v1/webhook/sms", json={"message": message}, headers={"Authorization": "Bearer test-webhook-secret"})
            self.assertEqual(response.json()["status"], "ignored")
        async with AsyncSessionLocal() as db:
            self.assertIsNone(await db.scalar(select(Transaction)))

    async def test_logout_rejects_foreign_origin(self):
        response = await self.client.post("/api/v1/auth/logout", headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        response = await self.client.post("/api/v1/auth/logout", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(response.status_code, 200)

    async def test_signed_session_and_google_account_restriction(self):
        profile = {"sub": "google-owner", "email": "owner@example.com", "email_verified": True, "name": "Owner"}
        with patch("auth.configured", return_value=True), patch("auth.oauth.google.authorize_access_token", new_callable=AsyncMock) as token:
            token.return_value = {"userinfo": profile}
            response = await self.client.get("/api/v1/auth/google/callback")
            self.assertEqual(response.status_code, 303)
            self.assertIn("httponly", response.headers["set-cookie"].lower())
            me = await self.client.get("/api/v1/auth/me")
            self.assertEqual(me.json()["email"], "owner@example.com")
            token.return_value = {"userinfo": {**profile, "email": "stranger@example.com"}}
            denied = await self.client.get("/api/v1/auth/google/callback")
            self.assertIn("error=account", denied.headers["location"])
            self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
            token.side_effect = OAuthError("invalid_state")
            invalid = await self.client.get("/api/v1/auth/google/callback")
            self.assertIn("error=signin", invalid.headers["location"])

    async def test_duplicate_insert_race_returns_existing_record(self):
        async with AsyncSessionLocal() as db:
            row = Transaction(type="DEBIT", amount=250, currency="INR", raw_message="fixture", transaction_time=datetime.now(timezone.utc))
            db.add(row); await db.commit(); await db.refresh(row)
            fake_db = AsyncMock()
            from unittest.mock import MagicMock
            empty_result = MagicMock()
            empty_result.scalars.return_value.first.return_value = None
            fake_db.execute.return_value = empty_result
            fake_db.add = MagicMock()
            fake_db.commit.side_effect = IntegrityError("insert", {}, Exception("unique constraint"))
            fake_db.scalar.return_value = row
            async def override_db():
                yield fake_db
            app.dependency_overrides[get_db] = override_db
            response = await self.client.post("/api/v1/webhook/sms", json={"message": "INR 250 debited at ZOMATO"}, headers={"X-Webhook-Secret": "test-webhook-secret"})
            self.assertEqual(response.json()["status"], "duplicate")
            fake_db.rollback.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
