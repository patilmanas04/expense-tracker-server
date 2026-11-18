"""Payment alerts (payment_alerts.py): the large and unusual rules, their edges, the history they read, what's
skipped, one notification per payment, and end to end through the webhook and the app's routes.
Isolated in-memory SQLite; email stays off (no Gmail keys); Gemini stays off."""
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
from datetime import timedelta
from unittest.mock import patch

import payment_alerts
import post_save
from database import AsyncSessionLocal
from test_post_save import MINUTE, NOW, ORIGIN, PostSaveTestCase

DAY = timedelta(days=1)


def upi(amount, merchant, ref):
    return f"Sent Rs.{amount}\nFrom HDFC Bank A/C *4821\nTo {merchant}\nOn 21/10/26\nRef {ref}"


def vpa_debit(amount, vpa, ref):
    return (f"Rs.{amount} debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA {vpa} (UPI Ref No {ref}). "
            "Not you? Call 18002586161")


class PaymentAlertTestCase(PostSaveTestCase):
    async def fire(self, transaction_id, cause="sms"):
        await post_save.run(transaction_id, cause)

    async def alerts(self):
        return await self.notifications("payment_alert")

    async def alert_for(self, transaction_id):
        return [n for n in await self.alerts() if n.dedupe_key == f"payment_alert:{transaction_id}"]

    async def history(self, merchant, *amounts, start=10):
        """Earlier payments at `merchant`, `start`, `start + 10`, … days before NOW."""
        for i, amount in enumerate(amounts):
            await self.seed(merchant, amount, NOW - (start + 10 * i) * DAY)

    async def body_of(self, transaction_id):
        [alert] = await self.alert_for(transaction_id)
        return alert.title, alert.body


class LargeTests(PaymentAlertTestCase):
    async def test_off_by_default(self):
        await self.fire(await self.seed("Croma", 8000))
        self.assertEqual(await self.alerts(), [])

    async def test_cuj2_a_large_payment(self):
        await self.settings(alerts__large_amount=5000)
        txn_id = await self.seed("Croma", 8000)
        await self.fire(txn_id)
        [alert] = await self.alerts()
        self.assertEqual((alert.kind, alert.title, alert.body, alert.link, alert.dedupe_key, alert.user_id), (
            "payment_alert", "Large payment", "₹8,000.00 at Croma", f"/activity?txn={txn_id}",
            f"payment_alert:{txn_id}", self.owner.id))

    async def test_equal_is_not_over(self):
        await self.settings(alerts__large_amount=5000)
        equal, over = await self.seed("Croma", 5000), await self.seed("Croma", "5000.01")
        await self.fire(equal)
        await self.fire(over)
        self.assertEqual(await self.alert_for(equal), [])
        self.assertEqual(await self.body_of(over), ("Large payment", "₹5,000.01 at Croma"))

    async def test_no_merchant(self):
        await self.settings(alerts__large_amount=5000)
        for merchant in (None, "Unknown", "   "):
            with self.subTest(merchant=merchant):
                txn_id = await self.seed(merchant, 8000)
                await self.fire(txn_id)
                self.assertEqual(await self.body_of(txn_id), ("Large payment", "₹8,000.00 payment"))

    async def test_the_name_is_tidied(self):
        await self.settings(alerts__large_amount=5000)
        txn_id = await self.seed("  Croma   Retail ", 123456)
        await self.fire(txn_id)
        self.assertEqual(await self.body_of(txn_id), ("Large payment", "₹1,23,456.00 at Croma Retail"))


class UnusualTests(PaymentAlertTestCase):
    async def test_cuj1_an_unusual_order(self):
        await self.history("Swiggy", 300, 350, 400)
        txn_id = await self.seed("Swiggy", 3200)
        await self.fire(txn_id)
        self.assertEqual(await self.body_of(txn_id), (
            "Unusual payment", "₹3,200.00 at Swiggy — about 9× what you usually spend there (₹350.00)"))

    async def test_median_of_an_even_count(self):
        await self.history("Swiggy", 200, 300, 400, 500)
        txn_id = await self.seed("Swiggy", 1050)
        await self.fire(txn_id)
        self.assertEqual(await self.body_of(txn_id), (
            "Unusual payment", "₹1,050.00 at Swiggy — about 3× what you usually spend there (₹350.00)"))

    async def test_the_multiple_rounds_half_up(self):
        await self.history("Swiggy", 200, 200, 200)
        txn_id = await self.seed("Swiggy", 700)  # 3.5×
        await self.fire(txn_id)
        self.assertIn("about 4×", (await self.body_of(txn_id))[1])

    async def test_exactly_three_times(self):
        await self.history("Swiggy", 200, 200, 200)
        exact, under = await self.seed("Swiggy", 600), await self.seed("Swiggy", "599.99")  # same moment: not history
        await self.fire(exact)
        await self.fire(under)
        self.assertEqual(await self.body_of(exact), (
            "Unusual payment", "₹600.00 at Swiggy — about 3× what you usually spend there (₹200.00)"))
        self.assertEqual(await self.alert_for(under), [])

    async def test_the_floor(self):
        await self.history("Chai Point", 100, 100, 100)
        under, floor = await self.seed("Chai Point", "499.99"), await self.seed("Chai Point", 500)
        await self.fire(under)
        await self.fire(floor)
        self.assertEqual(await self.alert_for(under), [])
        self.assertEqual(await self.body_of(floor), (
            "Unusual payment", "₹500.00 at Chai Point — about 5× what you usually spend there (₹100.00)"))

    async def test_needs_three_earlier_payments(self):
        await self.history("Swiggy", 300, 350)
        txn_id = await self.seed("Swiggy", 3200)
        await self.fire(txn_id)
        self.assertEqual(await self.alerts(), [])

    async def test_the_180_day_window(self):
        await self.history("Inside", 300, 300)
        await self.seed("Inside", 300, NOW - 180 * DAY)
        await self.history("Outside", 300, 300)
        await self.seed("Outside", 300, NOW - 180 * DAY - timedelta(seconds=1))
        inside, outside = await self.seed("Inside", 3000), await self.seed("Outside", 3000)
        await self.fire(inside)
        await self.fire(outside)
        self.assertEqual(len(await self.alert_for(inside)), 1)
        self.assertEqual(await self.alert_for(outside), [])

    async def test_the_same_moment_and_later_payments_are_not_history(self):
        await self.history("Swiggy", 300, 300)
        await self.seed("Swiggy", 300, NOW)
        await self.seed("Swiggy", 300, NOW + MINUTE)
        txn_id = await self.seed("Swiggy", 3200)
        await self.fire(txn_id)
        self.assertEqual(await self.alerts(), [])

    async def test_history_reads_only_counted_inr_debits(self):
        excluded = {
            "Credit Card Payment": {"category": "Credit Card Payment"},
            "Self Transfer": {"category": "Self Transfer"},
            "Investments & Finance": {"category": "Investments & Finance"},
            "unparsed": {"is_parsed": False},
            "USD": {"currency": "USD"},
            "CREDIT": {"type": "CREDIT"},
            "other user": {"user_id": self.other.id},
            "held out": {},
        }
        for i, (label, fields) in enumerate(excluded.items()):
            with self.subTest(label):
                merchant = f"Shop {i}"
                first = await self.seed(merchant, 300, NOW - 30 * DAY)
                await self.seed(merchant, 300, NOW - 20 * DAY)
                odd = await self.seed(merchant, 300, NOW - 10 * DAY, **fields)
                if label == "held out":
                    await self.flag(odd, first)
                txn_id = await self.seed(merchant, 3000)
                await self.fire(txn_id)
                self.assertEqual(await self.alert_for(txn_id), [])

    async def test_the_owners_and_unassigned_rows_count(self):
        await self.seed("Swiggy", 300, NOW - 30 * DAY)
        await self.seed("Swiggy", 300, NOW - 20 * DAY, user_id=self.owner.id)
        await self.seed("Swiggy", 300, NOW - 10 * DAY, parsed_by="manual", user_id=self.owner.id)
        txn_id = await self.seed("Swiggy", 3000, user_id=self.owner.id)
        await self.fire(txn_id)
        self.assertEqual(len(await self.alert_for(txn_id)), 1)

    async def test_a_held_out_row_does_not_move_the_median(self):
        a = await self.seed("Swiggy", 100, NOW - 40 * DAY)
        await self.seed("Swiggy", 100, NOW - 30 * DAY)
        await self.seed("Swiggy", 300, NOW - 20 * DAY)
        await self.seed("Swiggy", 300, NOW - 15 * DAY)
        held = await self.seed("Swiggy", 5000, NOW - 10 * DAY)  # counted, it would make the median ₹300
        await self.flag(held, a)
        txn_id = await self.seed("Swiggy", 1200)
        await self.fire(txn_id)
        self.assertEqual(await self.body_of(txn_id), (
            "Unusual payment", "₹1,200.00 at Swiggy — about 6× what you usually spend there (₹200.00)"))

    async def test_merchant_names_share_history_by_key(self):
        await self.seed("Swiggy", 300, NOW - 30 * DAY)
        await self.seed("SWIGGY", 350, NOW - 20 * DAY)
        await self.seed(" swiggy  ", 400, NOW - 10 * DAY)
        same, different = await self.seed("Swiggy", 3200), await self.seed("Swiggy Instamart", 3200)
        await self.fire(same)
        await self.fire(different)
        self.assertEqual(await self.body_of(same), (
            "Unusual payment", "₹3,200.00 at Swiggy — about 9× what you usually spend there (₹350.00)"))
        self.assertEqual(await self.alert_for(different), [])


class BothAndSwitchTests(PaymentAlertTestCase):
    async def test_both_rules_make_one_notification(self):
        await self.settings(alerts__large_amount=3000)
        await self.history("Swiggy", 300, 350, 400)
        txn_id = await self.seed("Swiggy", 3200)
        await self.fire(txn_id)
        [alert] = await self.alerts()
        self.assertEqual((alert.title, alert.body), (
            "Large and unusual payment", "₹3,200.00 at Swiggy — about 9× what you usually spend there (₹350.00)"))

    async def test_unusual_off(self):
        await self.settings(alerts__large_amount=3000, alerts__unusual=False)
        await self.history("Swiggy", 300, 350, 400)
        txn_id = await self.seed("Swiggy", 3200)
        await self.fire(txn_id)
        self.assertEqual(await self.body_of(txn_id), ("Large payment", "₹3,200.00 at Swiggy"))

    async def statements_for_check(self, txn_id) -> int:
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, txn_id, "sms")
            return await self.count_statements(lambda: payment_alerts.check(ctx))

    async def test_both_off_reads_nothing(self):
        await self.history("Swiggy", 300, 350, 400)
        txn_id = await self.seed("Swiggy", 3200)
        await self.settings(alerts__unusual=False)
        self.assertEqual(await self.statements_for_check(txn_id), 0)
        await self.fire(txn_id)
        self.assertEqual(await self.alerts(), [])
        await self.settings(alerts__unusual=True)
        small = await self.seed("Swiggy", 499)
        self.assertEqual(await self.statements_for_check(small), 0)  # under the floor: no history read
        self.assertEqual(await self.statements_for_check(await self.seed("Swiggy", 900)), 1)  # one history read


class SkipTests(PaymentAlertTestCase):
    async def test_payments_that_are_not_spending(self):
        await self.settings(alerts__large_amount=1000)
        cases = {
            "Investments & Finance": {"category": "Investments & Finance"},
            "Credit Card Payment": {"category": "Credit Card Payment"},
            "Self Transfer": {"category": "Self Transfer"},
            "CREDIT": {"type": "CREDIT", "category": "Salary"},
            "USD": {"currency": "USD"},
            "unparsed": {"is_parsed": False},
        }
        for label, fields in cases.items():
            with self.subTest(label):
                await self.fire(await self.seed("Croma", 8000, **fields))
        self.assertEqual(await self.alerts(), [])

    async def test_uncategorized_and_no_category_count(self):
        await self.settings(alerts__large_amount=1000)
        for category in ("Uncategorized", None):
            with self.subTest(category=category):
                txn_id = await self.seed("Croma", 8000, category=category)
                await self.fire(txn_id)
                self.assertEqual(len(await self.alert_for(txn_id)), 1)

    async def test_held_out_and_edit(self):
        await self.settings(alerts__large_amount=1000)
        a = await self.seed("Croma", 8000, NOW - MINUTE)
        b = await self.seed("Croma", 8000)
        await self.flag(b, a)
        await self.fire(b)
        await self.fire(a, "edit")
        self.assertEqual(await self.alerts(), [])

    async def test_once_per_payment(self):
        await self.settings(alerts__large_amount=1000)
        txn_id = await self.seed("Croma", 8000)
        for cause in ("sms", "ai_retry", "restored", "manual"):
            await self.fire(txn_id, cause)
        self.assertEqual(len(await self.alerts()), 1)


class EndToEndTests(PaymentAlertTestCase):
    async def test_webhook_hand_add_and_edit(self):
        await self.settings(alerts__large_amount=5000)
        sms = await self.sms(vpa_debit("8000.00", "croma@ybl", "629400200001"))
        [alert] = await self.alerts()
        self.assertEqual((alert.title, alert.link), ("Large payment", f"/activity?txn={sms['id']}"))
        self.assertTrue(alert.body.startswith("₹8,000.00 at "), alert.body)
        small = await self.sms(vpa_debit("80.00", "croma@ybl", "629400200002"))
        self.assertEqual(await self.alert_for(small["id"]), [])

        hand = await self.manual(7000, "Furniture", NOW - 2 * DAY)
        self.assertEqual(await self.body_of(hand["id"]), ("Large payment", "₹7,000.00 at Furniture"))
        response = await self.client.patch(f"/api/v1/transactions/{small['id']}", json={"amount": "9000"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.alert_for(small["id"]), [])  # edits don't alert
        self.assertEqual(len(await self.alerts()), 2)

    async def test_a_held_out_sms_alerts_once_it_counts(self):
        await self.settings(alerts__large_amount=5000)
        first = await self.sms(upi("6000.00", "SWIGGY", "629400300001"))
        second = await self.sms(vpa_debit("6000.00", "swiggy@icici", "629400300001"), NOW + MINUTE)
        self.assertEqual(second["duplicate_of"], first["id"])
        self.assertEqual([n.dedupe_key for n in await self.alerts()], [f"payment_alert:{first['id']}"])
        response = await self.client.post(f"/api/v1/transactions/{second['id']}/not-duplicate", headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(await self.alert_for(second["id"])), 1)
        self.assertEqual(len(await self.alerts()), 2)

    async def test_a_failing_payment_alert_never_stops_limit_alerts(self):
        await self.client.put("/api/v1/limits", json={"monthly_limit": 1}, headers=ORIGIN)
        with patch("payment_alerts.usual_amount", side_effect=RuntimeError("bug")), \
                self.assertLogs("post_save", "ERROR") as logs:
            await self.sms(upi("600.00", "SWIGGY", "629400400001"))
        self.assertIn("Post-save check payment_alerts failed", logs.output[0])
        self.assertEqual([n.kind for n in await self.notifications()], ["spend_limit"])


if __name__ == "__main__":
    unittest.main()
