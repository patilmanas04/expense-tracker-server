"""Low-balance alert (low_balance.py, alerts.low_balance in settings_store.py): crossing the level, once per dip,
re-arming, the edges, what never alerts, "Not a duplicate" versus a delete, the query budget, the setting and the
registry, and end to end through the webhook and the app's routes.
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
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

import low_balance
import post_save
from auth import current_user
from database import AsyncSessionLocal
from main import app
from models import DUPLICATE_DISMISSED, DuplicateFlag, Transaction, UserSetting
from test_post_save import DMART, MINUTE, NOW, ORIGIN, PostSaveTestCase

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)

# Copied from tests/test_balance.py.
HDFC_CARD_SPEND = "Spent Rs.1,249 On HDFC Bank Card 9012 At AMAZON PAY INDIA On 2026-10-21:13:05:44.Not You? To Block+Reissue Call 18002586161/SMS BLOCK CC 9012 to 7308080808"
HDFC_CARD_BILL_PAID = "Rs.1249.00 debited from HDFC Bank A/c XX4821 on 21-10-26 towards HDFC Bank Credit Card XX9012. Ref 629400000555"

LEVEL_ERROR = "alerts.low_balance: Enter a whole number of rupees from 1 to 10,00,00,000, or turn it off."
BODY = "Below your ₹10,000 alert (HDFC and ICICI together)."


class LowBalanceTestCase(PostSaveTestCase):
    async def balance(self, amount, as_of: datetime = NOW - DAY) -> None:
        """Saves Balance left. Always with `as_of`: without it the server uses the real clock, which is after NOW,
        and every fixture would be before the balance."""
        response = await self.client.put("/api/v1/balance", json={"amount": str(amount), "as_of": as_of.isoformat()},
                                         headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)

    async def restart(self, amount) -> None:
        """Saves a new balance as of the latest fixture, so every earlier payment is already in it."""
        await self.balance(amount, NOW + self.counter * MINUTE)

    async def level(self, rupees) -> dict:
        return await self.settings(alerts__low_balance=rupees)

    async def add(self, amount, *, type="DEBIT", when=None, raw_message="fixture", account_ref=None, currency="INR",
                  is_parsed=True) -> uuid.UUID:
        self.counter += 1
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=type, amount=Decimal(str(amount)), currency=currency, merchant="Croma",
                              category="Shopping", is_parsed=is_parsed, parsed_by="regex", raw_message=raw_message,
                              account_ref=account_ref, message_hash=f"low-{self.counter}",
                              transaction_time=when or NOW + self.counter * MINUTE,
                              created_at=NOW + timedelta(seconds=self.counter), updated_at=NOW)
            db.add(txn)
            await db.commit()
            return txn.id

    async def pay(self, amount, cause="sms", **kw) -> uuid.UUID:
        txn_id = await self.add(amount, **kw)
        await post_save.run(txn_id, cause)
        return txn_id

    async def alerts(self):
        return await self.notifications("low_balance")

    async def titles(self) -> list[str]:
        return [n.title for n in await self.alerts()]


class RuleTests(LowBalanceTestCase):
    async def test_cuj1_dip_below(self):
        await self.balance(11000)
        await self.level(10000)
        txn_id = await self.pay(1800)  # 11,000 → 9,200
        [alert] = await self.alerts()
        self.assertEqual((alert.kind, alert.title, alert.body, alert.link, alert.dedupe_key, alert.user_id),
                         ("low_balance", "Balance low: ₹9,200.00 left", BODY, "/settings/balance",
                          f"low_balance:{txn_id}", self.owner.id))
        await post_save.run(txn_id, "sms")  # the same payment again: still one
        await self.pay(500)  # 9,200 → 8,700: already below
        self.assertEqual(len(await self.alerts()), 1)

    async def test_cuj2_rearmed_after_a_credit(self):
        await self.balance(11000)
        await self.level(10000)
        await self.pay(1800)
        await self.pay(500)  # 8,700
        await self.pay(85000, type="CREDIT")  # salary: 93,700
        await self.pay(83300)  # 10,400: still above
        self.assertEqual(len(await self.alerts()), 1)
        await self.pay(800)  # 10,400 → 9,600
        self.assertEqual(await self.titles(), ["Balance low: ₹9,200.00 left", "Balance low: ₹9,600.00 left"])

    async def test_landing_on_the_level_is_not_below(self):
        await self.balance(11000)
        await self.level(10000)
        await self.pay(1000)  # exactly 10,000
        self.assertEqual(await self.alerts(), [])
        await self.pay("0.01")  # from exactly the level counts as at or above it
        self.assertEqual(await self.titles(), ["Balance low: ₹9,999.99 left"])

    async def test_far_above_to_far_below(self):
        await self.balance(50000)
        await self.level(10000)
        await self.pay(48000)
        self.assertEqual(await self.titles(), ["Balance low: ₹2,000.00 left"])

    async def test_below_zero(self):
        await self.balance(1500)
        await self.level(1000)
        await self.pay(2000)
        [alert] = await self.alerts()
        self.assertEqual((alert.title, alert.body),
                         ("Balance low: -₹500.00 left", "Below your ₹1,000 alert (HDFC and ICICI together)."))

    async def test_saving_a_balance_below_the_level_never_alerts(self):
        await self.level(10000)
        await self.balance(5000)
        self.assertEqual(await self.alerts(), [])
        await self.pay(100)  # 5,000 → 4,900: already below
        self.assertEqual(await self.alerts(), [])

    async def test_changing_the_level_never_alerts(self):
        await self.balance(15000)
        await self.level(20000)
        await self.level(10000)
        self.assertEqual(await self.notifications(), [])

    async def test_off(self):
        self.assertIsNone((await self.client.get("/api/v1/settings")).json()["alerts.low_balance"])
        await self.balance(11000)
        await self.pay(1800)
        await self.level(10000)
        await self.level(None)
        await self.restart(11000)
        await self.pay(1800)
        self.assertEqual(await self.alerts(), [])

    async def test_payments_that_dont_move_the_balance(self):
        # Already below the level, so a check that counted these would see 9,000 + 1,800 ≥ 10,000 > 9,000.
        await self.balance(9000)
        await self.level(10000)
        cash = await self.pay(1800, account_ref="Cash")
        await self.pay(1800, raw_message=HDFC_CARD_SPEND)
        await self.pay(1800, currency="USD")
        await self.pay(1800, is_parsed=False)
        held = await self.add(1800)
        await self.flag(held, cash)
        await post_save.run(held, "sms")
        await self.pay(500, type="CREDIT")  # 9,500: a check that counted credits would see 10,000 before it
        self.assertEqual(await self.alerts(), [])
        # A card bill paid from the bank account does move it.
        await self.restart(11000)
        await self.pay(1800, raw_message=HDFC_CARD_BILL_PAID)
        self.assertEqual(await self.titles(), ["Balance low: ₹9,200.00 left"])

    async def test_each_payment_that_doesnt_move_the_balance(self):
        cases = {"cash": {"account_ref": "Cash"}, "card spend": {"raw_message": HDFC_CARD_SPEND},
                 "other currency": {"currency": "USD"}, "unreadable": {"is_parsed": False}}
        await self.level(10000)
        for name, kw in cases.items():
            with self.subTest(name):
                await self.restart(9000)
                await self.pay(1800, **kw)
                self.assertEqual(await self.alerts(), [])

    async def test_payment_at_or_before_the_saved_balance(self):
        # 9,500 is already below: a check that counted these would see 9,500 + 1,800 ≥ 10,000 > 9,500.
        await self.balance(9500, as_of=NOW)
        await self.level(10000)
        await self.pay(1800, when=NOW - MINUTE)
        await self.pay(1800, when=NOW)
        self.assertEqual(await self.alerts(), [])

    async def test_balance_in_another_currency(self):
        response = await self.client.put("/api/v1/balance", headers=ORIGIN, json={
            "amount": "11000", "currency": "USD", "as_of": (NOW - DAY).isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        await self.level(10000)
        await self.pay(1800, currency="USD")
        self.assertEqual(await self.alerts(), [])

    async def test_no_balance_saved(self):
        await self.level(10000)
        await self.pay(1800)
        self.assertEqual(await self.alerts(), [])

    async def test_two_payments_saved_together(self):
        """The accepted limit: both checks see the final balance, and neither payment alone spans the level."""
        await self.balance(10500)
        await self.level(10000)
        a = await self.add(600)
        b = await self.add(600)
        await post_save.run(a, "sms")
        await post_save.run(b, "sms")  # 9,300 now; each sees 9,900 before it
        self.assertEqual(await self.alerts(), [])


class CauseTests(LowBalanceTestCase):
    async def test_not_a_duplicate_on_a_crossing_debit_alerts(self):
        await self.balance(11000)
        await self.level(10000)
        x = await self.pay(800)  # 10,200
        y = await self.add(800)
        await self.flag(y, x)
        self.assertEqual(await self.alerts(), [])
        response = await self.client.post(f"/api/v1/transactions/{y}/not-duplicate", headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        [alert] = await self.alerts()
        self.assertEqual((alert.dedupe_key, alert.title), (f"low_balance:{y}", "Balance low: ₹9,400.00 left"))

    async def test_deleting_the_original_of_a_held_out_payment_does_not_alert_again(self):
        await self.balance(11000)
        await self.level(10000)
        x = await self.pay(1800)  # 9,200: alerts
        y = await self.add(1800)
        await self.flag(y, x)
        response = await self.client.delete(f"/api/v1/transactions/{x}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204)
        # y now counts in x's place: still 9,200, and the dip already alerted.
        self.assertEqual([n.dedupe_key for n in await self.alerts()], [f"low_balance:{x}"])

    async def test_restored_without_a_dismissed_flag_does_not_alert(self):
        await self.balance(11000)
        await self.level(10000)
        txn_id = await self.add(1800)
        await post_save.run(txn_id, "restored")
        self.assertEqual(await self.alerts(), [])
        await post_save.run(txn_id, "sms")  # the same payment does cross
        self.assertEqual(len(await self.alerts()), 1)

    async def test_edits_and_ai_retry_never_alert(self):
        await self.balance(11000)
        await self.level(10000)
        await self.pay(1800, cause="edit")
        await self.restart(11000)
        await self.pay(1800, cause="ai_retry")
        await self.restart(11000)
        txn_id = await self.pay(100)  # 10,900
        response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json={"amount": "1800.00"},
                                           headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)  # 9,200 now, from an edit
        self.assertEqual(await self.alerts(), [])

    async def test_registered_for_sms_manual_restored(self):
        [(check, causes)] = [(check, causes) for name, check, causes in post_save._CHECKS if name == "low_balance"]
        self.assertIs(check, low_balance.check)
        self.assertEqual(causes, {"sms", "manual", "restored"})
        names = [name for name, _, _ in post_save._CHECKS]
        self.assertEqual(names.index("low_balance"), names.index("payment_alerts") + 1)


class EndToEndTests(LowBalanceTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.balance(11000)
        await self.level(10000)

    async def test_end_to_end_sms(self):
        sms = await self.sms(DMART)  # ₹1,200
        [alert] = await self.alerts()
        self.assertEqual((alert.title, alert.dedupe_key), ("Balance low: ₹9,800.00 left", f"low_balance:{sms['id']}"))
        listed = (await self.client.get("/api/v1/notifications")).json()["items"]
        [item] = [n for n in listed if n["kind"] == "low_balance"]
        self.assertEqual((item["title"], item["body"], item["link"]),
                         ("Balance low: ₹9,800.00 left", BODY, "/settings/balance"))

    async def test_end_to_end_hand_added(self):
        response = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": "DEBIT", "amount": "1800", "merchant": "Chai stall", "transaction_time": NOW.isoformat(),
            "category": "Food & Dining", "paid_with": "cash"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(await self.alerts(), [])
        await self.manual(1800, "Croma")
        self.assertEqual(await self.titles(), ["Balance low: ₹9,200.00 left"])


class QueryBudgetTests(LowBalanceTestCase):
    async def statements(self, txn_id, cause="sms") -> int:
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, txn_id, cause)
            self.assertIsNotNone(ctx)
            return await self.count_statements(lambda: low_balance.check(ctx))

    async def test_query_budget(self):
        debit = await self.add(100)
        self.assertEqual(await self.statements(debit), 0)  # Off
        await self.level(10000)
        self.assertEqual(await self.statements(await self.add(100, type="CREDIT")), 0)
        self.assertEqual(await self.statements(debit), 1)  # no balance saved
        await self.balance(50000)
        self.assertEqual(await self.statements(debit), 2)  # the saved balance, then summarize
        restored = await self.add(100)
        async with AsyncSessionLocal() as db:
            db.add(DuplicateFlag(transaction_id=restored, original_id=debit, user_id=self.owner.id,
                                 reason="reference", status=DUPLICATE_DISMISSED, created_at=NOW, decided_at=NOW))
            await db.commit()
        self.assertEqual(await self.statements(restored, "restored"), 3)
        self.assertEqual(await self.alerts(), [])


class SettingTests(LowBalanceTestCase):
    async def stored(self) -> list[tuple[str, str]]:
        async with AsyncSessionLocal() as db:
            rows = await db.scalars(select(UserSetting).where(UserSetting.key == "alerts.low_balance"))
            return [(row.key, row.value) for row in rows]

    async def test_setting_validation(self):
        response = await self.client.get("/api/v1/settings")
        self.assertEqual(response.json(), {"alerts.large_amount": None, "alerts.unusual": True,
                                           "limits.warn_percent": 80, "alerts.low_balance": None,
                                           "periods.payday": False})
        self.assertEqual(list(response.json()), ["alerts.large_amount", "alerts.unusual", "limits.warn_percent",
                                                 "alerts.low_balance", "periods.payday"])
        for value in (0, 100000001, True, False, "5000", 5000.5, -1, [5000], {"rupees": 5000}):
            with self.subTest(value=value):
                response = await self.client.put("/api/v1/settings", json={"alerts.low_balance": value},
                                                 headers=ORIGIN)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json(), {"detail": LEVEL_ERROR})
        self.assertEqual(await self.stored(), [])  # nothing written
        for value, expected in ((1, 1), (100000000, 100000000), (5000.0, 5000), (None, None), (10000, 10000)):
            with self.subTest(value=value):
                data = await self.level(value)
                self.assertEqual(list(data), ["alerts.large_amount", "alerts.unusual", "limits.warn_percent",
                                              "alerts.low_balance", "periods.payday"])
                self.assertEqual(data["alerts.low_balance"], expected)
                self.assertIs(type(data["alerts.low_balance"]), type(expected))
        self.assertEqual(await self.stored(), [("alerts.low_balance", "10000")])
        response = await self.client.put("/api/v1/settings", json={"alerts.low_balance": 0}, headers=ORIGIN)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(await self.stored(), [("alerts.low_balance", "10000")])  # unchanged
        self.assertIsNone((await self.level(None))["alerts.low_balance"])
        self.assertEqual(await self.stored(), [("alerts.low_balance", "null")])

    async def test_origin_and_session(self):
        response = await self.client.put("/api/v1/settings", json={"alerts.low_balance": 10000},
                                         headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        app.dependency_overrides.pop(current_user)
        self.assertEqual((await self.client.get("/api/v1/settings")).status_code, 401)
        response = await self.client.put("/api/v1/settings", json={"alerts.low_balance": 10000}, headers=ORIGIN)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(await self.stored(), [])


if __name__ == "__main__":
    unittest.main()
