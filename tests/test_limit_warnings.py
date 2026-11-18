"""Near-limit warnings (limits.py, limits.warn_percent in settings_store.py): one in-app spend_warning per limit,
period, limit amount and level when spending is at or above the level and still under the limit; never emailed.
Isolated in-memory SQLite; email stays off (no Gmail keys); Gemini is faked."""
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
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import limits
import post_save
from database import AsyncSessionLocal
from models import UserSetting
from test_post_save import NOW, ORIGIN, SWIGGY_UPI, PostSaveTestCase

IST = timezone(timedelta(hours=5, minutes=30))
EARLIER = datetime(2026, 10, 5, 12, 0, tzinfo=IST)  # Mon 5 Oct: this month, an earlier week
LAST_MONTH = datetime(2026, 9, 30, 12, 0, tzinfo=IST)
FOOD = "Food & Dining"
WARN_ERROR = "limits.warn_percent: Choose 50% to 95% in steps of 5, or Off."


class WarningTestCase(PostSaveTestCase):
    async def limit(self, category=None, weekly=None, monthly=None) -> dict:
        body = {"weekly_limit": weekly, "monthly_limit": monthly}
        if category:
            body["category"] = category
        response = await self.client.put("/api/v1/limits", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def merchant_limit(self, name, weekly=None, monthly=None, category=FOOD) -> str:
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN, json={
            "merchant": name, "weekly_limit": weekly, "monthly_limit": monthly, "category": category})
        self.assertEqual(response.status_code, 200, response.text)
        return next(m["id"] for m in response.json()["merchants"] if m["merchant"] == name)

    async def pay(self, amount, merchant="Swiggy", when=NOW, category=FOOD, currency="INR", cause="sms"):
        txn_id = await self.seed(merchant, amount, when, category=category, currency=currency)
        await post_save.run(txn_id, cause)
        return txn_id

    async def warnings(self):
        return await self.notifications("spend_warning")

    async def reached(self):
        return await self.notifications("spend_limit")


class CategoryWarningTests(WarningTestCase):
    async def test_default_warns_at_80_percent(self):  # CUJ 1
        await self.limit(FOOD, monthly=5000)
        await self.pay(3700, when=EARLIER)
        self.assertEqual(await self.warnings(), [])
        await self.pay(450)
        [warning] = await self.warnings()
        self.assertEqual(warning.title, "80% of your monthly Food & Dining limit used")
        self.assertEqual(warning.body, "₹4,150.00 of ₹5,000.00 on Food & Dining this month (Thu 1 Oct – Sat 31 Oct) · 83%")
        self.assertEqual(warning.link, "/settings/limits")
        self.assertEqual(warning.dedupe_key, "spend_warn:Food & Dining:monthly:2026-10-01:5000.00:80")
        self.assertEqual(await self.reached(), [])

    async def test_boundary_is_inclusive_and_below_is_silent(self):
        await self.limit(FOOD, monthly=5000)
        await self.pay("3999.99")
        self.assertEqual(await self.warnings(), [])
        await self.pay("0.01")
        [warning] = await self.warnings()
        self.assertTrue(warning.body.startswith("₹4,000.00 of ₹5,000.00"), warning.body)

    async def test_once_per_period_limit_and_level(self):
        await self.limit(FOOD, monthly=5000)
        await self.pay(4100)
        self.assertEqual(len(await self.warnings()), 1)
        await self.pay(100)
        self.assertEqual(len(await self.warnings()), 1)
        await self.limit(FOOD, monthly=5100)
        await self.pay(10)
        first, second = await self.warnings()
        self.assertTrue(first.dedupe_key.endswith(":5000.00:80"), first.dedupe_key)
        self.assertTrue(second.dedupe_key.endswith(":5100.00:80"), second.dedupe_key)

    async def test_level_lowered_mid_month(self):  # CUJ 5
        await self.settings(limits__warn_percent=90)
        await self.limit(FOOD, monthly=5000)
        await self.pay(4250)
        self.assertEqual(await self.warnings(), [])
        await self.settings(limits__warn_percent=80)
        await self.pay(1)
        [warning] = await self.warnings()
        self.assertTrue(warning.dedupe_key.endswith(":5000.00:80"), warning.dedupe_key)
        self.assertTrue(warning.title.startswith("80%"), warning.title)

    async def test_jump_past_sends_only_the_reached_alert(self):  # CUJ 3
        await self.limit(FOOD, monthly=5000)
        await self.pay(3500)
        await self.pay(2000)
        [crossed] = await self.reached()
        self.assertEqual(crossed.title, "Monthly Food & Dining limit crossed")
        self.assertEqual(await self.warnings(), [])

    async def test_off_sends_no_warnings_but_reached_alerts_continue(self):  # CUJ 4
        await self.settings(limits__warn_percent=None)
        await self.limit(FOOD, monthly=1000)
        await self.pay(900)
        self.assertEqual(await self.warnings(), [])
        await self.pay(200)
        self.assertEqual(await self.warnings(), [])
        self.assertEqual(len(await self.reached()), 1)

    async def test_category_limit_only_for_the_payments_category(self):
        await self.limit(FOOD, monthly=1000)
        await self.seed(amount=900)  # no check run
        await self.pay(10, category="Shopping")
        self.assertEqual(await self.warnings(), [])  # a Shopping payment doesn't run the Food check
        await self.pay(10)
        [warning] = await self.warnings()
        self.assertEqual(warning.title, "80% of your monthly Food & Dining limit used")

    async def test_edit_that_lowers_spending_warns_once(self):
        await self.limit(FOOD, monthly=1000)
        txn_id = await self.pay(1200)
        self.assertEqual(len(await self.reached()), 1)
        self.assertEqual(await self.warnings(), [])
        for amount in ("900", "950"):
            response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json={"amount": amount}, headers=ORIGIN)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(await self.warnings()), 1, amount)
        self.assertEqual(len(await self.reached()), 1)

    async def test_late_sms_for_a_past_month_never_warns(self):
        await self.limit(FOOD, monthly=1000)
        await self.pay(900, when=LAST_MONTH)
        self.assertEqual(await self.warnings(), [])
        await self.seed(amount=850)  # this month, no check run
        await self.pay(1, when=LAST_MONTH)  # only the payment's own period is checked
        self.assertEqual(await self.warnings(), [])

    async def test_merges_and_rule_applies_warn_too(self):
        await self.limit(FOOD, monthly=1000)
        txn_id = await self.seed(amount=850)
        await limits.check_spend_limits(txn_id)
        self.assertEqual(len(await self.warnings()), 1)

    async def test_warnings_never_email(self):
        await self.limit(FOOD, monthly=1000)
        with patch("limits.mailer.configured", return_value=True), \
                patch("limits.mailer.send_email", new=AsyncMock()) as send_email, \
                patch("limits.mailer.recipient", return_value="me@example.com"):
            await self.pay(850)
            self.assertEqual(len(await self.warnings()), 1)
            send_email.assert_not_awaited()
            await self.pay(200)
            self.assertEqual(len(await self.reached()), 1)
            send_email.assert_awaited_once()
            self.assertIn("Monthly Food & Dining limit crossed", send_email.await_args.args[1])

    async def test_held_out_duplicate_does_not_warn(self):
        await self.limit(FOOD, monthly=1000)
        original = await self.seed(amount=500)
        flagged = await self.seed(amount=500)
        await self.flag(flagged, original)
        await post_save.run(flagged, "sms")
        self.assertEqual(await self.warnings(), [])

    async def test_summary_alerted_stays_false_after_a_warning(self):
        await self.limit(FOOD, monthly=1000)
        await self.pay(850)
        self.assertEqual(len(await self.warnings()), 1)
        response = await self.client.get("/api/v1/limits")
        [food] = response.json()["categories"]
        self.assertEqual(food["category"], FOOD)
        self.assertIs(food["monthly"]["alerted"], False)

    async def test_webhook_end_to_end(self):
        await self.limit(FOOD, monthly=600)
        txn = await self.sms(SWIGGY_UPI)
        self.assertEqual(txn["category"], FOOD)
        [warning] = await self.warnings()
        self.assertEqual(warning.title, "80% of your monthly Food & Dining limit used")
        self.assertTrue(warning.body.startswith("₹500.00 of ₹600.00 on Food & Dining this month"), warning.body)


class OverallWarningTests(WarningTestCase):
    async def test_overall_weekly_and_monthly(self):
        await self.limit(weekly=1000, monthly=10000)
        await self.pay(850, category="Shopping")
        [warning] = await self.warnings()
        self.assertEqual(warning.title, "80% of your weekly spend limit used")
        self.assertEqual(warning.body, "₹850.00 of ₹1,000.00 this week (Mon 19 Oct – Sun 25 Oct) · 85%")
        self.assertEqual(warning.dedupe_key, "spend_warn:weekly:2026-10-19:1000.00:80")

    async def test_overall_limit_ignores_other_currency(self):
        await self.limit(monthly=1000)
        await self.pay(900, currency="USD")
        self.assertEqual(await self.warnings(), [])


class MerchantWarningTests(WarningTestCase):
    async def test_merchant_limit_warns_with_its_id_in_the_key(self):
        limit_id = await self.merchant_limit("Swiggy", monthly=1000)
        await self.merchant_limit("SWIGGY", monthly=1000)
        await self.pay(850, merchant="Swiggy", category="Shopping")  # no Food limit either way
        [warning] = await self.warnings()
        self.assertEqual(warning.title, "80% of your monthly Swiggy limit used")
        self.assertEqual(warning.body, "₹850.00 of ₹1,000.00 at Swiggy this month (Thu 1 Oct – Sat 31 Oct) · 85%")
        self.assertEqual(warning.dedupe_key, f"spend_warn:merchant:{limit_id}:monthly:2026-10-01:1000.00:80")

    async def test_long_merchant_name_keeps_key_and_title_short(self):
        name = " ".join(["Abcdefg"] * 32)
        self.assertEqual(len(name), 255)
        await self.merchant_limit(name, monthly=1000)
        await self.pay(850, merchant=name, category="Shopping")
        [warning] = await self.warnings()
        self.assertLessEqual(len(warning.dedupe_key), 200)
        self.assertLessEqual(len(warning.title), 200)
        self.assertIn("…", warning.title)
        self.assertIn(f" at {name} this month", warning.body)


class WarnSettingTests(WarningTestCase):
    async def stored(self) -> list[tuple[str, str]]:
        async with AsyncSessionLocal() as db:
            return [(row.key, row.value) for row in await db.scalars(select(UserSetting))]

    async def test_default_is_80(self):
        response = await self.client.get("/api/v1/settings")
        self.assertEqual(response.json(), {"alerts.large_amount": None, "alerts.unusual": True,
                                           "limits.warn_percent": 80, "alerts.low_balance": None})
        self.assertEqual(list(response.json()), ["alerts.large_amount", "alerts.unusual", "limits.warn_percent",
                                                 "alerts.low_balance"])

    async def test_invalid_levels_write_nothing(self):
        for value in (45, 82, 100, 0, 50.5, True, "80", 96, 49, -80, [80]):
            with self.subTest(value=value):
                response = await self.client.put("/api/v1/settings", json={"limits.warn_percent": value}, headers=ORIGIN)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json(), {"detail": WARN_ERROR})
                self.assertEqual(await self.stored(), [])

    async def test_valid_levels_and_off(self):
        for value, expected in ((50, 50), (95, 95), (80.0, 80), (85, 85)):
            with self.subTest(value=value):
                data = await self.settings(limits__warn_percent=value)
                self.assertEqual(data["limits.warn_percent"], expected)
                self.assertIs(type(data["limits.warn_percent"]), int)
        self.assertEqual(await self.stored(), [("limits.warn_percent", "85")])
        self.assertIsNone((await self.settings(limits__warn_percent=None))["limits.warn_percent"])
        self.assertIsNone((await self.client.get("/api/v1/settings")).json()["limits.warn_percent"])
        self.assertEqual(await self.stored(), [("limits.warn_percent", "null")])

    async def test_other_whole_numbers_keep_step_one(self):
        self.assertEqual((await self.settings(alerts__large_amount=4999))["alerts.large_amount"], 4999)


if __name__ == "__main__":
    unittest.main()
