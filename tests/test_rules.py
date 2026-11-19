"""Merged names and category rules (rules.py): matching, the API, the save-time hooks and apply-to-past.
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
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import merchants
import rules
from auth import current_user
from database import AsyncSessionLocal, Base, engine, get_db
from main import app
from models import (CategoryLimit, CategoryRule, MerchantAlias, MerchantCategory, MerchantLimit, Notification,
                    Transaction, User)
from parser import ParsedTransaction, RuleOutcome, parse_sms

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
GEMINI_ON = {"GEMINI_API_KEY": "test-key"}
_refs = itertools.count(777000000001)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)  # Wed 21 Oct, 12:00 IST (limit tests)


def upi_debit(amount, merchant):
    return (f"Sent Rs.{amount}\nFrom HDFC Bank A/C *3304\nTo {merchant}\nOn 22/10/26\nRef {next(_refs)}\n"
            "Not You?\nCall 18002586161/SMS BLOCK UPI to 7308080808")


def upi_credit(amount, handle):
    return f"Credit Alert!\nRs.{amount} credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA {handle}@okaxis (UPI {next(_refs)})"


def card_sms(amount, merchant, extra=""):
    """An SMS the regex parser can't read, so the Gemini full parse (faked) decides."""
    return f"Txn of {amount} done at {merchant} {extra} ref {next(_refs)}"


def gemini_says(merchant="SWIGGY", category="Shopping", amount="250.00", kind="DEBIT"):
    return ParsedTransaction(type=kind, amount=Decimal(amount), currency="INR", merchant=merchant, category=category,
                             parsed_by="ai_agent", confidence=0.9, transaction_time=datetime.now(timezone.utc))


def view(merchant_words=None, sms_words=None, min_amount=None, direction=None, category="Shopping", created_at=T0,
         id=None):
    return rules.RuleView(id or uuid.uuid4(), merchant_words, sms_words,
                          None if min_amount is None else Decimal(str(min_amount)), direction, category, created_at)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


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

    async def sms(self, message, received_at=None):
        payload = {"message": message}
        if received_at is not None:
            payload["received_at"] = received_at.isoformat()
        response = await self.client.post("/api/v1/webhook/sms", json=payload, headers=HOOK)
        body = response.json()
        self.assertEqual(body["status"], "success", response.text)
        return body["transaction"]

    async def seed(self, merchant, category="Uncategorized", kind="DEBIT", user_id=None, parsed_by="regex", amount=50,
                   raw_message="fixture", currency="INR", transaction_time=None, updated_at=None, is_parsed=True):
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=kind, amount=Decimal(str(amount)), currency=currency, merchant=merchant,
                              category=category, user_id=user_id, raw_message=raw_message,
                              message_hash=uuid.uuid4().hex, is_parsed=is_parsed, parsed_by=parsed_by,
                              transaction_time=transaction_time or datetime.now(timezone.utc) - timedelta(days=1),
                              updated_at=updated_at or datetime.now(timezone.utc) - timedelta(hours=1))
            db.add(txn)
            await db.commit()
            return txn.id

    async def row(self, transaction_id):
        async with AsyncSessionLocal() as db:
            return await db.get(Transaction, transaction_id if isinstance(transaction_id, uuid.UUID) else uuid.UUID(transaction_id))

    async def edit(self, transaction_id, **fields):
        return await self.client.patch(f"/api/v1/transactions/{transaction_id}", json=fields, headers=ORIGIN)

    async def add_rule(self, apply_to_past=False, expect=201, **fields):
        response = await self.client.post("/api/v1/rules", json={**fields, "apply_to_past": apply_to_past}, headers=ORIGIN)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    async def merge(self, from_name, to_name, expect=201):
        response = await self.client.post("/api/v1/merchant-names", json={"from_name": from_name, "to_name": to_name},
                                          headers=ORIGIN)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    async def preview(self, **fields):
        response = await self.client.post("/api/v1/rules/preview", json=fields, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def manual(self, merchant, amount, category=None, kind="DEBIT"):
        response = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": kind, "amount": str(amount), "merchant": merchant, "category": category,
            "transaction_time": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def remember(self, key, category, kind="DEBIT", user_id=None):
        async with AsyncSessionLocal() as db:
            db.add(MerchantCategory(user_id=user_id or self.owner.id, merchant_key=key, type=kind, category=category))
            await db.commit()

    async def memory(self):
        async with AsyncSessionLocal() as db:
            return {(row.user_id, row.merchant_key, row.type): row.category
                    for row in await db.scalars(select(MerchantCategory))}

    def assert_message(self, response, message):
        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()["detail"]
        if isinstance(detail, str):
            self.assertEqual(detail, message)
        else:
            self.assertIn(f"Value error, {message}", [item["msg"] for item in detail], response.text)


# ---------- Phase 1: matching ----------

class MatchesAnyCaseTests(unittest.TestCase):
    def test_whole_words_in_order_any_case(self):
        self.assertTrue(merchants.matches_any_case("swiggy", "SWIGGY"))
        self.assertTrue(merchants.matches_any_case("swiggy", "Swiggy Ltd"))
        self.assertFalse(merchants.matches_any_case("swiggy ltd", "Swiggy Instamart"))
        self.assertFalse(merchants.matches_any_case("ltd swiggy", "Swiggy Ltd"))
        self.assertFalse(merchants.matches_any_case("swiggy", "Swiggyone"))

    def test_punctuation_splits_words(self):
        self.assertTrue(merchants.matches_any_case("rahul@okaxis", "Rs.18000 debited to VPA rahul@okaxis on 21-10-26"))
        self.assertTrue(merchants.matches_any_case("XX4821", "credited to HDFC Bank A/c XX4821 on 21-10-26"))

    def test_phrases_without_words_match_nothing(self):
        self.assertFalse(merchants.matches_any_case("@@", "@@ anything"))
        self.assertFalse(merchants.matches_any_case("Unknown", "Unknown"))
        self.assertFalse(merchants.matches_any_case("swiggy", None))

    def test_limits_stay_case_sensitive(self):
        self.assertFalse(merchants.matches("Swiggy", "SWIGGY"))
        self.assertTrue(merchants.matches("Swiggy", "Swiggy Ltd"))


class RuleMatchingTests(unittest.TestCase):
    def test_amount_is_inclusive_and_inr_only(self):
        rule = view(min_amount=15000)
        self.assertTrue(rules.rule_matches(rule, "Rahul", "sms", Decimal("15000"), "INR", "DEBIT"))
        self.assertFalse(rules.rule_matches(rule, "Rahul", "sms", Decimal("14999.99"), "INR", "DEBIT"))
        self.assertFalse(rules.rule_matches(rule, "Rahul", "sms", Decimal("20000"), "USD", "DEBIT"))

    def test_direction(self):
        rule = view(merchant_words="rahul", direction="CREDIT")
        self.assertTrue(rules.rule_matches(rule, "Rahul", None, 10, "INR", "CREDIT"))
        self.assertFalse(rules.rule_matches(rule, "Rahul", None, 10, "INR", "DEBIT"))
        either = view(merchant_words="rahul")
        self.assertTrue(rules.rule_matches(either, "Rahul", None, 10, "INR", "DEBIT"))

    def test_sms_words_never_match_without_an_sms(self):
        rule = view(sms_words="Added by hand")
        self.assertFalse(rules.rule_matches(rule, "Chai", None, 10, "INR", "DEBIT"))
        self.assertTrue(rules.rule_matches(rule, "Chai", "added by hand in Folio", 10, "INR", "DEBIT"))

    def test_every_condition_must_hold(self):
        rule = view(merchant_words="rahul", sms_words="okaxis", min_amount=100, direction="DEBIT")
        self.assertTrue(rules.rule_matches(rule, "Rahul", "to rahul@okaxis", 100, "INR", "DEBIT"))
        self.assertFalse(rules.rule_matches(rule, "Ravi", "to rahul@okaxis", 100, "INR", "DEBIT"))
        self.assertFalse(rules.rule_matches(rule, "Rahul", "to rahul@ybl", 100, "INR", "DEBIT"))

    def test_a_rule_without_conditions_matches_nothing(self):
        self.assertFalse(rules.rule_matches(view(direction="DEBIT"), "Rahul", "sms", 10, "INR", "DEBIT"))
        self.assertFalse(rules.rule_matches(view(), "Rahul", "sms", 10, "INR", "DEBIT"))

    def test_the_newest_match_wins(self):
        broad = view(merchant_words="swiggy", category="Food & Dining", created_at=T0)
        narrow = view(merchant_words="swiggy instamart", category="Groceries", created_at=T0 + timedelta(days=1))
        ordered = rules.order([broad, narrow])
        self.assertEqual(ordered, [narrow, broad])
        self.assertIs(rules.first_match(ordered, "Swiggy Instamart", None, 10, "INR", "DEBIT"), narrow)
        self.assertIs(rules.first_match(ordered, "Swiggy", None, 10, "INR", "DEBIT"), broad)
        self.assertIsNone(rules.first_match(ordered, "Zomato", None, 10, "INR", "DEBIT"))

    def test_equal_times_fall_back_to_id(self):
        low = view(merchant_words="swiggy", id=uuid.UUID(int=1))
        high = view(merchant_words="swiggy", id=uuid.UUID(int=2))
        self.assertEqual(rules.order([low, high]), [high, low])
        self.assertEqual(rules.order([high, low]), [high, low])

    def test_with_rule(self):
        a, b = view(merchant_words="a"), view(merchant_words="b")
        new = view(merchant_words="c")
        self.assertEqual(rules.with_rule([a, b], new), [new, a, b])
        edited = view(merchant_words="d", id=b.id)
        self.assertEqual(rules.with_rule([a, b], edited, replace_id=b.id), [a, edited])
        with self.assertRaises(LookupError):
            rules.with_rule([a], edited, replace_id=b.id)

    def test_valid_category(self):
        self.assertEqual(rules.valid_category("  self transfer "), "Self Transfer")
        self.assertEqual(rules.valid_category("BILLS & UTILITIES"), "Bills & Utilities")
        self.assertIsNone(rules.valid_category("Uncategorized"))
        self.assertIsNone(rules.valid_category("Rent"))
        self.assertIsNone(rules.valid_category(None))


# ---------- Phase 1: API ----------

class TablesTests(DbTestCase):
    async def test_tables_exist_and_a_name_merges_once(self):
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        self.assertIn("merchant_aliases", tables)
        self.assertIn("category_rules", tables)
        async with AsyncSessionLocal() as db:
            for to_name in ("Swiggy", "Swiggy Food"):
                db.add(MerchantAlias(user_id=self.owner.id, from_name="SWIGGY", from_key="swiggy",
                                     from_hash=merchants.name_hash("swiggy"), to_name=to_name))
            with self.assertRaises(IntegrityError):
                await db.commit()


class RulesApiTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sign_in()

    async def test_create_list_update_delete(self):
        saved = await self.add_rule(sms_words="  rahul@okaxis  ", min_amount="15000", direction="DEBIT",
                                    category="bills & utilities")
        rule = saved["rule"]
        self.assertEqual(saved["changed"], 0)
        self.assertEqual((rule["merchant_words"], rule["sms_words"], rule["min_amount"], rule["direction"], rule["category"]),
                         (None, "rahul@okaxis", "15000.00", "DEBIT", "Bills & Utilities"))
        newer = (await self.add_rule(merchant_words="Swiggy  Instamart", category="Groceries"))["rule"]
        listed = (await self.client.get("/api/v1/rules")).json()
        self.assertEqual([r["id"] for r in listed], [newer["id"], rule["id"]])
        self.assertEqual(listed[0]["merchant_words"], "Swiggy Instamart")

        response = await self.client.put(f"/api/v1/rules/{rule['id']}", headers=ORIGIN,
                                          json={"merchant_words": "Rahul", "sms_words": None, "category": "Transfer"})
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()["rule"]
        self.assertEqual((updated["merchant_words"], updated["sms_words"], updated["min_amount"], updated["direction"],
                          updated["category"]), ("Rahul", None, None, None, "Transfer"))
        self.assertEqual(updated["created_at"], rule["created_at"])
        listed = (await self.client.get("/api/v1/rules")).json()
        self.assertEqual([r["id"] for r in listed], [newer["id"], rule["id"]])  # kept its place

        response = await self.client.delete(f"/api/v1/rules/{rule['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204)
        response = await self.client.delete(f"/api/v1/rules/{rule['id']}", headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Rule not found."))
        response = await self.client.put(f"/api/v1/rules/{rule['id']}", headers=ORIGIN,
                                          json={"merchant_words": "Rahul", "category": "Transfer"})
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Rule not found."))

    async def test_validation_messages(self):
        cases = [
            ({"category": "Shopping"}, "Add at least one condition."),
            ({"merchant_words": "   ", "sms_words": "", "category": "Shopping"}, "Add at least one condition."),
            ({"direction": "DEBIT", "category": "Shopping"}, "Add at least one condition."),
            ({"merchant_words": "@@", "category": "Shopping"}, "Enter words that appear in the name."),
            ({"merchant_words": "Unknown", "category": "Shopping"}, "Enter words that appear in the name."),
            ({"sms_words": "--", "category": "Shopping"}, "Enter words that appear in the SMS."),
            ({"merchant_words": "Swi\x00ggy", "category": "Shopping"}, "Words can't contain invalid characters."),
            ({"sms_words": "XX\ud8004821", "category": "Shopping"}, "Words can't contain invalid characters."),
            ({"merchant_words": "a" * 256, "category": "Shopping"}, "Use up to 255 characters."),
            ({"sms_words": "b" * 256, "category": "Shopping"}, "Use up to 255 characters."),
            ({"min_amount": "0", "category": "Shopping"}, "Enter an amount above zero."),
            ({"min_amount": "-1", "category": "Shopping"}, "Enter an amount above zero."),
            ({"min_amount": "1234567890123", "category": "Shopping"}, "Enter an amount above zero."),
            ({"min_amount": "10000000000", "category": "Shopping"}, "Enter an amount above zero."),
            ({"min_amount": "100.505", "category": "Shopping"}, "Enter an amount above zero."),
            ({"min_amount": "abc", "category": "Shopping"}, "Enter an amount above zero."),
            ({"min_amount": "NaN", "category": "Shopping"}, "Enter an amount above zero."),
            ({"merchant_words": "Swiggy", "category": "Uncategorized"}, "Pick a category from the list."),
            ({"merchant_words": "Swiggy", "category": "Rent"}, "Pick a category from the list."),
        ]
        headers = {**ORIGIN, "Content-Type": "application/json"}
        for body, message in cases:
            with self.subTest(body=body):
                raw = json.dumps(body)  # ASCII escapes, so a lone surrogate can be sent
                response = await self.client.post("/api/v1/rules", content=raw, headers=headers)
                self.assert_message(response, message)
                response = await self.client.post("/api/v1/rules/preview", content=raw, headers=headers)
                self.assert_message(response, message)
        self.assertEqual((await self.client.get("/api/v1/rules")).json(), [])
        # 255 characters, an amount at the top of the range and two decimals are fine.
        saved = await self.add_rule(merchant_words="a" * 255, min_amount="9999999999.99", category="Shopping")
        self.assertEqual(saved["rule"]["min_amount"], "9999999999.99")
        saved = await self.add_rule(min_amount=100.5, category="Shopping")
        self.assertEqual(saved["rule"]["min_amount"], "100.50")

    async def test_session_origin_and_ownership(self):
        rule = (await self.add_rule(merchant_words="Swiggy", category="Shopping"))["rule"]
        body = {"merchant_words": "Swiggy", "category": "Shopping"}
        evil = {"Origin": "https://evil.example"}
        for response in (await self.client.post("/api/v1/rules", json=body, headers=evil),
                         await self.client.post("/api/v1/rules/preview", json=body, headers=evil),
                         await self.client.put(f"/api/v1/rules/{rule['id']}", json=body, headers=evil),
                         await self.client.delete(f"/api/v1/rules/{rule['id']}", headers=evil)):
            self.assertEqual((response.status_code, response.json()["detail"]), (403, "Invalid request origin."))

        self.sign_in(self.other)
        self.assertEqual((await self.client.get("/api/v1/rules")).json(), [])
        for response in (await self.client.put(f"/api/v1/rules/{rule['id']}", json=body, headers=ORIGIN),
                         await self.client.delete(f"/api/v1/rules/{rule['id']}", headers=ORIGIN),
                         await self.client.post("/api/v1/rules/preview", json={**body, "id": rule["id"]}, headers=ORIGIN)):
            self.assertEqual((response.status_code, response.json()["detail"]), (404, "Rule not found."))

        app.dependency_overrides.clear()
        for response in (await self.client.get("/api/v1/rules"),
                         await self.client.post("/api/v1/rules", json=body, headers=ORIGIN),
                         await self.client.get("/api/v1/merchant-names"),
                         await self.client.post("/api/v1/merchant-names", json={"from_name": "A", "to_name": "B"},
                                                headers=ORIGIN)):
            self.assertEqual(response.status_code, 401)
            self.assertTrue(response.json()["detail"].startswith("Please sign in"))
        async with AsyncSessionLocal() as db:
            self.assertEqual(len((await db.scalars(select(CategoryRule))).all()), 1)


class NamesApiTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sign_in()

    async def test_a_payment_without_a_name_cant_be_merged(self):
        for from_name in ("Unknown", "", "   ", " unknown "):
            with self.subTest(from_name=from_name):
                response = await self.client.post("/api/v1/merchant-names", headers=ORIGIN,
                                                  json={"from_name": from_name, "to_name": "Swiggy"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "This payment has no name to rename.")

    async def test_to_name_checks(self):
        cases = [("a" * 101, "Merchant names can be up to 100 characters."),
                 ("Swi\x00ggy", "Merchant names can't contain invalid characters."),
                 ("@@", "Enter a merchant name."),
                 ("   ", "Enter a merchant name."),
                 ("Unknown", "Enter a merchant name.")]
        for to_name, message in cases:
            with self.subTest(to_name=to_name):
                response = await self.client.post("/api/v1/merchant-names", headers=ORIGIN,
                                                  json={"from_name": "SWIGGY", "to_name": to_name})
                self.assert_message(response, message)
        saved = await self.merge("SWIGGY", "  " + "a" * 98 + "   b ")
        self.assertEqual(saved["name"]["to_name"], "a" * 98 + " b")  # 104 typed, 100 once cleaned

    async def test_list_delete_and_ownership(self):
        first = (await self.merge("SWIGGY", "Swiggy"))["name"]
        second = (await self.merge("  Amazonpayindiap ", "Amazon"))["name"]
        self.assertEqual(second["from_name"], "Amazonpayindiap")
        listed = (await self.client.get("/api/v1/merchant-names")).json()
        self.assertEqual([n["id"] for n in listed], [second["id"], first["id"]])
        self.assertEqual(set(listed[0]), {"id", "from_name", "to_name", "created_at"})

        evil = await self.client.delete(f"/api/v1/merchant-names/{first['id']}", headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status_code, 403)
        self.sign_in(self.other)
        self.assertEqual((await self.client.get("/api/v1/merchant-names")).json(), [])
        response = await self.client.delete(f"/api/v1/merchant-names/{first['id']}", headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Merged name not found."))

        self.sign_in()
        response = await self.client.delete(f"/api/v1/merchant-names/{first['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204)
        response = await self.client.delete(f"/api/v1/merchant-names/{first['id']}", headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Merged name not found."))
        self.assertEqual([n["id"] for n in (await self.client.get("/api/v1/merchant-names")).json()], [second["id"]])

    async def test_merging_the_same_name_again_replaces_it(self):
        await self.merge("SWIGGY", "Swiggy")
        saved = await self.merge("swiggy", "Swiggy Food")
        listed = (await self.client.get("/api/v1/merchant-names")).json()
        self.assertEqual(len(listed), 1)
        self.assertEqual((listed[0]["from_name"], listed[0]["to_name"]), ("swiggy", "Swiggy Food"))
        self.assertEqual(saved["name"]["id"], listed[0]["id"])

    async def test_a_same_name_race_is_retried(self):
        # Signed in as auth.current_user does it: loaded through the route's own session, so the rollback
        # before the retry expires the user.
        owner_id = self.owner.id

        async def session_user(db: AsyncSession = Depends(get_db)):
            return await db.get(User, owner_id)

        app.dependency_overrides[current_user] = session_user
        real, calls = rules.merge_names, []

        async def racing(db, user, from_name, to_name):
            saved = await real(db, user, from_name, to_name)
            calls.append(to_name)
            if len(calls) == 1:  # another save of the same name commits between this one's read and commit
                async with AsyncSessionLocal() as other:
                    other.add(MerchantAlias(user_id=owner_id, from_name="SWIGGY", from_key="swiggy",
                                            from_hash=merchants.name_hash("swiggy"), to_name="Swiggy Other"))
                    await other.commit()
            return saved

        with patch.object(rules, "merge_names", racing):
            saved = await self.merge("swiggy", "Swiggy")
        self.assertEqual(calls, ["Swiggy", "Swiggy"])  # the commit failed once and the retry saved it
        listed = (await self.client.get("/api/v1/merchant-names")).json()
        self.assertEqual([(n["id"], n["from_name"], n["to_name"]) for n in listed],
                         [(saved["name"]["id"], "swiggy", "Swiggy")])  # the winner's merge, updated



# ---------- Phase 2: save-time hooks ----------

class SmsTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sign_in()

    async def test_an_sms_for_a_merged_name_is_saved_under_it(self):
        await self.merge("Qwikly Hub", "Qwikly")
        txn = await self.sms(upi_debit("41.00", "QWIKLY  HUB"))
        self.assertEqual((txn["merchant"], txn["parsed_by"]), ("Qwikly", "regex"))

    async def test_keywords_run_again_on_the_merged_name(self):
        await self.merge("Amazonpayindiap", "Amazon")
        txn = await self.sms(upi_debit("42.00", "Amazonpayindiap"))
        self.assertEqual((txn["merchant"], txn["category"], txn["parsed_by"]), ("Amazon", "Shopping", "regex"))
        # A keyword category the bank's name already had stays.
        await self.merge("Swiggy", "Swiggy Food Court")
        txn = await self.sms(upi_debit("43.00", "Swiggy"))
        self.assertEqual((txn["merchant"], txn["category"]), ("Swiggy Food Court", "Food & Dining"))

    async def test_a_rule_beats_memory_and_keywords_without_gemini(self):
        await self.remember("swiggy", "Groceries")
        await self.add_rule(merchant_words="swiggy", category="Entertainment")
        await self.add_rule(sms_words="XX4821", direction="CREDIT", category="Self Transfer")
        with patch.dict(os.environ, GEMINI_ON), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, side_effect=AssertionError("no AI")) as ai:
            debit = await self.sms(upi_debit("44.00", "Swiggy"))
            credit = await self.sms(upi_credit("5000.00", "qwikly.hub"))
        ai.assert_not_called()
        self.assertEqual((debit["category"], debit["parsed_by"]), ("Entertainment", "regex+rule"))
        self.assertEqual((credit["category"], credit["parsed_by"]), ("Self Transfer", "regex+rule"))
        txn = await self.sms(upi_debit("45.00", "Zomato"))  # no rule matches: keywords as before
        self.assertEqual((txn["category"], txn["parsed_by"]), ("Food & Dining", "regex"))

    async def test_rules_run_on_the_merged_name(self):
        await self.merge("Amazonpayindiap", "Amazon")
        await self.add_rule(merchant_words="amazon", min_amount="1000", category="Groceries")
        big = await self.sms(upi_debit("1500.00", "Amazonpayindiap"))
        small = await self.sms(upi_debit("46.00", "Amazonpayindiap"))
        self.assertEqual((big["merchant"], big["category"], big["parsed_by"]), ("Amazon", "Groceries", "regex+rule"))
        self.assertEqual((small["merchant"], small["category"], small["parsed_by"]), ("Amazon", "Shopping", "regex"))

    async def test_rent_by_amount_and_payee(self):
        await self.add_rule(sms_words="rahul@okaxis", min_amount="15000", direction="DEBIT", category="Bills & Utilities")
        sms = "Rs.{} debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA rahul@okaxis (UPI Ref No {}). Not you? Call 18002586161"
        rent = await self.sms(sms.format("18000.00", next(_refs)))
        small = await self.sms(sms.format("300.00", next(_refs)))
        self.assertEqual((rent["merchant"], rent["category"], rent["parsed_by"]), ("Rahul", "Bills & Utilities", "regex+rule"))
        # Not matched, and the rule's category doesn't reach it through earlier payments either.
        self.assertEqual((small["merchant"], small["category"], small["parsed_by"]), ("Rahul", "Uncategorized", "regex"))

    async def test_picking_another_category_by_hand_ends_set_by_rule(self):
        await self.add_rule(merchant_words="swiggy", category="Entertainment")
        txn = await self.sms(upi_debit("47.00", "Swiggy"))
        self.assertEqual(txn["parsed_by"], "regex+rule")
        # Editing anything else, or sending the same category, leaves the rule's marker.
        response = await self.edit(txn["id"], notes="team lunch", category="Entertainment")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["category"], response.json()["parsed_by"]), ("Entertainment", "regex+rule"))
        # Picking another category: the sheet no longer says a rule set it ("Rules + your category").
        response = await self.edit(txn["id"], category="Groceries")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["category"], response.json()["parsed_by"]), ("Groceries", "regex+user"))
        self.assertEqual((await self.row(txn["id"])).parsed_by, "regex+user")
        # A hand-added payment keeps "manual" when its category changes.
        manual = await self.manual("Swiggy", 48)
        response = await self.edit(manual["id"], category="Shopping")
        self.assertEqual((response.json()["category"], response.json()["parsed_by"]), ("Shopping", "manual"))

    async def match(self, transaction_id, expect=200):
        response = await self.client.get(f"/api/v1/rules/match/{transaction_id}")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    async def test_the_rule_that_decides_a_payment_still_matches_after_a_hand_pick(self):
        rule = await self.add_rule(merchant_words="swiggy", category="Entertainment")
        await self.add_rule(sms_words="XX9999", category="Shopping")  # newer, but matches no SMS here
        txn = await self.sms(upi_debit("47.00", "Swiggy"))
        self.assertEqual((await self.match(txn["id"]))["rule"], rule["rule"])
        # Picked by hand twice: parsed_by is "regex+user", but the rule still decides the next SMS, so the edit
        # sheet keeps saying "Future payments like this follow your category rule".
        for category in ("Groceries", "Shopping"):
            response = await self.edit(txn["id"], category=category)
            self.assertEqual((response.json()["category"], response.json()["parsed_by"]), (category, "regex+user"))
            self.assertEqual((await self.match(txn["id"]))["rule"]["id"], rule["rule"]["id"])
        later = await self.sms(upi_debit("48.00", "Swiggy"))
        self.assertEqual((later["category"], later["parsed_by"]), ("Entertainment", "regex+rule"))
        # No rule matches: null. SMS words never match a payment added by hand.
        other = await self.sms(upi_debit("49.00", "Zomato"))
        self.assertEqual(await self.match(other["id"]), {"rule": None})
        hand = await self.manual("Zomato", 50)
        await self.add_rule(sms_words="zomato", category="Shopping")
        self.assertEqual((await self.match(other["id"]))["rule"]["category"], "Shopping")
        self.assertEqual(await self.match(hand["id"]), {"rule": None})
        # Only your own rules are tried; someone else's payment, or none, is a 404.
        self.sign_in(self.other)
        self.assertEqual(await self.match(txn["id"]), {"rule": None})
        response = await self.client.get(f"/api/v1/rules/match/{hand['id']}")
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Transaction not found."))
        self.sign_in()
        response = await self.client.get(f"/api/v1/rules/match/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404, response.text)

    async def test_only_sms_rows_the_regex_parser_read_keep_a_rule_out_of_history(self):
        # Documented limit (README Categories step 3): a rule's category on a hand-added or Gemini-read row has
        # no marker (parsed_by stays "manual" / "ai_agent"), so it reaches later payments like any category.
        sms = "Rs.{} debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA {} (UPI Ref No {}). Not you? Call 18002586161"
        await self.add_rule(merchant_words="rahul", min_amount="15000", direction="DEBIT", category="Bills & Utilities")
        rent = await self.manual("Rahul", 18000)
        self.assertEqual((rent["category"], rent["parsed_by"]), ("Bills & Utilities", "manual"))
        small = await self.sms(sms.format("300.00", "rahul@okaxis", next(_refs)))
        self.assertEqual((small["merchant"], small["category"], small["parsed_by"]),
                         ("Rahul", "Bills & Utilities", "regex+history"))

        await self.add_rule(sms_words="rent", min_amount="15000", category="Bills & Utilities")
        with patch("parser.parse_sms_with_regex", return_value=None), \
                patch("parser.parse_sms_with_gemini", new_callable=AsyncMock,
                      return_value=gemini_says(merchant="Kiran", amount="18000.00")):
            rent = await self.sms(card_sms("18000.00", "Kiran", "rent"))
        self.assertEqual((rent["merchant"], rent["category"], rent["parsed_by"]), ("Kiran", "Bills & Utilities", "ai_agent"))
        small = await self.sms(sms.format("300.00", "kiran@okaxis", next(_refs)))
        self.assertEqual((small["merchant"], small["category"], small["parsed_by"]),
                         ("Kiran", "Bills & Utilities", "regex+history"))

    async def test_full_parse_gets_the_merged_name_and_a_rule(self):
        await self.merge("SWIGGY", "Swiggy")
        await self.add_rule(sms_words="XX9999", category="Self Transfer")
        await self.remember("swiggy", "Groceries")
        with patch("parser.parse_sms_with_regex", return_value=None), \
                patch("parser.parse_sms_with_gemini", new_callable=AsyncMock, return_value=gemini_says()):
            ruled = await self.sms(card_sms("250.00", "SWIGGY", "card XX9999"))
            remembered = await self.sms(card_sms("250.00", "SWIGGY"))
        self.assertEqual((ruled["merchant"], ruled["category"], ruled["parsed_by"]), ("Swiggy", "Self Transfer", "ai_agent"))
        self.assertEqual((remembered["merchant"], remembered["category"], remembered["parsed_by"]),
                         ("Swiggy", "Groceries", "ai_agent"))

    async def test_failing_rules_never_lose_an_sms(self):
        await self.add_rule(merchant_words="swiggy", category="Entertainment")
        with patch("rules.load_owner_rules", new_callable=AsyncMock, side_effect=RuntimeError("database hiccup")):
            txn = await self.sms(upi_debit("47.00", "Swiggy"))
        self.assertEqual((txn["category"], txn["parsed_by"]), ("Food & Dining", "regex"))
        later = await self.sms(upi_debit("48.00", "Swiggy"))
        self.assertEqual((later["category"], later["parsed_by"]), ("Entertainment", "regex+rule"))

    async def test_without_an_owner_nothing_applies(self):
        await self.merge("Swiggy", "Swiggy Food")
        await self.add_rule(merchant_words="swiggy", category="Entertainment")
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.email == "owner@example.com"))
            await db.commit()
        txn = await self.sms(upi_debit("49.00", "Swiggy"))
        self.assertEqual((txn["merchant"], txn["category"], txn["parsed_by"]), ("Swiggy", "Food & Dining", "regex"))

    async def test_another_users_merges_and_rules_never_apply(self):
        self.sign_in(self.other)
        await self.merge("Swiggy", "Someone Else")
        await self.add_rule(merchant_words="swiggy", category="Entertainment")
        txn = await self.sms(upi_debit("51.00", "Swiggy"))
        self.assertEqual((txn["merchant"], txn["category"], txn["parsed_by"]), ("Swiggy", "Food & Dining", "regex"))


class ParserHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_merged_name_gets_its_keyword(self):
        calls = []

        async def apply_rules(*args):
            calls.append(args)
            return RuleOutcome("Amazon", None)

        sms = upi_debit("12.00", "Amazonpayindiap")
        parsed = await parse_sms(sms, apply_rules=apply_rules)
        self.assertEqual((parsed.merchant, parsed.category, parsed.parsed_by), ("Amazon", "Shopping", "regex"))
        self.assertEqual(calls, [("Amazonpayindiap", sms, Decimal("12.00"), "INR", "DEBIT")])

    async def test_a_failing_callback_keeps_the_regex_result(self):
        async def apply_rules(*args):
            raise RuntimeError("down")

        with self.assertLogs("parser", "WARNING") as logs:
            parsed = await parse_sms(upi_debit("13.00", "Amazonpayindiap"), apply_rules=apply_rules)
        self.assertEqual((parsed.merchant, parsed.category, parsed.parsed_by), ("Amazonpayindiap", "Uncategorized", "regex"))
        self.assertIn("Rules unavailable; saving without them", "\n".join(logs.output))

    async def test_unreadable_sms_never_run_rules(self):
        apply_rules = AsyncMock(return_value=RuleOutcome("Amazon", "Shopping"))
        with patch("parser.parse_sms_with_regex", return_value=None), \
                patch("parser.parse_sms_with_gemini", new_callable=AsyncMock, side_effect=RuntimeError("down")):
            parsed = await parse_sms(card_sms("14.00", "Somewhere"), apply_rules=apply_rules)
        self.assertEqual(parsed.parsed_by, "failed")
        apply_rules.assert_not_called()


class HandAddedTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sign_in()

    async def test_the_merged_name_is_always_used(self):
        await self.merge("SWIGGY", "Swiggy Food")
        auto = await self.manual("swiggy", 60)
        picked = await self.manual(" SWIGGY ", 61, category="Groceries")
        self.assertEqual((auto["merchant"], auto["category"], auto["parsed_by"]), ("Swiggy Food", "Food & Dining", "manual"))
        self.assertEqual((picked["merchant"], picked["category"], picked["parsed_by"]), ("Swiggy Food", "Groceries", "manual"))
        self.assertEqual((await self.memory())[(self.owner.id, "swiggy food", "DEBIT")], "Groceries")

    async def test_auto_uses_rules_and_a_picked_category_wins(self):
        await self.merge("Amazonpayindiap", "Amazon")
        await self.add_rule(merchant_words="chai point", min_amount="100", category="Entertainment")
        await self.add_rule(merchant_words="amazon", category="Groceries")
        ruled = await self.manual("Chai Point", 150)
        small = await self.manual("Chai Point", 50)
        picked = await self.manual("AMAZONPAYINDIAP", 70, category="Shopping")
        self.assertEqual((ruled["category"], ruled["parsed_by"]), ("Entertainment", "manual"))
        self.assertEqual((small["category"], small["parsed_by"]), ("Food & Dining", "manual"))
        self.assertEqual((picked["merchant"], picked["category"], picked["parsed_by"]), ("Amazon", "Shopping", "manual"))
        self.assertEqual(await self.memory(), {(self.owner.id, "amazon", "DEBIT"): "Shopping"})
        auto = await self.manual("Amazon", 71)  # the rule beats the memory just written
        self.assertEqual(auto["category"], "Groceries")

    async def test_sms_words_never_match_a_hand_added_payment(self):
        await self.add_rule(sms_words="Added by hand", category="Entertainment")
        await self.add_rule(sms_words="Qwikly", category="Entertainment")
        txn = await self.manual("Qwikly Hub", 80)
        self.assertEqual((txn["category"], txn["parsed_by"]), ("Uncategorized", "manual"))

    async def test_editing_runs_no_merges_or_rules(self):
        await self.merge("SWIGGY", "Swiggy Food")
        await self.add_rule(merchant_words="swiggy", category="Entertainment")
        txn = await self.manual("Zomato", 90)
        response = await self.edit(txn["id"], merchant="SWIGGY", category="Groceries")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["merchant"], response.json()["category"]), ("SWIGGY", "Groceries"))


# ---------- Phase 2: merges ----------

class MergeTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sign_in()

    async def names(self):
        async with AsyncSessionLocal() as db:
            return {row.id: row.merchant for row in await db.scalars(select(Transaction))}

    async def test_past_payments_with_the_name_are_renamed(self):
        edited = await self.seed("SWIGGY", amount=101)
        sms_row = await self.seed("SWIGGY ", amount=102)
        lower = await self.seed("swiggy", amount=103, user_id=self.owner.id)
        hand = await self.seed("Swiggy", amount=104, user_id=self.owner.id, parsed_by="manual",
                               raw_message="Added by hand in Folio")
        untouched = {await self.seed("Swiggy Instamart", amount=105): "Swiggy Instamart",
                     await self.seed("Swiggy Ltd", amount=106): "Swiggy Ltd",
                     await self.seed("SWIGGY", amount=107, user_id=self.other.id): "SWIGGY",
                     await self.seed("Swiggyone", amount=108): "Swiggyone",
                     await self.seed(None, amount=109): None}
        await self.remember("swiggy", "Groceries")
        saved = await self.merge("SWIGGY", "Swiggy")  # case only: the hand-added "Swiggy" already has it
        self.assertEqual(saved["changed"], 3)
        self.assertEqual((saved["name"]["from_name"], saved["name"]["to_name"]), ("SWIGGY", "Swiggy"))
        names = await self.names()
        for row_id in (edited, sms_row, lower, hand):
            self.assertEqual(names[row_id], "Swiggy")
        for row_id, name in untouched.items():
            self.assertEqual(names[row_id], name)
        self.assertEqual(await self.memory(), {(self.owner.id, "swiggy", "DEBIT"): "Groceries"})  # nothing moved
        row = await self.row(edited)
        self.assertGreater(_utc(row.updated_at), datetime.now(timezone.utc) - timedelta(minutes=1))

    async def test_the_next_sms_uses_a_case_only_merge(self):
        await self.merge("SWIGGY", "Swiggy")
        with patch("parser.parse_sms_with_regex", return_value=None), \
                patch("parser.parse_sms_with_gemini", new_callable=AsyncMock, return_value=gemini_says()):
            txn = await self.sms(card_sms("250.00", "SWIGGY"))
        self.assertEqual(txn["merchant"], "Swiggy")

    async def test_merges_never_chain(self):
        a = await self.seed("Swiggy Ltd", amount=111)
        await self.merge("Swiggy Ltd", "Swiggy Foods")
        b = await self.seed("Swiggy Foods", amount=112)
        saved = await self.merge("swiggy foods", "Swiggy")
        self.assertEqual(saved["changed"], 2)  # the renamed "Swiggy Ltd" row and the new one
        listed = {n["from_name"]: n["to_name"] for n in (await self.client.get("/api/v1/merchant-names")).json()}
        self.assertEqual(listed, {"Swiggy Ltd": "Swiggy", "swiggy foods": "Swiggy"})
        names = await self.names()
        self.assertEqual((names[a], names[b]), ("Swiggy", "Swiggy"))
        txn = await self.sms(upi_debit("113.00", "Swiggy Ltd"))
        self.assertEqual(txn["merchant"], "Swiggy")

    async def test_renaming_back_reverses_a_merge(self):
        alpha = await self.seed("Alpha Mart", amount=125)
        beta = await self.seed("Beta Mart", amount=126)
        await self.merge("Beta Mart", "Alpha Mart")
        self.assertEqual((await self.names())[beta], "Alpha Mart")
        saved = await self.merge("Alpha Mart", "Beta Mart")  # not a self-merge Alpha Mart -> Alpha Mart
        self.assertEqual((saved["name"]["from_name"], saved["name"]["to_name"], saved["changed"]),
                         ("Alpha Mart", "Beta Mart", 2))
        names = await self.names()
        self.assertEqual((names[alpha], names[beta]), ("Beta Mart", "Beta Mart"))
        listed = {n["from_name"]: n["to_name"] for n in (await self.client.get("/api/v1/merchant-names")).json()}
        self.assertEqual(listed, {"Alpha Mart": "Beta Mart", "Beta Mart": "Beta Mart"})  # none chains
        txn = await self.sms(upi_debit("127.00", "Alpha Mart"))
        self.assertEqual(txn["merchant"], "Beta Mart")

    async def test_merging_into_a_merged_name_ends_at_its_final_name(self):
        await self.merge("Swiggy Foods", "Swiggy")
        c = await self.seed("SWGY", amount=121)
        saved = await self.merge("SWGY", "swiggy foods")
        self.assertEqual((saved["name"]["to_name"], saved["changed"]), ("Swiggy", 1))
        self.assertEqual((await self.names())[c], "Swiggy")

    async def test_memory_moves_and_an_existing_entry_wins(self):
        await self.remember("amazonpayindiap", "Shopping", "DEBIT")
        await self.remember("amazonpayindiap", "Refunds & Cashback", "CREDIT")
        await self.remember("amazon", "Groceries", "DEBIT")
        await self.remember("amazonpayindiap", "Education", "DEBIT", user_id=self.other.id)
        await self.merge("Amazonpayindiap", "Amazon")
        self.assertEqual(await self.memory(), {
            (self.owner.id, "amazon", "DEBIT"): "Groceries",
            (self.owner.id, "amazon", "CREDIT"): "Refunds & Cashback",
            (self.other.id, "amazonpayindiap", "DEBIT"): "Education",
        })

    async def test_an_edit_made_meanwhile_wins(self):
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        fresh = await self.seed("SWIGGY", amount=131, updated_at=later)
        old = await self.seed("SWIGGY", amount=132)
        saved = await self.merge("SWIGGY", "Swiggy")
        self.assertEqual(saved["changed"], 1)
        names = await self.names()
        self.assertEqual((names[fresh], names[old]), ("SWIGGY", "Swiggy"))

    async def test_deleting_a_merge_keeps_the_names(self):
        row = await self.seed("SWIGGY", amount=141)
        saved = await self.merge("SWIGGY", "Swiggy")
        response = await self.client.delete(f"/api/v1/merchant-names/{saved['name']['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.names())[row], "Swiggy")
        with patch("parser.parse_sms_with_regex", return_value=None), \
                patch("parser.parse_sms_with_gemini", new_callable=AsyncMock, return_value=gemini_says()):
            txn = await self.sms(card_sms("250.00", "SWIGGY"))
        self.assertEqual(txn["merchant"], "SWIGGY")


# ---------- Phase 2: apply to past ----------

class ApplyTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sign_in()
        day = datetime.now(timezone.utc) - timedelta(days=30)
        sms = "Rs.{} credited to HDFC Bank A/c XX7777 from VPA {}@okaxis"
        self.regex = [await self.seed("Qwikly Hub", "Transfer", "CREDIT", amount=200 + i, raw_message=sms.format(200 + i, "q"),
                                      transaction_time=day + timedelta(days=i)) for i in range(6)]
        self.ai = await self.seed("Me", "Income", "CREDIT", parsed_by="ai_agent", amount=300,
                                  raw_message=sms.format(300, "me"), transaction_time=day + timedelta(days=10))
        self.rahul = await self.seed("Rahul", "Transfer", "CREDIT", amount=310, raw_message=sms.format(310, "rahul"),
                                     transaction_time=day + timedelta(days=11))
        self.untouched = [
            await self.seed("Me", "Transfer", "CREDIT", parsed_by="manual", amount=320, raw_message=sms.format(320, "x")),
            await self.seed("Me", "Transfer", "CREDIT", parsed_by="edited", amount=330, raw_message=sms.format(330, "x")),
            await self.seed("Me", "Self Transfer", "CREDIT", amount=340, raw_message=sms.format(340, "x")),
            await self.seed("Me", "Transfer", "DEBIT", amount=350, raw_message=sms.format(350, "x")),
            await self.seed("Me", "Transfer", "CREDIT", amount=360, raw_message="Rs.360 credited to A/c XX8888"),
            await self.seed("Me", "Transfer", "CREDIT", amount=0, raw_message=sms.format(0, "x"), is_parsed=False),
            await self.seed("Me", "Transfer", "CREDIT", amount=380, raw_message=sms.format(380, "x"), user_id=self.other.id),
        ]
        self.rule = {"sms_words": "xx7777", "direction": "CREDIT", "category": "Self Transfer"}

    async def snapshot(self):
        async with AsyncSessionLocal() as db:
            return {row.id: (row.category, row.parsed_by, row.updated_at) for row in await db.scalars(select(Transaction))}

    async def test_preview_matches_apply(self):
        before = await self.snapshot()
        preview = await self.preview(**self.rule)
        self.assertEqual(await self.snapshot(), before)  # writes nothing
        expected = {*self.regex, self.ai, self.rahul}
        self.assertEqual(preview["count"], len(expected))
        examples = preview["examples"]
        self.assertEqual([e["id"] for e in examples],
                         [str(i) for i in (self.rahul, self.ai, self.regex[5], self.regex[4], self.regex[3])])
        self.assertEqual((examples[0]["merchant"], examples[0]["amount"], examples[0]["currency"], examples[0]["category"]),
                         ("Rahul", "310.00", "INR", "Transfer"))
        self.assertTrue(examples[0]["transaction_time"].endswith(("Z", "+00:00")))

        saved = await self.add_rule(apply_to_past=True, **self.rule)
        self.assertEqual(saved["changed"], len(expected))
        after = await self.snapshot()
        changed = {row_id for row_id in after if after[row_id][:2] != before[row_id][:2]}
        self.assertEqual(changed, expected)
        for row_id in [*self.regex, self.rahul]:
            self.assertEqual(after[row_id][:2], ("Self Transfer", "regex+rule"))
        self.assertEqual(after[self.ai][:2], ("Self Transfer", "ai_agent"))
        self.assertEqual(len({after[row_id][2] for row_id in expected}), 1)  # one stamp
        for row_id in self.untouched:
            self.assertEqual(after[row_id], before[row_id])
        self.assertEqual(await self.memory(), {})  # an apply never writes memory

        again = await self.client.put(f"/api/v1/rules/{saved['rule']['id']}", headers=ORIGIN,
                                      json={**self.rule, "apply_to_past": True})
        self.assertEqual((again.status_code, again.json()["changed"]), (200, 0))
        self.assertEqual((await self.preview(**self.rule, id=saved["rule"]["id"]))["count"], 0)

    async def test_an_edit_made_meanwhile_wins(self):
        fresh = await self.seed("Me", "Transfer", "CREDIT", amount=370, raw_message="Rs.370 credited to A/c XX7777",
                                updated_at=datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual((await self.preview(**self.rule))["count"], 9)  # previewed before the edit
        saved = await self.add_rule(apply_to_past=True, **self.rule)
        self.assertEqual(saved["changed"], 8)
        self.assertEqual((await self.row(fresh)).category, "Transfer")

    async def test_without_apply_to_past_nothing_changes(self):
        before = await self.snapshot()
        saved = await self.add_rule(**self.rule)
        self.assertEqual(saved["changed"], 0)
        self.assertEqual(await self.snapshot(), before)

    async def test_a_newer_rule_keeps_its_rows(self):
        older = (await self.add_rule(**self.rule))["rule"]
        await self.add_rule(merchant_words="rahul", category="Income")  # newer: decides Rahul's row
        # Previewed as a new rule it would be the newest and take Rahul's row; in its own place it doesn't.
        self.assertEqual((await self.preview(**self.rule))["count"], 8)
        self.assertEqual((await self.preview(**self.rule, id=older["id"]))["count"], 7)
        response = await self.client.put(f"/api/v1/rules/{older['id']}", headers=ORIGIN,
                                         json={**self.rule, "apply_to_past": True})
        self.assertEqual((response.status_code, response.json()["changed"]), (200, 7))
        self.assertEqual((await self.row(self.rahul)).category, "Transfer")

    async def test_preview_of_another_place_and_missing_id(self):
        response = await self.client.post("/api/v1/rules/preview", headers=ORIGIN, json={**self.rule, "id": str(uuid.uuid4())})
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Rule not found."))

    async def test_deleting_a_rule_keeps_its_categories(self):
        saved = await self.add_rule(apply_to_past=True, **self.rule)
        response = await self.client.delete(f"/api/v1/rules/{saved['rule']['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.row(self.regex[0])).category, "Self Transfer")


# ---------- Phase 2: one limit check afterwards ----------

class LimitCheckTests(DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sign_in()
        clock = patch("limits.utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    async def notifications(self):
        async with AsyncSessionLocal() as db:
            return list(await db.scalars(select(Notification)))

    async def category_limit(self, category, monthly):
        async with AsyncSessionLocal() as db:
            db.add(CategoryLimit(user_id=self.owner.id, category=category, monthly_limit=Decimal(monthly), currency="INR"))
            await db.commit()

    async def test_an_apply_into_a_crossed_limit_notifies_once(self):
        await self.category_limit("Entertainment", 500)
        await self.seed("Qwikly Hub", "Shopping", amount=400, transaction_time=NOW - timedelta(days=2))
        await self.seed("Qwikly Hub", "Shopping", amount=300, transaction_time=NOW - timedelta(days=1))
        saved = await self.add_rule(apply_to_past=True, merchant_words="qwikly", category="Entertainment")
        self.assertEqual(saved["changed"], 2)
        found = await self.notifications()
        self.assertEqual(len(found), 1)
        self.assertIn("Entertainment", found[0].title)

    async def test_last_months_payments_never_notify(self):
        await self.category_limit("Entertainment", 500)
        await self.seed("Qwikly Hub", "Shopping", amount=900, transaction_time=datetime(2026, 9, 15, tzinfo=timezone.utc))
        saved = await self.add_rule(apply_to_past=True, merchant_words="qwikly", category="Entertainment")
        self.assertEqual(saved["changed"], 1)
        self.assertEqual(await self.notifications(), [])

    async def test_a_merge_counts_toward_a_merchant_limit(self):
        async with AsyncSessionLocal() as db:
            db.add(MerchantLimit(user_id=self.owner.id, merchant="Swiggy", merchant_hash=merchants.name_hash("Swiggy"),
                                 currency="INR", monthly_limit=Decimal("1000")))
            await db.commit()
        await self.seed("Swiggy", "Food & Dining", amount=400, transaction_time=NOW - timedelta(days=3))
        await self.seed("SWIGGY", "Food & Dining", amount=350, transaction_time=NOW - timedelta(days=2))
        await self.seed("SWIGGY", "Food & Dining", amount=300, transaction_time=NOW - timedelta(days=1))
        saved = await self.merge("SWIGGY", "Swiggy")
        self.assertEqual(saved["changed"], 2)
        found = await self.notifications()
        self.assertEqual(len(found), 1)
        self.assertIn("Swiggy", found[0].title)


if __name__ == "__main__":
    unittest.main()
