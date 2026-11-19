"""Merchant spending limits: the API, the summary and the alerts. Isolated in-memory SQLite; the
fake Google server from test_alerts stands in for Gmail, so no real mail is sent."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import subprocess
import sys
import unicodedata
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import limits
import merchants
import pay_periods
import settings_store
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import KnownMerchant, MerchantCategory, MerchantLimit, Notification, Transaction, User
from test_alerts import GoogleTestCase, body_text

SERVER = Path(__file__).resolve().parent.parent
ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
# Wednesday 21 Oct 2026, 12:00 IST. Week = Mon 19 – Sun 25 Oct; month = October.
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))
EARLIER_THIS_MONTH = datetime(2026, 10, 5, 12, 0, tzinfo=IST)
LAST_MONTH = datetime(2026, 9, 30, 12, 0, tzinfo=IST)
PAY_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=IST)  # Tue 10 Nov


class ImportOrderTests(unittest.TestCase):
    def test_limits_can_be_imported_before_main(self):
        # categorize imports limits at the top, so limits must not import categorize at the top.
        env = {**os.environ, "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
        code = "import limits, categorize, main; print('ok')"
        result = subprocess.run([sys.executable, "-c", code], cwd=SERVER, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr)


class MerchantLimitTests(GoogleTestCase):
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
        clock = patch("limits.utc_now", return_value=NOW)
        clock.start()
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(clock.stop)
        self.addCleanup(app.dependency_overrides.clear)
        self.counter = 0

    def sign_in(self, user=None):
        app.dependency_overrides[current_user] = lambda: user or self.owner

    async def put(self, merchant, weekly=None, monthly=None, category=None, status=200):
        body = {"merchant": merchant, "weekly_limit": weekly, "monthly_limit": monthly, "category": category}
        response = await self.client.put("/api/v1/limits/merchants", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def set_limits(self, weekly=None, monthly=None, category=None):
        body = {"weekly_limit": weekly, "monthly_limit": monthly, **({"category": category} if category else {})}
        response = await self.client.put("/api/v1/limits", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)

    async def seed(self, merchant, amount, when=NOW, currency="INR", *, parsed=True, kind="DEBIT",
                   owner="owner", category="Food & Dining") -> uuid.UUID:
        """Inserts a payment directly, so names the SMS rules would never store (such as "SWIGGY") exist."""
        self.counter += 1
        user_id = {"owner": self.owner.id, "other": self.other.id, None: None}[owner]
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=kind, amount=amount, currency=currency, merchant=merchant, category=category,
                              is_parsed=parsed, user_id=user_id, raw_message="fixture", message_hash=f"seed-{self.counter}",
                              transaction_time=when.astimezone(timezone.utc))
            db.add(txn)
            await db.commit()
            return txn.id

    async def debit(self, amount, merchant="SWIGGY", when=NOW):
        self.counter += 1
        message = f"Sent Rs.{amount}\nFrom HDFC Bank A/C *4821\nTo {merchant}\nOn 21/10/26\nRef 6294000{self.counter:05d}"
        response = await self.client.post(
            "/api/v1/webhook/sms", json={"message": message, "received_at": when.isoformat()}, headers=HOOK
        )
        self.assertEqual(response.json()["status"], "success", response.text)
        return response.json()

    async def notifications(self):
        items = (await self.client.get("/api/v1/notifications")).json()["items"]
        return sorted(items, key=lambda n: n["title"])

    async def summary(self):
        response = await self.client.get("/api/v1/limits")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def rows(self, model):
        async with AsyncSessionLocal() as db:
            return (await db.scalars(select(model))).all()

    async def preview(self, merchant=None, *, query=None):
        """The preview for `merchant`, or for a raw, already percent-encoded `query` string."""
        url = "/api/v1/limits/merchants/preview" + (f"?merchant={query}" if query is not None else "")
        response = await self.client.get(url, params=None if query is not None else {"merchant": merchant})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ---------- Saving and the summary ----------

    async def test_meter_counts_the_same_words_with_the_same_case(self):
        self.sign_in()
        await self.seed("Swiggy", 100)  # this week
        await self.seed("Swiggy", 60, owner=None)  # an SMS from the Shortcut, which has no owner
        await self.seed("Swiggy Ltd", 200, EARLIER_THIS_MONTH)
        await self.seed("Swiggy", 300, LAST_MONTH)  # not this month, but still in "Counts"
        await self.seed("SWIGGY", 400)  # different case
        await self.seed("Swiggyone", 450)  # not a whole word
        await self.seed("Swiggy", 50, currency="USD")
        await self.seed("Swiggy", 999, parsed=False)  # needs review
        await self.seed("Swiggy", 1000, kind="CREDIT", category="Refunds & Cashback")
        await self.seed("Swiggy", 777, owner="other")
        data = await self.put("Swiggy", weekly="1000", monthly="5000")
        [swiggy] = data["merchants"]
        self.assertEqual(swiggy["merchant"], "Swiggy")
        uuid.UUID(swiggy["id"])
        self.assertEqual(swiggy["counts"], ["Swiggy", "Swiggy Ltd"])
        self.assertEqual(swiggy["weekly"], {"limit": 1000, "spent": 160, "start": "2026-10-19", "end": "2026-10-25", "alerted": False,
                                               "base": None, "carried": None})
        self.assertEqual(swiggy["monthly"], {"limit": 5000, "spent": 360, "start": "2026-10-01", "end": "2026-10-31", "alerted": False,
                                               "base": None, "carried": None})
        # The overall and category meters are unchanged by merchant limits.
        self.assertEqual((data["weekly"]["limit"], data["weekly"]["spent"], data["categories"]), (None, 100 + 60 + 400 + 450, []))
        # A limit on a merchant with no payments yet counts nothing.
        data = await self.put("Zepto", monthly="900", category="Groceries")
        self.assertEqual([m["merchant"] for m in data["merchants"]], ["Swiggy", "Zepto"])  # oldest first
        zepto = data["merchants"][1]
        self.assertEqual((zepto["counts"], zepto["weekly"]["limit"], zepto["monthly"]["spent"]), ([], None, 0))
        self.assertEqual((await self.summary())["merchants"], data["merchants"])

    async def test_a_typed_new_merchant_needs_a_category_and_becomes_known(self):
        self.sign_in()
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                          json={"merchant": "Cult.fit", "weekly_limit": None, "monthly_limit": "2000"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "Pick a category for this new merchant."})
        self.assertEqual((await self.rows(KnownMerchant), await self.rows(MerchantLimit), await self.rows(MerchantCategory)), ([], [], []))

        data = await self.put("  Cult.fit ", monthly="2000", category="entertainment")
        [limit] = data["merchants"]
        self.assertEqual((limit["merchant"], limit["counts"], limit["monthly"]["spent"]), ("Cult.fit", [], 0))
        [known] = await self.rows(KnownMerchant)
        [memory] = await self.rows(MerchantCategory)
        self.assertEqual(known.name, "Cult.fit")
        self.assertEqual((memory.user_id, memory.merchant_key, memory.type, memory.category),
                         (self.owner.id, "cult.fit", "DEBIT", "Entertainment"))
        # Memory ignores case: an SMS from "CULT.FIT" (stored "Cult.fit") gets the category, and counts.
        await self.debit("1500.00", merchant="CULT.FIT")
        [txn] = (await self.client.get("/api/v1/transactions")).json()
        self.assertEqual((txn["merchant"], txn["category"]), ("Cult.fit", "Entertainment"))
        [limit] = (await self.summary())["merchants"]
        self.assertEqual((limit["counts"], limit["monthly"]["spent"]), (["Cult.fit"], 1500))
        # It stays known after its limit is deleted.
        await self.put("Cult.fit")
        known = (await self.client.get("/api/v1/limits/merchants/known")).json()
        self.assertEqual(known, [{"name": "Cult.fit", "payments": 1}])

    async def test_a_known_merchant_needs_no_category_and_keeps_its_own(self):
        self.sign_in()
        await self.seed("Swiggy", 100)
        await self.put("Swiggy", weekly="500", category="Shopping")  # ignored: Swiggy is known
        self.assertEqual((await self.rows(MerchantCategory), await self.rows(KnownMerchant)), ([], []))
        await self.put("Zepto", weekly="500", category="Groceries")
        await self.put("Zepto")  # deleted; Zepto stays known
        await self.put("Zepto", weekly="700")  # so a new limit on it needs no category
        self.assertEqual(len(await self.rows(KnownMerchant)), 1)

    async def test_a_typed_case_variant_keeps_the_remembered_category(self):
        self.sign_in()
        async with AsyncSessionLocal() as db:
            db.add(MerchantCategory(user_id=self.owner.id, merchant_key="swiggy", type="DEBIT", category="Food & Dining"))
            await db.commit()
        await self.seed("Swiggy", 100)
        data = await self.put("swiggy", weekly="500", category="Shopping")  # "swiggy" itself is new
        self.assertEqual(data["merchants"][0]["counts"], [])  # "Swiggy" has a different case
        [memory] = await self.rows(MerchantCategory)
        self.assertEqual(memory.category, "Food & Dining")
        self.assertEqual([k.name for k in await self.rows(KnownMerchant)], ["swiggy"])

    async def test_validation(self):
        self.sign_in()
        for category in ["Uncategorized", "Salary", "Income", "Refunds & Cashback", "Snacks"]:
            with self.subTest(category=category):
                response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                                 json={"merchant": "Cult.fit", "weekly_limit": "100", "category": category})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("Pick a spending category.", response.text)
        for merchant, message in [("", "Enter a merchant name."), ("   ", "Enter a merchant name."),
                                  ("!!!", "Enter a merchant name."), ("unknown", "Enter a merchant name."),
                                  ("A" * 256, "Merchant names can be up to 255 characters.")]:
            with self.subTest(merchant=merchant):
                response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                                 json={"merchant": merchant, "weekly_limit": "100", "category": "Shopping"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(message, response.json()["detail"][0]["msg"])
        for body in [{"weekly_limit": 0}, {"monthly_limit": -5}, {"weekly_limit": "10.555"}, {"monthly_limit": "1e16"}]:
            with self.subTest(body=body):
                response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                                 json={"merchant": "Cult.fit", "category": "Shopping", **body})
                self.assertEqual(response.status_code, 422, response.text)
        # 255 characters after trimming is fine.
        data = await self.put(" " + "A" * 255 + " ", weekly="100", category="Shopping")
        self.assertEqual([m["merchant"] for m in data["merchants"]], ["A" * 255])

    async def test_invalid_characters_are_rejected(self):
        self.sign_in()
        # Raw JSON: a lone surrogate can't be sent any other way.
        for merchant in ["Swiggy \\ud800", "Swi\\u0000ggy", "Swiggy\\u0007", "Swiggy\\u009f", "Swiggy \\ufffd"]:
            with self.subTest(merchant=merchant):
                body = '{"merchant": "%s", "weekly_limit": "100", "category": "Shopping"}' % merchant
                response = await self.client.put("/api/v1/limits/merchants", content=body,
                                                 headers={**ORIGIN, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("Merchant names can't contain invalid characters.", response.json()["detail"][0]["msg"])
        self.assertEqual((await self.rows(MerchantLimit), await self.rows(KnownMerchant)), ([], []))
        # Tabs and newlines are only spaces.
        data = await self.put("Swiggy\tLtd\n", weekly="100", category="Shopping")
        self.assertEqual([m["merchant"] for m in data["merchants"]], ["Swiggy Ltd"])

    async def test_names_that_differ_in_case_are_separate_limits(self):
        self.sign_in()
        await self.seed("Swiggy", 100)
        await self.seed("SWIGGY", 40)
        await self.put("Swiggy", weekly="500")
        data = await self.put("SWIGGY", weekly="700")
        self.assertEqual([(m["merchant"], m["weekly"]["limit"], m["weekly"]["spent"]) for m in data["merchants"]],
                         [("Swiggy", 500, 100), ("SWIGGY", 700, 40)])
        data = await self.put("Swiggy  ", weekly="900")  # the same name once cleaned: updated, not added
        self.assertEqual([(m["merchant"], m["weekly"]["limit"]) for m in data["merchants"]], [("Swiggy", 900), ("SWIGGY", 700)])
        self.assertEqual(len(await self.rows(MerchantLimit)), 2)

    async def test_two_saves_at_once_end_with_one_limit(self):
        self.sign_in()
        real_commit = AsyncSession.commit
        commits = []

        async def racing_commit(session):
            # A simultaneous save (its own amounts and category) commits first, so this save's
            # commit fails on the unique key and rolls back; the retry must find that row and update it.
            commits.append(1)
            if len(commits) == 1:
                await session.rollback()
                async with AsyncSessionLocal() as winner:
                    winner.add(KnownMerchant(user_id=self.owner.id, name="Cult.fit", name_hash=merchants.name_hash("Cult.fit")))
                    winner.add(MerchantCategory(user_id=self.owner.id, merchant_key="cult.fit", type="DEBIT", category="Health & Fitness"))
                    winner.add(MerchantLimit(user_id=self.owner.id, merchant="Cult.fit", merchant_hash=merchants.name_hash("Cult.fit"),
                                             currency="INR", weekly_limit=500, monthly_limit=None))
                    await real_commit(winner)
                raise IntegrityError("INSERT INTO merchant_limits", {}, Exception("UNIQUE constraint failed"))
            await real_commit(session)

        with patch.object(AsyncSession, "commit", racing_commit):
            data = await self.put("Cult.fit", monthly="2000", category="Entertainment")
        # The retry applied this save's amounts to the winner's limit.
        [limit] = data["merchants"]
        self.assertEqual((limit["merchant"], limit["weekly"]["limit"], limit["monthly"]["limit"]), ("Cult.fit", None, 2000))
        [row] = await self.rows(MerchantLimit)
        self.assertEqual((row.weekly_limit, row.monthly_limit), (None, 2000))
        # The losing attempt's known merchant and memory were rolled back: only the winner's remain.
        self.assertEqual(len(await self.rows(KnownMerchant)), 1)
        [memory] = await self.rows(MerchantCategory)
        self.assertEqual(memory.category, "Health & Fitness")

    async def test_a_limit_whose_name_no_longer_appears_can_be_edited(self):
        self.sign_in()
        txn_id = await self.seed("Swiggy", 100)
        await self.put("Swiggy", monthly="1000")
        response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json={"merchant": "Zomato"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        data = await self.put("Swiggy", monthly="2000")  # no category needed
        [limit] = data["merchants"]
        self.assertEqual((limit["monthly"]["limit"], limit["monthly"]["spent"], limit["counts"]), (2000, 0, []))

    async def test_both_amounts_empty_removes_the_limit(self):
        self.sign_in()
        await self.seed("Swiggy", 100)
        await self.put("Swiggy", weekly="500")
        before = await self.put("Zomato")  # no limit, and not even known: nothing to do
        self.assertEqual([m["merchant"] for m in before["merchants"]], ["Swiggy"])
        self.assertEqual(await self.rows(KnownMerchant), [])
        data = await self.put("Swiggy")
        self.assertEqual(data["merchants"], [])
        self.assertEqual(await self.rows(MerchantLimit), [])

    # ---------- Alerts ----------

    async def test_crossing_a_merchant_limit_notifies_once(self):
        self.sign_in()
        await self.seed("Swiggy", 2800, EARLIER_THIS_MONTH)
        await self.put("Swiggy", monthly="3000")
        await self.debit("400.00", merchant="SWIGGY LTD")  # stored "Swiggy Ltd"
        [note] = await self.notifications()
        self.assertEqual(note["title"], "Monthly Swiggy limit crossed")
        self.assertEqual(note["body"], "₹3,200.00 of ₹3,000.00 at Swiggy this month (Thu 1 Oct – Sat 31 Oct) · 106%")
        self.assertEqual((note["kind"], note["link"]), ("spend_limit", "/settings/limits"))
        [mail] = self.google.sent
        self.assertEqual(mail["Subject"], "Monthly Swiggy limit crossed: ₹3,200.00 of ₹3,000.00")
        self.assertIn("You've spent ₹3,200.00 at Swiggy this month", body_text(mail))
        self.assertIn("of your ₹3,000.00 monthly Swiggy limit", body_text(mail))
        await self.debit("10.00", merchant="SWIGGY")
        self.assertEqual((len(await self.notifications()), len(self.google.sent)), (1, 1))
        [limit] = (await self.summary())["merchants"]
        self.assertTrue(limit["monthly"]["alerted"])
        self.assertEqual(limit["monthly"]["spent"], 3210)

    async def test_spending_equal_to_the_limit_is_reached(self):
        self.sign_in()
        await self.put("Zomato", weekly="500", category="Food & Dining")
        await self.debit("500.00", merchant="ZOMATO")
        self.assertEqual([n["title"] for n in await self.notifications()], ["Weekly Zomato limit reached"])

    async def test_names_the_limit_does_not_count_never_alert(self):
        self.sign_in()
        await self.put("Swiggy", weekly="100", monthly="100", category="Food & Dining")
        await self.seed("SWIGGY", 500)
        await self.seed("Swiggy", 500, currency="USD")
        for txn in (await self.rows(Transaction)):
            await limits.check_spend_limits(txn.id)
        await self.debit("500.00", merchant="SWIGGYONE")
        self.assertEqual(await self.notifications(), [])

    async def test_raising_the_limit_notifies_again(self):
        self.sign_in()
        await self.put("Swiggy", weekly="500", category="Food & Dining")
        await self.debit("600.00")
        await self.put("Swiggy", weekly="1000")
        await self.debit("300.00")  # 900 < 1000 (90%: a spend_warning, tests/test_limit_warnings.py)
        self.assertEqual(len([n for n in await self.notifications() if n["kind"] == "spend_limit"]), 1)
        await self.debit("150.00")  # 1050 >= 1000
        notes = [n for n in await self.notifications() if n["kind"] == "spend_limit"]
        self.assertEqual([n["title"] for n in notes], ["Weekly Swiggy limit crossed"] * 2)
        self.assertEqual(len(self.google.sent), 2)
        self.assertIn("₹1,000.00", body_text(self.google.sent[1]))

    async def test_one_payment_crossing_three_limits_notifies_three_times(self):
        self.sign_in()
        await self.seed("Swiggy", 2800, EARLIER_THIS_MONTH)
        await self.set_limits(monthly="3000")
        await self.set_limits(monthly="3000", category="Food & Dining")
        await self.put("Swiggy", monthly="3000")
        await self.debit("400.00")
        self.assertEqual([n["title"] for n in await self.notifications()],
                         ["Monthly Food & Dining limit crossed", "Monthly Swiggy limit crossed", "Monthly spend limit crossed"])
        subjects = sorted(mail["Subject"] for mail in self.google.sent)
        self.assertEqual(subjects, [
            "Monthly Food & Dining limit crossed: ₹3,200.00 of ₹3,000.00",
            "Monthly Swiggy limit crossed: ₹3,200.00 of ₹3,000.00",
            "Monthly spend limit crossed: ₹3,200.00 of ₹3,000.00",
        ])
        [merchant_mail] = [mail for mail in self.google.sent if "Swiggy limit" in mail["Subject"]]
        self.assertIn("₹3,200.00 at Swiggy this month", body_text(merchant_mail))

    async def test_a_failed_email_is_not_retried(self):
        self.sign_in()
        await self.put("Swiggy", weekly="100", category="Food & Dining")
        self.google.send_error = "Backend Error"
        with self.assertLogs("limits", "WARNING") as logs:
            await self.debit("150.00")
        self.assertIn("Merchant limit email failed", "\n".join(logs.output))
        self.assertEqual([n["title"] for n in await self.notifications()], ["Weekly Swiggy limit crossed"])
        self.google.send_error = None
        await self.debit("10.00")
        self.assertEqual(self.google.sent, [])

    async def test_a_late_sms_for_last_month_does_not_alert(self):
        self.sign_in()
        await self.put("Swiggy", weekly="100", monthly="100", category="Food & Dining")
        await self.debit("500.00", when=LAST_MONTH)
        self.assertEqual(await self.notifications(), [])

    async def test_renaming_a_payment_into_a_limit_alerts(self):
        self.sign_in()
        await self.put("Swiggy", weekly="500", category="Food & Dining")
        await self.debit("600.00", merchant="ZOMATO")
        self.assertEqual(await self.notifications(), [])
        [txn] = (await self.client.get("/api/v1/transactions")).json()
        await self.client.patch(f"/api/v1/transactions/{txn['id']}", json={"merchant": "Swiggy Instamart"}, headers=ORIGIN)
        self.assertEqual([n["title"] for n in await self.notifications()], ["Weekly Swiggy limit crossed"])

    async def test_a_long_name_is_shortened_in_the_title_only(self):
        self.sign_in()
        name = "Shop " + "x" * 245
        await self.seed(name, 100, EARLIER_THIS_MONTH)
        await self.put(name, monthly="150")
        await limits.check_spend_limits(await self.seed(name, 60))
        [note] = await self.notifications()
        label = note["title"].removeprefix("Monthly ").removesuffix(" limit crossed")
        self.assertEqual((len(name), len(label), label), (250, 120, name[:119] + "…"))
        self.assertLessEqual(len(note["title"]), 200)
        self.assertIn(f"at {name} this month", note["body"])

    async def test_a_failing_merchant_check_does_not_stop_the_overall_and_category_alerts(self):
        self.sign_in()
        await self.set_limits(weekly="100")
        await self.set_limits(weekly="100", category="Food & Dining")
        await self.put("Swiggy", weekly="100", category="Food & Dining")
        with patch("limits.merchants.meter", side_effect=RuntimeError("boom")), self.assertLogs("limits", "ERROR") as logs:
            await self.debit("150.00")
        self.assertEqual([n["title"] for n in await self.notifications()],
                         ["Weekly Food & Dining limit crossed", "Weekly spend limit crossed"])
        self.assertIn("Merchant limit check failed", "\n".join(logs.output))
        self.assertNotIn("Spend-limit check failed", "\n".join(logs.output))

    async def test_one_payment_matching_two_merchant_limits_notifies_both(self):
        self.sign_in()
        await self.put("Swiggy", weekly="100", category="Food & Dining")
        await self.put("Swiggy Instamart", weekly="100", category="Groceries")
        await limits.check_spend_limits(await self.seed("Swiggy Instamart", 150))
        self.assertEqual([n["title"] for n in await self.notifications()],
                         ["Weekly Swiggy Instamart limit crossed", "Weekly Swiggy limit crossed"])
        self.assertEqual(sorted(mail["Subject"] for mail in self.google.sent), [
            "Weekly Swiggy Instamart limit crossed: ₹150.00 of ₹100.00",
            "Weekly Swiggy limit crossed: ₹150.00 of ₹100.00",
        ])

    async def test_a_late_payment_from_last_week_alerts_only_for_the_month(self):
        self.sign_in()
        await self.put("Swiggy", weekly="100", monthly="100", category="Food & Dining")
        last_week = NOW - timedelta(days=7)  # Wed 14 Oct: this month, but last week
        await limits.check_spend_limits(await self.seed("Swiggy", 150, last_week))
        self.assertEqual([n["title"] for n in await self.notifications()], ["Monthly Swiggy limit crossed"])

    async def test_a_lost_notification_race_does_not_skip_the_next_period(self):
        # When a simultaneous SMS creates the weekly notification first, create_once rolls back,
        # which expires every object in the session; the monthly ones must still go out.
        self.sign_in()
        await self.set_limits(weekly="100", monthly="100")
        await self.set_limits(weekly="100", monthly="100", category="Food & Dining")
        await self.put("Swiggy", weekly="100", monthly="100", category="Food & Dining")
        real_create_once, lost = limits.create_once, []

        async def lose_weekly(db, user_id, **fields):
            if ":weekly:" in fields["dedupe_key"]:
                lost.append(fields["dedupe_key"])
                await db.rollback()  # what create_once does on the unique-key conflict
                return False
            return await real_create_once(db, user_id, **fields)

        with patch("limits.create_once", lose_weekly), self.assertNoLogs("limits", "WARNING"):
            await self.debit("150.00")
        self.assertEqual(len(lost), 3)  # overall, category and merchant
        self.assertEqual([n["title"] for n in await self.notifications()], [
            "Monthly Food & Dining limit crossed", "Monthly Swiggy limit crossed", "Monthly spend limit crossed",
        ])
        subjects = [mail["Subject"] for mail in self.google.sent]
        self.assertIn("Monthly Swiggy limit crossed: ₹150.00 of ₹100.00", subjects)
        self.assertIn("Monthly Food & Dining limit crossed: ₹150.00 of ₹100.00", subjects)

    # ---------- Round trips ----------

    async def statements_for_summary(self) -> int:
        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await self.summary()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        return len(statements)

    async def test_summary_round_trips(self):
        # The database can be a continent away (~250 ms a round trip).
        self.sign_in()
        await self.set_limits(weekly="500")
        await self.set_limits(weekly="100", category="Food & Dining")
        self.assertEqual(await self.statements_for_summary(), 4)  # rules, merchant limits, pay-period setting, meters
        await self.put("Swiggy", weekly="100", category="Food & Dining")
        await self.put("Zomato", monthly="900", category="Food & Dining")
        await self.debit("150.00")
        self.assertEqual(await self.statements_for_summary(), 5)  # plus one read of your debits
        merchants = (await self.summary())["merchants"]
        self.assertEqual([(m["merchant"], m["weekly"]["alerted"], m["monthly"]["alerted"]) for m in merchants],
                         [("Swiggy", True, False), ("Zomato", False, False)])

    async def statements_for_check(self, transaction_id) -> int:
        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await limits.check_spend_limits(transaction_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        return len(statements)

    async def test_alert_check_round_trips(self):
        self.sign_in()
        await self.set_limits(weekly="100000")  # never reached, so no notification is written
        await self.put("Swiggy", weekly="100000", category="Food & Dining")
        zomato, swiggy = await self.seed("Zomato", 10), await self.seed("Swiggy", 10)
        with patch("limits._check_merchants", AsyncMock()):  # the same check without merchant limits
            before = await self.statements_for_check(zomato)
            self.assertEqual(await self.statements_for_check(swiggy), before)
        self.assertEqual(await self.statements_for_check(zomato), before + 1)  # load merchant limits
        self.assertEqual(await self.statements_for_check(swiggy), before + 2)  # plus one read of your debits

    async def test_summary_function_without_merchant_limits_is_unchanged(self):
        self.sign_in()
        await self.put("Swiggy", weekly="100", category="Food & Dining")
        async with AsyncSessionLocal() as db:
            summary = await limits.summarize(db, self.owner, await limits.load_rules(db, self.owner.id))
        self.assertEqual(summary.merchants, [])

    # ---------- Pay periods ('Month starts on payday', pay_periods.py) ----------

    async def test_monthly_merchant_limit_in_pay_mode(self):
        self.sign_in()
        await self.seed("Acme Payroll", 50000, datetime(2026, 10, 31, 10, 0, tzinfo=IST), kind="CREDIT", category="Salary")
        async with AsyncSessionLocal() as db:
            await settings_store.save(db, self.owner.id, {"periods.payday": True})
        await self.seed("Swiggy", 2000, datetime(2026, 10, 30, 12, 0, tzinfo=IST))  # before the period
        await self.seed("Swiggy", 2800, datetime(2026, 10, 31, 12, 0, tzinfo=IST))
        limit_id = next(m["id"] for m in (await self.put("Swiggy", monthly="3000"))["merchants"])
        with patch("limits.utc_now", return_value=PAY_NOW):
            await self.debit("400.00", merchant="SWIGGY LTD", when=PAY_NOW)
            [limit] = (await self.summary())["merchants"]
            with patch("pay_periods.load", wraps=pay_periods.load) as load:
                self.assertEqual(await self.preview("Swiggy"), {"known": True, "counts": ["Swiggy", "Swiggy Ltd"]})
            load.assert_called_once()
        [note] = await self.notifications()
        self.assertEqual(note["body"], "₹3,200.00 of ₹3,000.00 at Swiggy this month (Sat 31 Oct – Sun 29 Nov) · 106%")
        async with AsyncSessionLocal() as db:
            keys = list(await db.scalars(select(Notification.dedupe_key)))
        self.assertEqual(keys, [f"spend_limit:merchant:{limit_id}:monthly:2026-10-31:3000.00"])
        self.assertEqual((limit["monthly"]["start"], limit["monthly"]["end"], limit["monthly"]["spent"]),
                         ("2026-10-31", "2026-11-29", 3200))
        self.assertTrue(limit["monthly"]["alerted"])

    # ---------- Known merchants and the preview ----------

    async def test_known_merchants(self):
        self.sign_in()
        await self.seed("Swiggy", 1)
        await self.seed("Swiggy", 2, LAST_MONTH)
        await self.seed("SWIGGY", 3)
        await self.seed("Swiggy Ltd", 4)
        await self.seed("Unknown", 5)
        await self.seed(None, 6)
        await self.seed("!!!", 7)
        await self.seed("Zomato", 8, currency="USD")
        await self.seed("Dunzo", 9, parsed=False)
        await self.seed("Salary Co", 10, kind="CREDIT", category="Salary")
        await self.seed("Blinkit", 11, owner="other")
        await self.seed("Ola", 12, owner=None)  # from the Shortcut: yours
        await self.put("Cult.fit", weekly="100", category="Health & Fitness")
        await self.put("Ola", weekly="100")  # known from its payment, so not stored as typed
        known = (await self.client.get("/api/v1/limits/merchants/known")).json()
        self.assertEqual(known, [
            {"name": "Swiggy", "payments": 2},
            {"name": "Ola", "payments": 1},
            {"name": "SWIGGY", "payments": 1},
            {"name": "Swiggy Ltd", "payments": 1},
            {"name": "Cult.fit", "payments": 0},
        ])
        self.sign_in(self.other)
        self.assertEqual((await self.client.get("/api/v1/limits/merchants/known")).json(),
                         [{"name": "Blinkit", "payments": 1}, {"name": "Ola", "payments": 1}])

    async def test_preview(self):
        self.sign_in()
        await self.seed("Swiggy", 1)
        await self.seed("Swiggy Ltd", 2)
        await self.seed("Swiggy Ltd", 3, LAST_MONTH)
        await self.seed("SWIGGY", 4)
        await self.put("Cult.fit", weekly="100", category="Health & Fitness")

        async def preview(merchant):
            response = await self.client.get("/api/v1/limits/merchants/preview", params={"merchant": merchant})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        self.assertEqual(await preview("Swiggy"), {"known": True, "counts": ["Swiggy Ltd", "Swiggy"]})
        self.assertEqual(await preview("  Swiggy   Ltd "), {"known": True, "counts": ["Swiggy Ltd"]})
        self.assertEqual(await preview("SWIGGY"), {"known": True, "counts": ["SWIGGY"]})
        self.assertEqual(await preview("swiggy"), {"known": False, "counts": []})  # known only with its own case
        self.assertEqual(await preview("Ltd"), {"known": False, "counts": ["Swiggy Ltd"]})
        self.assertEqual(await preview("Cult.fit"), {"known": True, "counts": []})  # typed earlier
        for merchant in ["", "   ", "!!!", "Unknown", "A" * 300]:
            with self.subTest(merchant=merchant):
                self.assertEqual(await preview(merchant), {"known": False, "counts": []})
        response = await self.client.get("/api/v1/limits/merchants/preview")  # no merchant at all
        self.assertEqual(response.json(), {"known": False, "counts": []})

    async def test_preview_of_invalid_characters_gives_nothing(self):
        self.sign_in()
        await self.seed("Swiggy", 1)  # each name below would otherwise count it
        for merchant in ["Swi\x00ggy", "Swiggy\x07", "Swiggy \ufffd"]:
            with self.subTest(merchant=merchant.encode("unicode_escape")):
                self.assertEqual(await self.preview(merchant), {"known": False, "counts": []})
        # A lone surrogate, sent as its UTF-8-style bytes.
        self.assertEqual(await self.preview(query="Swiggy%20%ED%A0%80"), {"known": False, "counts": []})

    async def test_names_are_compared_and_listed_cleaned(self):
        self.sign_in()
        await self.seed("Amazon  Pay", 100)  # the AI parser only trims
        await self.seed("Amazon Pay", 50)
        await self.seed("D/Mart 50%_off", 10)  # LIKE's special characters
        known = (await self.client.get("/api/v1/limits/merchants/known")).json()
        self.assertEqual(known, [{"name": "Amazon Pay", "payments": 2}, {"name": "D/Mart 50%_off", "payments": 1}])
        self.assertEqual(await self.preview("Amazon Pay"), {"known": True, "counts": ["Amazon Pay"]})
        self.assertEqual(await self.preview("D/Mart 50%_off"), {"known": True, "counts": ["D/Mart 50%_off"]})
        self.assertEqual((await self.preview("D/Mart 5_%_off"))["known"], False)  # wildcards match only themselves
        # Picked from the list, each needs no category, so neither becomes a typed merchant.
        await self.put("Amazon Pay", weekly="500")
        data = await self.put("D/Mart 50%_off", weekly="500")
        self.assertEqual([(m["merchant"], m["weekly"]["spent"]) for m in data["merchants"]],
                         [("Amazon Pay", 150), ("D/Mart 50%_off", 10)])
        self.assertEqual(await self.rows(KnownMerchant), [])

    async def test_a_decomposed_stored_name_is_known(self):
        self.sign_in()
        await self.seed(unicodedata.normalize("NFD", "Café Coffee Day"), 100)  # hand-typed or AI-read
        self.assertEqual((await self.client.get("/api/v1/limits/merchants/known")).json(),
                         [{"name": "Café Coffee Day", "payments": 1}])
        self.assertEqual(await self.preview("Café Coffee Day"), {"known": True, "counts": ["Café Coffee Day"]})
        data = await self.put("Café Coffee Day", weekly="500")  # picked from the list: no category asked
        self.assertEqual([(m["merchant"], m["weekly"]["spent"]) for m in data["merchants"]], [("Café Coffee Day", 100)])
        self.assertEqual((await self.rows(KnownMerchant), await self.rows(MerchantCategory)), ([], []))

    async def test_names_with_invalid_characters_are_not_offered(self):
        self.sign_in()
        await self.seed("AMAZON\ufffdPAY", 100)  # a badly decoded SMS
        await self.seed("ZEPTO\x1bMART", 50)
        await self.seed("Zepto", 20)
        self.assertEqual((await self.client.get("/api/v1/limits/merchants/known")).json(), [{"name": "Zepto", "payments": 1}])

    async def test_invalid_characters_in_other_fields_are_a_422(self):
        self.sign_in()
        bodies = [
            ("/api/v1/limits/merchants", '{"merchant": "Swiggy", "weekly_limit": "100", "category": "\\ud800"}'),
            ("/api/v1/limits/merchants", '{"merchant": "Swiggy", "weekly_limit": "\\ud800"}'),
            ("/api/v1/limits", '{"category": "\\ud800", "weekly_limit": "100"}'),
            ("/api/v1/limits", '{"weekly_limit": "\\ud800"}'),
        ]
        for url, body in bodies:
            with self.subTest(url=url, body=body):
                response = await self.client.put(url, content=body, headers={**ORIGIN, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)

    async def test_editing_a_limit_deleted_elsewhere_does_not_recreate_it(self):
        self.sign_in()
        await self.seed("Nykaa", 100)
        limit_id = (await self.put("Nykaa", monthly="900"))["merchants"][0]["id"]
        await self.put("Nykaa")  # deleted on another device
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                         json={"id": limit_id, "merchant": "Nykaa", "monthly_limit": "950"})
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "This limit was deleted."))
        self.assertEqual(await self.rows(MerchantLimit), [])
        # Removing it again (a swipe on a stale list) is no error.
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN, json={"id": limit_id, "merchant": "Nykaa"})
        self.assertEqual((response.status_code, response.json()["merchants"]), (200, []))
        # With its id, an edit changes that limit.
        limit_id = (await self.put("Nykaa", monthly="900"))["merchants"][0]["id"]
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                         json={"id": limit_id, "merchant": "Nykaa", "monthly_limit": "950"})
        self.assertEqual([(m["id"], m["monthly"]["limit"]) for m in response.json()["merchants"]], [(limit_id, 950)])

    async def test_a_name_with_a_limit_is_known(self):
        self.sign_in()
        txn_id = await self.seed("Swiggy", 100)
        await self.put("Swiggy", monthly="1000")  # known from its payment, so not stored as typed
        response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json={"merchant": "Zomato"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.rows(KnownMerchant), [])
        self.assertEqual(await self.preview("Swiggy"), {"known": True, "counts": []})
        self.assertEqual(await self.preview(" Swiggy "), {"known": True, "counts": []})
        self.assertEqual(await self.preview("SWIGGY"), {"known": False, "counts": []})

    # ---------- Access ----------

    async def test_requires_login_and_same_origin(self):
        body = {"merchant": "Swiggy", "weekly_limit": "100", "category": "Food & Dining"}
        for response in [
            await self.client.put("/api/v1/limits/merchants", json=body, headers=ORIGIN),
            await self.client.get("/api/v1/limits/merchants/known"),
            await self.client.get("/api/v1/limits/merchants/preview", params={"merchant": "Swiggy"}),
        ]:
            self.assertEqual(response.status_code, 401)
        self.sign_in()
        foreign = await self.client.put("/api/v1/limits/merchants", json=body, headers={"Origin": "https://attacker.example"})
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(await self.rows(MerchantLimit), [])

    async def test_another_users_limits_are_invisible(self):
        self.sign_in()
        await self.put("Swiggy", weekly="100", category="Food & Dining")
        self.sign_in(self.other)
        self.assertEqual((await self.summary())["merchants"], [])
        self.assertEqual((await self.client.get("/api/v1/limits/merchants/known")).json(), [])
        data = await self.put("Swiggy", weekly="999", category="Shopping")  # their own, separate limit
        self.assertEqual([m["weekly"]["limit"] for m in data["merchants"]], [999])
        self.sign_in()
        self.assertEqual([m["weekly"]["limit"] for m in (await self.summary())["merchants"]], [100])
        async with AsyncSessionLocal() as db:
            count = await db.scalar(select(func.count()).select_from(Notification))
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
