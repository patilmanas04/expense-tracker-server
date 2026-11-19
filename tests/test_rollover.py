"""Rollover budgets (limits.py): the carried limit in the summary and the alerts, PUT /limits/rollover, and the
cleanup when a limit goes. Isolated in-memory SQLite; the fake Google server from test_alerts stands in for Gmail,
so no real mail is sent."""
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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

import limits
import settings_store
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import LimitRollover, Notification, SpendAlert, Transaction, User
from test_alerts import GoogleTestCase, body_text

ORIGIN = {"Origin": "http://localhost:3000"}
BAD_ORIGIN = {"Origin": "https://evil.example"}
IST = timezone(timedelta(hours=5, minutes=30))
# Thursday 5 Nov 2026, 12:00 IST. Month = November; last month = October.
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=IST).astimezone(timezone.utc)
SEP_15 = datetime(2026, 9, 15, 10, 0, tzinfo=IST)
OCTOBER = datetime(2026, 10, 12, 12, 0, tzinfo=IST)
NOVEMBER = datetime(2026, 11, 3, 12, 0, tzinfo=IST)
FOOD = "Food & Dining"


class PureTests(unittest.TestCase):
    def test_rollover_limit_clamps(self):
        base = Decimal("5000")
        cases = {"4200": "5800", "6500": "3500", "0": "10000", "12000": "0", "5000": "5000", "10000": "0"}
        for previous, expected in cases.items():
            with self.subTest(previous=previous):
                self.assertEqual(limits.rollover_limit(base, Decimal(previous)), Decimal(expected))

    def test_previous_window_and_carry(self):
        windows = {"weekly": limits.period_window("weekly", NOW), "monthly": limits.period_window("monthly", NOW)}
        start, _, end = limits.previous_monthly_window(windows)
        self.assertEqual((start, limits._last_day(end)), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertTrue(limits.carry_applies(Decimal(5000), SEP_15, windows))
        self.assertTrue(limits.carry_applies(Decimal(5000), datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc), windows))
        # 1 Nov 00:30 IST is still 31 Oct in UTC, but it's November in India: no carry yet.
        self.assertFalse(limits.carry_applies(Decimal(5000), datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc), windows))
        self.assertFalse(limits.carry_applies(None, SEP_15, windows))
        self.assertFalse(limits.carry_applies(Decimal(5000), None, windows))
        self.assertFalse(limits.carry_applies(Decimal(5000), SEP_15, {**windows, "previous_monthly": None}))

    def test_percent_of_a_zero_limit(self):
        self.assertEqual(limits._percent(Decimal("50"), Decimal("0")), 100)
        self.assertEqual(limits._percent(Decimal("50"), Decimal("200")), 25)


class RolloverTests(GoogleTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSessionLocal() as db:
            self.owner = User(email="owner@example.com", name="Owner")
            db.add(self.owner)
            await db.commit()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")
        self.now = NOW
        clock = patch("limits.utc_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(app.dependency_overrides.clear)
        self.counter = 0
        self.sign_in()

    def sign_in(self):
        app.dependency_overrides[current_user] = lambda: self.owner

    async def seed(self, amount, when, category=FOOD, kind="DEBIT", merchant="Cafe") -> uuid.UUID:
        self.counter += 1
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=kind, amount=amount, currency="INR", merchant=merchant, category=category,
                              is_parsed=True, user_id=self.owner.id, raw_message="fixture",
                              message_hash=f"seed-{self.counter}", transaction_time=when.astimezone(timezone.utc))
            db.add(txn)
            await db.commit()
            return txn.id

    async def set_limits(self, weekly=None, monthly=None, category=None, status=200):
        body = {"weekly_limit": weekly, "monthly_limit": monthly, **({"category": category} if category else {})}
        response = await self.client.put("/api/v1/limits", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def rollover(self, category, enabled, status=200, headers=ORIGIN):
        response = await self.client.put("/api/v1/limits/rollover", json={"category": category, "enabled": enabled},
                                         headers=headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def enable_since(self, category, when):
        async with AsyncSessionLocal() as db:
            db.add(LimitRollover(user_id=self.owner.id, category=category, enabled_at=when.astimezone(timezone.utc)))
            await db.commit()

    async def summary(self):
        response = await self.client.get("/api/v1/limits")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def food(self):
        return next(c for c in (await self.summary())["categories"] if c["category"] == FOOD)

    async def rows(self, model):
        async with AsyncSessionLocal() as db:
            return (await db.scalars(select(model))).all()

    async def notifications(self, kind):
        return [n for n in await self.rows(Notification) if n.kind == kind]

    def detail(self, response_json):
        detail = response_json["detail"]
        return detail[0]["msg"].removeprefix("Value error, ") if isinstance(detail, list) else detail

    # ---------- Summary ----------

    async def test_unspent_amount_rolls_over(self):
        await self.set_limits(monthly="5000", category=FOOD)
        await self.enable_since(FOOD, SEP_15)
        await self.seed("4200", OCTOBER)
        await self.seed("300", NOVEMBER)
        await self.seed("999", OCTOBER, category="Shopping")  # another category's spending doesn't carry
        food = await self.food()
        self.assertTrue(food["rollover"])
        self.assertEqual({k: food["monthly"][k] for k in ("limit", "base", "carried", "spent")},
                         {"limit": 5800, "base": 5000, "carried": 800, "spent": 300})
        self.assertEqual((food["weekly"]["base"], food["weekly"]["carried"]), (None, None))
        self.assertFalse((await self.summary())["rollover"])

    async def test_overspend_takes_off(self):
        await self.set_limits(monthly="5000", category=FOOD)
        await self.enable_since(FOOD, SEP_15)
        await self.seed("6500", OCTOBER)
        monthly = (await self.food())["monthly"]
        self.assertEqual((monthly["limit"], monthly["carried"]), (3500, -1500))

    async def test_clamps(self):
        await self.set_limits(monthly="5000", category=FOOD)
        await self.enable_since(FOOD, SEP_15)
        monthly = (await self.food())["monthly"]
        self.assertEqual((monthly["limit"], monthly["carried"]), (10000, 5000))
        await self.seed("12000", OCTOBER)
        monthly = (await self.food())["monthly"]
        self.assertIsNotNone(monthly["limit"])
        self.assertEqual((monthly["limit"], monthly["base"], monthly["carried"]), (0.0, 5000, -5000))

    async def test_starts_with_the_next_month(self):
        await self.set_limits(monthly="5000", category=FOOD)
        self.now = datetime(2026, 11, 3, 9, 0, tzinfo=IST).astimezone(timezone.utc)
        summary = await self.rollover(FOOD, True)
        self.now = NOW
        await self.seed("1000", OCTOBER)
        food = await self.food()
        self.assertTrue(food["rollover"])
        self.assertEqual((food["monthly"]["limit"], food["monthly"]["base"], food["monthly"]["carried"]), (5000, None, None))
        self.assertTrue(summary["categories"][0]["rollover"])

    async def test_overall_limit_rolls_over(self):
        await self.set_limits(weekly="2000", monthly="5000")
        await self.enable_since("All", SEP_15)
        await self.seed("3000", OCTOBER, category="Shopping")
        await self.seed("1200", OCTOBER)
        await self.seed("100", OCTOBER, category="Self Transfer")  # not spending
        summary = await self.summary()
        self.assertTrue(summary["rollover"])
        self.assertEqual((summary["monthly"]["limit"], summary["monthly"]["base"], summary["monthly"]["carried"]),
                         (5800, 5000, 800))
        self.assertEqual((summary["weekly"]["limit"], summary["weekly"]["base"]), (2000, None))

    # ---------- Alerts ----------

    async def test_alerts_compare_with_the_rollover_limit_and_key_on_the_base(self):
        await self.set_limits(monthly="5000")
        await self.enable_since("All", SEP_15)
        await self.seed("4200", OCTOBER)
        first = await self.seed("5500", NOVEMBER)
        await limits.check_spend_limits(first)
        self.assertEqual(await self.notifications("spend_limit"), [])
        warnings = await self.notifications("spend_warning")
        self.assertEqual(len(warnings), 1)
        self.assertIn(":5000.00:", warnings[0].dedupe_key)
        self.assertIn("₹5,800.00", warnings[0].body)
        second = await self.seed("400", NOVEMBER + timedelta(hours=1))
        await limits.check_spend_limits(second)
        reached = await self.notifications("spend_limit")
        self.assertEqual(len(reached), 1)
        self.assertTrue(reached[0].dedupe_key.endswith(":5000.00"), reached[0].dedupe_key)
        self.assertIn("₹5,900.00 of ₹5,800.00", reached[0].body)
        alerts = await self.rows(SpendAlert)
        self.assertEqual([(a.limit_amount, a.status) for a in alerts], [(Decimal("5000.00"), "sent")])
        self.assertEqual(len(self.google.sent), 1)
        self.assertIn("₹5,800.00", body_text(self.google.sent[0]))
        self.assertTrue((await self.summary())["monthly"]["alerted"])
        # A late October SMS lowers the carry; a November edit checks again: still one alert.
        late = await self.seed("300", OCTOBER + timedelta(days=5))
        await limits.check_spend_limits(late)
        response = await self.client.patch(f"/api/v1/transactions/{second}", json={"amount": "450"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(await self.notifications("spend_limit")), 1)
        self.assertEqual(len(await self.rows(SpendAlert)), 1)
        self.assertEqual(len(self.google.sent), 1)
        self.assertEqual((await self.summary())["monthly"]["limit"], 5500)

    async def test_a_zero_rollover_limit_alerts_once(self):
        await self.set_limits(monthly="5000", category=FOOD)
        await self.enable_since(FOOD, SEP_15)
        await self.seed("12000", OCTOBER)
        first = await self.seed("100", NOVEMBER)
        await limits.check_spend_limits(first)
        second = await self.seed("50", NOVEMBER + timedelta(hours=1))
        await limits.check_spend_limits(second)
        reached = await self.notifications("spend_limit")
        self.assertEqual(len(reached), 1)
        self.assertIn("₹100.00 of ₹0.00", reached[0].body)
        self.assertTrue(reached[0].body.endswith("100%"), reached[0].body)
        self.assertEqual(reached[0].dedupe_key, f"spend_limit:{FOOD}:monthly:2026-11-01:5000.00")
        self.assertEqual(await self.notifications("spend_warning"), [])
        self.assertTrue((await self.food())["monthly"]["alerted"])

    # ---------- Turning it on and off ----------

    async def test_off_and_cleanup(self):
        await self.set_limits(monthly="5000", category=FOOD)
        await self.rollover(FOOD, True)
        first_enabled = (await self.rows(LimitRollover))[0].enabled_at
        self.now = NOW + timedelta(days=1)
        await self.rollover(FOOD, True)  # again: the first enabled_at stays
        rows = await self.rows(LimitRollover)
        self.assertEqual([(r.category, r.enabled_at) for r in rows], [(FOOD, first_enabled)])
        summary = await self.rollover(FOOD, False)
        self.assertFalse(summary["categories"][0]["rollover"])
        self.assertEqual(summary["categories"][0]["monthly"]["limit"], 5000)
        self.assertEqual(await self.rows(LimitRollover), [])
        await self.rollover(FOOD, False)  # already off
        # Removing the category limit turns it off.
        await self.rollover("food & dining", True)
        await self.set_limits(category=FOOD)
        self.assertEqual(await self.rows(LimitRollover), [])
        # Saving without a monthly amount turns it off; overall and category alike.
        await self.set_limits(monthly="5000", category=FOOD)
        await self.set_limits(monthly="9000")
        await self.rollover(FOOD, True)
        await self.rollover("All", True)
        await self.set_limits(weekly="1000", category=FOOD)
        self.assertEqual([r.category for r in await self.rows(LimitRollover)], ["All"])
        summary = await self.set_limits(weekly="2000")
        self.assertEqual(await self.rows(LimitRollover), [])
        self.assertFalse(summary["rollover"])

    async def test_route_errors(self):
        refused = await self.rollover(None, True, 422)
        self.assertEqual(self.detail(refused), "Set a monthly limit first.")
        await self.set_limits(weekly="1000", category=FOOD)
        self.assertEqual(self.detail(await self.rollover(FOOD, True, 422)), "Set a monthly limit first.")
        self.assertEqual(self.detail(await self.rollover("Salary", True, 422)), "Pick a spending category, or All.")
        self.assertEqual(self.detail(await self.rollover("Nope", False, 422)), "Pick a spending category, or All.")
        await self.rollover("All", False)
        await self.set_limits(monthly="5000")
        summary = await self.rollover(None, True)
        self.assertTrue(summary["rollover"])
        self.assertEqual([r.category for r in await self.rows(LimitRollover)], ["All"])
        await self.rollover("All", True, 403, headers=BAD_ORIGIN)
        app.dependency_overrides.clear()
        await self.rollover("All", False, 401)

    # ---------- Pay periods ----------

    async def test_pay_periods_carry_from_the_previous_pay_period(self):
        async with AsyncSessionLocal() as db:
            await settings_store.save(db, self.owner.id, {"periods.payday": True})
        await self.seed("80000", datetime(2026, 9, 30, 10, 0, tzinfo=IST), category="Salary", kind="CREDIT")
        await self.seed("80000", datetime(2026, 10, 31, 10, 0, tzinfo=IST), category="Salary", kind="CREDIT")
        await self.set_limits(monthly="5000", category=FOOD)
        await self.enable_since(FOOD, SEP_15)
        await self.seed("2000", datetime(2026, 9, 30, 20, 0, tzinfo=IST))  # the previous pay period
        await self.seed("2200", OCTOBER)
        await self.seed("1000", datetime(2026, 10, 31, 20, 0, tzinfo=IST))  # this pay period
        monthly = (await self.food())["monthly"]
        self.assertEqual((monthly["start"], monthly["limit"], monthly["carried"], monthly["spent"]),
                         ("2026-10-31", 5800, 800, 1000))

    async def test_no_previous_pay_period_means_no_carry(self):
        async with AsyncSessionLocal() as db:
            await settings_store.save(db, self.owner.id, {"periods.payday": True})
        await self.seed("80000", datetime(2026, 10, 31, 10, 0, tzinfo=IST), category="Salary", kind="CREDIT")
        await self.set_limits(monthly="5000", category=FOOD)
        await self.enable_since(FOOD, SEP_15)
        await self.seed("4200", OCTOBER)
        food = await self.food()
        self.assertTrue(food["rollover"])
        self.assertEqual((food["monthly"]["limit"], food["monthly"]["base"]), (5000, None))
        nov = await self.seed("5100", NOVEMBER)
        await limits.check_spend_limits(nov)
        self.assertEqual(len(await self.notifications("spend_limit")), 1)

    # ---------- Cost ----------

    async def test_summary_statements_are_the_same_with_rollover(self):
        await self.set_limits(weekly="2000", monthly="5000")
        await self.set_limits(monthly="5000", category=FOOD)
        await self.set_limits(monthly="3000", category="Shopping")
        await self.seed("4200", OCTOBER)

        async def count():
            statements = []

            def record(conn, cursor, statement, *args):
                statements.append(statement)

            async with AsyncSessionLocal() as db:
                event.listen(engine.sync_engine, "before_cursor_execute", record)
                try:
                    rules = await limits.load_rules(db, self.owner.id)
                    summary = await limits.summarize(db, self.owner, rules)
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", record)
            return len(statements), summary

        without, _ = await count()
        await self.enable_since("All", SEP_15)
        await self.enable_since(FOOD, SEP_15)
        with_rollover, summary = await count()
        self.assertEqual((without, with_rollover), (3, 3))
        self.assertEqual((summary.monthly.limit, summary.categories[0].monthly.limit), (5800, 5800))
