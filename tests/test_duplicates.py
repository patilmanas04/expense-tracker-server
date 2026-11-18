"""Possible duplicates (duplicates.py): the three rules, holding out of every total, Not a duplicate,
releasing on delete, the notification and its wording.
In-memory SQLite; email stays off (no Gmail keys); Gemini stays off."""
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
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import duplicates
import limits
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import DUPLICATE_DISMISSED, DUPLICATE_PENDING, DuplicateFlag, Notification, Transaction, User

SERVER = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)  # Wed 21 Oct, 12:00 IST
MINUTE = timedelta(minutes=1)

SWIGGY_UPI = "Sent Rs.500.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629400100001"
SWIGGY_DEBIT = ("Rs.500.00 debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA swiggy@icici (UPI Ref No 629400100001). "
                "Not you? Call 18002586161")
DMART = ("Rs.1200.00 debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA dmart@ybl (UPI Ref No 629400100777). "
         "Not you? Call 18002586161")
TEA_1 = ("ICICI Bank Acct XX310 debited for Rs 40.00 on 21-Oct-26; SHARMA TEA STALL credited. UPI:629400100901. "
         "Call 18002662 for dispute.")
TEA_2 = ("ICICI Bank Acct XX310 debited for Rs 40.00 on 21-Oct-26; SHARMA TEA STALL credited. UPI:629400100902. "
         "Call 18002662 for dispute.")
SUFFIX = " (possible duplicate: held out of totals)"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class DuplicatesTestCase(unittest.IsolatedAsyncioTestCase):
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
        app.dependency_overrides[current_user] = lambda: self.owner
        self.counter = 0

    async def sms(self, text: str, when: datetime = NOW) -> dict:
        response = await self.client.post("/api/v1/webhook/sms", json={"message": text, "received_at": when.isoformat()},
                                          headers=HOOK)
        self.assertEqual(response.json()["status"], "success", response.text)
        return response.json()

    async def manual(self, amount, merchant: str, when: datetime, paid_with: str = "bank") -> dict:
        response = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": "DEBIT", "amount": str(amount), "merchant": merchant, "transaction_time": when.isoformat(),
            "paid_with": paid_with})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def seed(self, amount=500, *, type="DEBIT", when=NOW, created_at=None, parsed_by="regex", reference_id=None,
                   account_ref=None, merchant=None, user_id=None, currency="INR", is_parsed=True,
                   category="Food & Dining") -> uuid.UUID:
        """A row saved directly; `created_at` defaults to NOW plus one second per seeded row, so rows are
        saved in the order they're seeded."""
        self.counter += 1
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=type, amount=Decimal(str(amount)), currency=currency, merchant=merchant,
                              category=category, account_ref=account_ref, reference_id=reference_id, user_id=user_id,
                              is_parsed=is_parsed, parsed_by=parsed_by, raw_message="fixture",
                              message_hash=f"seed-{self.counter}", transaction_time=when,
                              created_at=created_at or NOW + timedelta(seconds=self.counter), updated_at=NOW)
            db.add(txn)
            await db.commit()
            return txn.id

    async def flag(self, transaction_id, original_id, status=DUPLICATE_PENDING, reason="reference") -> None:
        async with AsyncSessionLocal() as db:
            db.add(DuplicateFlag(transaction_id=transaction_id, original_id=original_id, user_id=self.owner.id,
                                 reason=reason, status=status, created_at=NOW))
            await db.commit()

    async def find(self, transaction_id):
        async with AsyncSessionLocal() as db:
            txn = await db.get(Transaction, transaction_id)
            owner = await db.get(User, self.owner.id)
            return await duplicates.find_original(db, owner, txn)

    async def flags(self) -> list[DuplicateFlag]:
        async with AsyncSessionLocal() as db:
            return list((await db.scalars(select(DuplicateFlag))).all())

    async def notifications(self, kind=None) -> list[Notification]:
        async with AsyncSessionLocal() as db:
            rows = (await db.scalars(select(Notification).order_by(Notification.created_at))).all()
            return [n for n in rows if kind is None or n.kind == kind]

    async def row(self, transaction_id) -> Transaction:
        async with AsyncSessionLocal() as db:
            return await db.get(Transaction, uuid.UUID(str(transaction_id)))

    async def listed(self) -> dict:
        response = await self.client.get("/api/v1/transactions")
        self.assertEqual(response.status_code, 200, response.text)
        return {t["id"]: t for t in response.json()}

    async def put(self, url: str, body: dict) -> dict:
        response = await self.client.put(url, json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def not_duplicate(self, transaction_id, headers=ORIGIN):
        return await self.client.post(f"/api/v1/transactions/{transaction_id}/not-duplicate", headers=headers)

    async def spent(self) -> dict:
        body = (await self.client.get("/api/v1/limits")).json()
        return {
            "overall": body["monthly"]["spent"],
            "categories": {c["category"]: c["monthly"]["spent"] for c in body["categories"]},
            "merchants": {m["merchant"]: m["monthly"]["spent"] for m in body["merchants"]},
        }


class RuleTests(DuplicatesTestCase):
    async def test_reference_rule(self):
        a = await self.seed(when=NOW - timedelta(days=2), reference_id="629400100001", merchant="Swiggy",
                            account_ref="HDFC XX4821")
        b = await self.seed(reference_id=" 629400100001 ", merchant=None, account_ref="ICICI XX310")
        self.assertEqual(await self.find(b), (a, "reference"))

    async def test_reference_rule_window_amount_and_type(self):
        await self.seed(when=NOW - timedelta(days=3, minutes=1), reference_id="R1")
        self.assertIsNone(await self.find(await self.seed(reference_id="R1")))
        await self.seed(amount=499, reference_id="R2")
        self.assertIsNone(await self.find(await self.seed(reference_id="R2")))
        await self.seed(type="CREDIT", reference_id="R3", category="Income")
        self.assertIsNone(await self.find(await self.seed(reference_id="R3")))

    async def test_same_details_merchant_any_case_or_spacing(self):
        a = await self.seed(when=NOW - 6 * MINUTE, merchant="SWIGGY", reference_id="R1")
        b = await self.seed(merchant="  swiggy ", reference_id="R2")
        self.assertEqual(await self.find(b), (a, "same_details"))

    async def test_same_details_ten_minute_edge(self):
        a = await self.seed(when=NOW - 10 * MINUTE, merchant="Swiggy")
        self.assertEqual(await self.find(await self.seed(merchant="Swiggy")), (a, "same_details"))
        await self.seed(amount=300, when=NOW - 10 * MINUTE - timedelta(seconds=1), merchant="Zepto")
        self.assertIsNone(await self.find(await self.seed(amount=300, merchant="Zepto")))

    async def test_account_only_when_a_merchant_is_missing(self):
        a = await self.seed(amount=100, when=NOW - 5 * MINUTE, account_ref="HDFC XX4821")
        self.assertEqual(await self.find(await self.seed(amount=100, account_ref="HDFC XX4821")), (a, "same_details"))
        c = await self.seed(amount=200, when=NOW - 5 * MINUTE, merchant="Swiggy", account_ref="HDFC XX4821")
        self.assertEqual(await self.find(await self.seed(amount=200, account_ref=" HDFC XX4821")), (c, "same_details"))

    async def test_different_shops_on_one_account_never_match(self):
        await self.seed(when=NOW - 5 * MINUTE, merchant="Swiggy", account_ref="HDFC XX4821")
        self.assertIsNone(await self.find(await self.seed(merchant="Zomato", account_ref="HDFC XX4821")))

    async def test_missing_merchant_needs_the_same_account(self):
        await self.seed(amount=100, when=NOW - 5 * MINUTE, account_ref="HDFC XX4821")
        self.assertIsNone(await self.find(await self.seed(amount=100, account_ref="ICICI XX310")))
        await self.seed(amount=200, when=NOW - 5 * MINUTE, merchant="Unknown")
        self.assertIsNone(await self.find(await self.seed(amount=200, account_ref="  ")))

    async def test_manual_entry_both_orders(self):
        hand = await self.seed(amount=1200, when=NOW - 80 * MINUTE, parsed_by="manual", merchant="DMart",
                               user_id=self.owner.id)
        sms = await self.seed(amount=1200, merchant="Dmart", account_ref="HDFC XX4821", reference_id="R1")
        self.assertEqual(await self.find(sms), (hand, "manual_entry"))
        sms2 = await self.seed(amount=900, when=NOW - 80 * MINUTE, merchant="Croma", account_ref="HDFC XX4821")
        hand2 = await self.seed(amount=900, parsed_by="manual", merchant="Croma", user_id=self.owner.id)
        self.assertEqual(await self.find(hand2), (sms2, "manual_entry"))

    async def test_manual_entry_24_hour_edge(self):
        hand = await self.seed(amount=1200, when=NOW - timedelta(hours=24), parsed_by="manual", user_id=self.owner.id)
        self.assertEqual(await self.find(await self.seed(amount=1200, merchant="DMart")), (hand, "manual_entry"))
        await self.seed(amount=700, when=NOW - timedelta(hours=24, seconds=1), parsed_by="manual", user_id=self.owner.id)
        self.assertIsNone(await self.find(await self.seed(amount=700, merchant="DMart")))

    async def test_two_hand_entries_and_cash_never_match(self):
        await self.seed(amount=1200, when=NOW - 80 * MINUTE, parsed_by="manual", merchant="DMart", user_id=self.owner.id)
        self.assertIsNone(await self.find(await self.seed(amount=1200, parsed_by="manual", merchant="Big Bazaar",
                                                          user_id=self.owner.id)))
        await self.seed(amount=40, when=NOW - 30 * MINUTE, parsed_by="manual", account_ref="Cash",
                        merchant="Sharma Tea Stall", user_id=self.owner.id)
        self.assertIsNone(await self.find(await self.seed(amount=40, merchant="Tea Point", account_ref="ICICI XX310")))
        await self.seed(amount=60, when=NOW - 30 * MINUTE, merchant="Tea Point", account_ref="ICICI XX310")
        self.assertIsNone(await self.find(await self.seed(amount=60, parsed_by="manual", account_ref="Cash",
                                                          merchant="Sharma Tea Stall", user_id=self.owner.id)))

    async def test_cash_never_matches_on_same_details(self):
        # E9: a cash entry with the same merchant and amount minutes after a bank SMS is a different payment.
        await self.seed(amount=40, when=NOW - MINUTE, merchant="SHARMA TEA STALL", account_ref="ICICI XX310",
                        reference_id="629400100901")
        self.assertIsNone(await self.find(await self.seed(amount=40, parsed_by="manual", account_ref="Cash",
                                                          merchant="Sharma Tea Stall", user_id=self.owner.id)))
        # The other order: the SMS saved after the cash entry.
        await self.seed(amount=60, when=NOW - MINUTE, parsed_by="manual", account_ref="Cash",
                        merchant="Sharma Tea Stall", user_id=self.owner.id)
        self.assertIsNone(await self.find(await self.seed(amount=60, merchant="SHARMA TEA STALL",
                                                          account_ref="ICICI XX310")))
        # Two cash entries for the same shop can still be one payment entered twice.
        first = await self.seed(amount=80, when=NOW - MINUTE, parsed_by="manual", account_ref="Cash",
                                merchant="Sharma Tea Stall", user_id=self.owner.id)
        self.assertEqual(await self.find(await self.seed(amount=80, parsed_by="manual", account_ref="Cash",
                                                         merchant="sharma tea stall", user_id=self.owner.id)),
                         (first, "same_details"))

    async def test_lowest_rule_wins(self):
        later = NOW + timedelta(hours=1)
        await self.seed(when=NOW - timedelta(hours=2), parsed_by="manual", user_id=self.owner.id)
        details = await self.seed(when=NOW - 5 * MINUTE, merchant="Swiggy", reference_id="R0")
        x = await self.seed(merchant="Swiggy", reference_id="R1", account_ref="HDFC XX4821", created_at=later)
        self.assertEqual(await self.find(x), (details, "same_details"))
        reference = await self.seed(when=NOW - timedelta(days=2), reference_id="R1")
        self.assertEqual(await self.find(x), (reference, "reference"))

    async def test_smallest_delta_then_newest_saved_wins(self):
        later = NOW + timedelta(hours=1)
        await self.seed(when=NOW - 8 * MINUTE, merchant="Swiggy")
        closer = await self.seed(when=NOW - 3 * MINUTE, merchant="Swiggy")
        self.assertEqual(await self.find(await self.seed(merchant="Swiggy", created_at=later)), (closer, "same_details"))
        await self.seed(amount=300, when=NOW - 5 * MINUTE, merchant="Zepto")
        newer = await self.seed(amount=300, when=NOW + 5 * MINUTE, merchant="Zepto")
        self.assertEqual(await self.find(await self.seed(amount=300, merchant="Zepto", created_at=later)),
                         (newer, "same_details"))

    async def test_a_row_saved_later_is_never_a_candidate(self):
        # Two twins saved one second apart: the earlier one never sees the later one.
        a = await self.seed(reference_id="R1", created_at=NOW)
        b = await self.seed(reference_id="R1", created_at=NOW + timedelta(seconds=1))
        self.assertIsNone(await self.find(a))
        self.assertEqual(await self.find(b), (a, "reference"))

    async def test_a_held_out_row_is_not_an_original(self):
        a = await self.seed(when=NOW - 2 * MINUTE, reference_id="R1")
        b = await self.seed(when=NOW - MINUTE, reference_id="R1")
        await self.flag(b, a)
        c = await self.seed(reference_id="R1")
        self.assertEqual(await self.find(c), (a, "reference"))

    async def test_dismissed_is_never_flagged_again_and_unparsed_is_ignored(self):
        a = await self.seed(reference_id="R1")
        b = await self.seed(reference_id="R1")
        await self.flag(b, a, status=DUPLICATE_DISMISSED)
        self.assertIsNone(await self.find(b))
        await self.seed(amount=0, reference_id="R2", is_parsed=False)
        self.assertIsNone(await self.find(await self.seed(amount=0, reference_id="R2")))
        await self.seed(amount=300, reference_id="R3", is_parsed=False)
        self.assertIsNone(await self.find(await self.seed(amount=300, reference_id="R3")))
        await self.seed(amount=250, reference_id="R4")
        self.assertIsNone(await self.find(await self.seed(amount=250, reference_id="R4", is_parsed=False)))

    async def test_currency_and_ownership(self):
        await self.seed(currency="USD", reference_id="R1")
        self.assertIsNone(await self.find(await self.seed(reference_id="R1")))
        await self.seed(amount=1200, when=NOW - 80 * MINUTE, parsed_by="manual", user_id=self.other.id)
        self.assertIsNone(await self.find(await self.seed(amount=1200, merchant="DMart")))
        unassigned = await self.seed(amount=300, reference_id="R2", user_id=None)
        self.assertEqual(await self.find(await self.seed(amount=300, reference_id="R2", user_id=self.owner.id)),
                         (unassigned, "reference"))


class FlowTests(DuplicatesTestCase):
    async def cuj1(self) -> tuple[dict, dict]:
        first = await self.sms(SWIGGY_UPI, NOW)
        second = await self.sms(SWIGGY_DEBIT, NOW + MINUTE)
        return first, second

    async def test_cuj1_two_sms_for_one_payment(self):
        first, second = await self.cuj1()
        a, b = first["transaction"]["id"], second["transaction"]["id"]
        self.assertIsNone(first["transaction"]["duplicate_of"])
        self.assertEqual(first["message"], "Transaction logged successfully via regex")
        self.assertEqual(second["status"], "success")
        self.assertTrue(second["message"].endswith(SUFFIX), second["message"])
        self.assertEqual(second["transaction"]["duplicate_of"], a)
        listed = await self.listed()
        self.assertIsNone(listed[a]["duplicate_of"])
        self.assertEqual(listed[b]["duplicate_of"], a)
        # The same text again is still the exact-text duplicate, and carries the flag too.
        again = (await self.client.post("/api/v1/webhook/sms", headers=HOOK, json={
            "message": SWIGGY_DEBIT, "received_at": (NOW + MINUTE).isoformat()})).json()
        self.assertEqual((again["status"], again["transaction"]["id"], again["transaction"]["duplicate_of"]),
                         ("duplicate", b, a))
        [notification] = await self.notifications()
        self.assertEqual(notification.kind, "duplicate")
        self.assertEqual(notification.title, "Possible duplicate: ₹500.00 at Swiggy")
        self.assertEqual(notification.body, "Same reference number as the payment at 12:00 pm")
        self.assertEqual(notification.link, "/activity?kind=review")
        self.assertEqual(notification.dedupe_key, f"dup:{b}")
        self.assertEqual(notification.user_id, self.owner.id)
        [flag] = await self.flags()
        self.assertEqual((str(flag.transaction_id), str(flag.original_id), flag.reason, flag.status, flag.user_id,
                          flag.decided_at), (b, a, "reference", "pending", self.owner.id, None))

    async def set_limits_and_balance(self) -> None:
        await self.put("/api/v1/limits", {"monthly_limit": 800})
        await self.put("/api/v1/limits", {"category": "Food & Dining", "monthly_limit": 800})
        await self.put("/api/v1/limits/merchants", {"merchant": "Swiggy", "monthly_limit": 800,
                                                   "category": "Food & Dining"})
        await self.put("/api/v1/balance", {"amount": 10000, "as_of": (NOW - timedelta(hours=1)).isoformat()})

    async def test_held_out_of_every_total_until_not_a_duplicate(self):
        await self.set_limits_and_balance()
        _, second = await self.cuj1()
        b = second["transaction"]["id"]
        self.assertEqual(second["balance"]["current"], 9500)
        self.assertEqual(await self.spent(), {"overall": 500, "categories": {"Food & Dining": 500},
                                              "merchants": {"Swiggy": 500}})
        self.assertEqual(await self.notifications("spend_limit"), [])
        balance = (await self.client.get("/api/v1/balance")).json()
        self.assertEqual((balance["current"], balance["counted"]), (9500, 1))
        before = await self.row(b)

        response = await self.not_duplicate(b)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], b)
        self.assertIsNone(body["duplicate_of"])
        self.assertGreater(_utc(datetime.fromisoformat(body["updated_at"])), _utc(before.updated_at))
        [flag] = await self.flags()
        self.assertEqual(flag.status, "dismissed")
        self.assertIsNotNone(flag.decided_at)
        self.assertEqual(await self.spent(), {"overall": 1000, "categories": {"Food & Dining": 1000},
                                              "merchants": {"Swiggy": 1000}})
        self.assertEqual(len(await self.notifications("spend_limit")), 3)  # overall, category and merchant
        balance = (await self.client.get("/api/v1/balance")).json()
        self.assertEqual((balance["current"], balance["counted"]), (9000, 2))
        self.assertIsNone((await self.listed())[b]["duplicate_of"])

        again = await self.not_duplicate(b)
        self.assertEqual((again.status_code, again.json()), (409, {"detail": "Not a pending duplicate."}))

    async def test_not_duplicate_errors(self):
        first, _ = await self.cuj1()
        unflagged = await self.not_duplicate(first["transaction"]["id"])
        self.assertEqual((unflagged.status_code, unflagged.json()), (409, {"detail": "Not a pending duplicate."}))
        unknown = await self.not_duplicate(uuid.uuid4())
        self.assertEqual((unknown.status_code, unknown.json()), (404, {"detail": "Transaction not found."}))
        theirs = await self.seed(parsed_by="manual", user_id=self.other.id)
        self.assertEqual((await self.not_duplicate(theirs)).status_code, 404)
        bad_origin = await self.not_duplicate(first["transaction"]["id"], headers={"Origin": "https://attacker.example"})
        self.assertEqual((bad_origin.status_code, bad_origin.json()), (403, {"detail": "Invalid request origin."}))
        app.dependency_overrides.clear()
        self.assertEqual((await self.not_duplicate(first["transaction"]["id"])).status_code, 401)

    async def test_cuj2_deleting_the_hand_entry_releases_the_sms(self):
        await self.put("/api/v1/limits", {"monthly_limit": 100000})
        with patch("main.check_spend_limits", new=AsyncMock()) as check:
            hand = await self.manual(1200, "DMart", NOW - 80 * MINUTE)
            sms = (await self.sms(DMART, NOW))["transaction"]
            self.assertEqual(sms["duplicate_of"], hand["id"])
            self.assertEqual([str(c.args[0]) for c in check.await_args_list], [hand["id"]])
            [flag] = await self.flags()
            self.assertEqual(flag.reason, "manual_entry")
            [notification] = await self.notifications()
            self.assertTrue(notification.body.startswith("Matches the ₹1,200.00 you added by hand at "), notification.body)
            self.assertEqual(notification.body, "Matches the ₹1,200.00 you added by hand at 10:40 am")
            self.assertEqual((await self.spent())["overall"], 1200)
            before = await self.row(sms["id"])

            response = await self.client.delete(f"/api/v1/transactions/{hand['id']}", headers=ORIGIN)
            self.assertEqual(response.status_code, 204)
            after = await self.row(sms["id"])
            self.assertIsNone(after.duplicate_of)
            self.assertGreater(_utc(after.updated_at), _utc(before.updated_at))
            self.assertEqual(await self.flags(), [])
            self.assertEqual(str(check.await_args_list[-1].args[0]), sms["id"])
            self.assertEqual(len(check.await_args_list), 2)
        self.assertIsNone((await self.listed())[sms["id"]]["duplicate_of"])
        self.assertEqual((await self.spent())["overall"], 1200)

    async def test_e9_cash_after_the_same_sms_is_not_flagged(self):
        sms = (await self.sms(TEA_1, NOW))["transaction"]
        cash = await self.manual(40, "Sharma Tea Stall", NOW + MINUTE, paid_with="cash")
        self.assertIsNone(cash["duplicate_of"])
        self.assertEqual(await self.flags(), [])
        self.assertEqual(await self.notifications("duplicate"), [])
        listed = await self.listed()
        self.assertIsNone(listed[sms["id"]]["duplicate_of"])
        self.assertIsNone(listed[cash["id"]]["duplicate_of"])

    async def test_sms_then_hand_entry(self):
        with patch("main.check_spend_limits", new=AsyncMock()) as check:
            sms = (await self.sms(DMART, NOW))["transaction"]
            hand = await self.manual(1200, "DMart", NOW + 30 * MINUTE)
            self.assertEqual(hand["duplicate_of"], sms["id"])
            self.assertEqual([str(c.args[0]) for c in check.await_args_list], [sms["id"]])
        [notification] = await self.notifications()
        self.assertTrue(notification.body.startswith("Matches the bank SMS for ₹1,200.00 at "), notification.body)
        self.assertEqual(notification.title, "Possible duplicate: ₹1,200.00 at DMart")

    async def test_deleting_the_held_out_row_removes_its_flag(self):
        first, second = await self.cuj1()
        before = await self.row(first["transaction"]["id"])
        response = await self.client.delete(f"/api/v1/transactions/{second['transaction']['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(await self.flags(), [])
        after = await self.row(first["transaction"]["id"])
        self.assertEqual(_utc(after.updated_at), _utc(before.updated_at))
        self.assertEqual(list(await self.listed()), [first["transaction"]["id"]])

    async def test_cuj3_a_real_repeat_payment(self):
        await self.sms(TEA_1, NOW)
        second = (await self.sms(TEA_2, NOW + 6 * MINUTE))["transaction"]
        self.assertIsNotNone(second["duplicate_of"])
        [flag] = await self.flags()
        self.assertEqual(flag.reason, "same_details")
        [notification] = await self.notifications()
        self.assertTrue(notification.body.startswith("Same amount and merchant as the payment at"), notification.body)
        self.assertEqual(notification.title, "Possible duplicate: ₹40.00 at Sharma Tea Stall")
        response = await self.not_duplicate(second["id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.spent())["overall"], 80)
        self.assertIsNone(await self.find(uuid.UUID(second["id"])))

    async def test_flag_is_in_the_same_commit(self):
        a = await self.seed(reference_id="R1")
        async with AsyncSessionLocal() as db:
            owner = await db.get(User, self.owner.id)
            twin = Transaction(type="DEBIT", amount=Decimal("500"), currency="INR", reference_id="R1", raw_message="twin",
                               transaction_time=NOW, created_at=NOW + timedelta(minutes=5), updated_at=NOW)
            db.add(twin)
            await db.flush()
            self.assertEqual(await duplicates.flag_if_duplicate(db, owner, twin), (a, "reference"))
            await db.rollback()
        self.assertEqual(await self.flags(), [])
        async with AsyncSessionLocal() as db:
            self.assertEqual(list((await db.scalars(select(Transaction.id))).all()), [a])

    async def test_a_failing_check_never_loses_the_sms(self):
        await self.sms(SWIGGY_UPI, NOW)
        with patch("duplicates.find_original", side_effect=RuntimeError("bug")), \
                self.assertLogs("duplicates", "ERROR") as logs:
            second = await self.sms(SWIGGY_DEBIT, NOW + MINUTE)
        self.assertIn("Duplicate check failed", logs.output[0])
        self.assertIsNone(second["transaction"]["duplicate_of"])
        self.assertEqual(second["message"], "Transaction logged successfully via regex")
        self.assertEqual(len(await self.listed()), 2)
        self.assertEqual(await self.flags(), [])

    async def test_a_failing_notification_never_loses_the_flag(self):
        first = await self.sms(SWIGGY_UPI, NOW)
        with patch("duplicates.create_once", side_effect=RuntimeError("bug")), \
                self.assertLogs("duplicates", "ERROR") as logs:
            second = await self.sms(SWIGGY_DEBIT, NOW + MINUTE)
        self.assertIn("Duplicate notification failed", logs.output[0])
        self.assertEqual(second["transaction"]["duplicate_of"], first["transaction"]["id"])
        self.assertTrue(second["message"].endswith(SUFFIX))
        self.assertEqual(len(await self.flags()), 1)
        self.assertEqual(await self.notifications(), [])

    async def test_no_owner_skips_detection(self):
        with patch("duplicates.OWNER_EMAIL", ""):
            first, second = await self.cuj1()
        self.assertIsNone(second["transaction"]["duplicate_of"])
        self.assertEqual(second["message"], "Transaction logged successfully via regex")
        self.assertEqual(await self.flags(), [])
        self.assertEqual(await self.notifications("duplicate"), [])
        self.assertEqual(len(await self.listed()), 2)

    async def test_limit_check_skips_a_held_out_row(self):
        await self.put("/api/v1/limits", {"monthly_limit": 1})
        a = await self.seed(reference_id="R1")
        b = await self.seed(reference_id="R1")
        await self.flag(b, a)
        async with AsyncSessionLocal() as db:
            await limits._check(db, b)
        self.assertEqual(await self.notifications(), [])
        async with AsyncSessionLocal() as db:
            await limits._check(db, a)
        [notification] = await self.notifications()
        self.assertTrue(notification.body.startswith("₹500.00 of ₹1.00"), notification.body)  # b isn't counted

    async def test_edits_do_not_rerun_detection(self):
        await self.seed(amount=300, when=NOW - 2 * MINUTE, merchant="Zepto", reference_id="R1")
        other = await self.seed(amount=200, when=NOW - MINUTE, merchant="Zepto", reference_id="R2")
        response = await self.client.patch(f"/api/v1/transactions/{other}", json={"amount": "300"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["duplicate_of"])
        self.assertEqual(await self.flags(), [])

        _, second = await self.cuj1()
        b, a = second["transaction"]["id"], second["transaction"]["duplicate_of"]
        response = await self.client.patch(f"/api/v1/transactions/{b}", json={"notes": "check with bank"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["notes"], response.json()["duplicate_of"]), ("check with bank", a))
        [flag] = [f for f in await self.flags() if str(f.transaction_id) == b]
        self.assertEqual(flag.status, "pending")


class WordingTests(DuplicatesTestCase):
    async def notify(self, transaction_id) -> Notification:
        async with AsyncSessionLocal() as db:
            txn = await db.get(Transaction, transaction_id)
            owner = await db.get(User, self.owner.id)
            found = await duplicates.find_original(db, owner, txn)
            self.assertIsNotNone(found)
            await duplicates.notify(db, self.owner.id, txn, found)
        [notification] = [n for n in await self.notifications() if n.dedupe_key == f"dup:{transaction_id}"]
        return notification

    async def test_account_variant_without_a_merchant(self):
        await self.seed(account_ref="HDFC XX4821")
        n = await self.notify(await self.seed(when=NOW + 5 * MINUTE, account_ref="HDFC XX4821", merchant="unknown"))
        self.assertEqual((n.title, n.body), ("Possible duplicate: ₹500.00",
                                             "Same amount and account as the payment at 12:00 pm"))

    async def test_credit_wording(self):
        await self.seed(type="CREDIT", merchant="Acme Corp", reference_id="R1", category="Income")
        n = await self.notify(await self.seed(type="CREDIT", merchant="Acme Corp", reference_id="R1", category="Income"))
        self.assertEqual((n.title, n.body), ("Possible duplicate: ₹500.00 from Acme Corp",
                                             "Same reference number as the credit at 12:00 pm"))

    async def test_original_on_another_day(self):
        await self.seed(when=NOW - timedelta(days=1, hours=3), reference_id="R1")  # 20 Oct, 9:00 am IST
        n = await self.notify(await self.seed(reference_id="R1", merchant="Swiggy"))
        self.assertEqual((n.title, n.body), ("Possible duplicate: ₹500.00 at Swiggy",
                                             "Same reference number as the payment at 9:00 am on 20 Oct"))

    async def test_long_merchant_is_shortened_in_the_title(self):
        name = "A" * 130
        await self.seed(merchant=name, when=NOW - MINUTE)
        n = await self.notify(await self.seed(merchant=name))
        self.assertEqual(n.title, f"Possible duplicate: ₹500.00 at {'A' * 119}…")
        self.assertEqual(n.body, "Same amount and merchant as the payment at 11:59 am")


class ImportOrderTests(unittest.TestCase):
    def test_duplicates_imports_cleanly(self):
        env = {**os.environ, "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
        code = "import spending, duplicates, limits, categorize, main; print('ok')"
        result = subprocess.run([sys.executable, "-c", code], cwd=SERVER, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr)


if __name__ == "__main__":
    unittest.main()
