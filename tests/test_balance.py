"""Running-balance tests. Isolated in-memory SQLite; never touches the real database or network."""
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
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from auth import current_user
from balance import SKIP_REASONS, affects_bank_balance, classify
from database import AsyncSessionLocal, Base, engine
from main import TransactionResponse, app
from models import DUPLICATE_PENDING, DuplicateFlag, Transaction, User

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}

HDFC_UPI_DEBIT = "Sent Rs.450.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629412345678\nNot You?\nCall 18002586161/SMS BLOCK UPI to 7308080808"
HDFC_UPI_CREDIT = "Credit Alert!\nRs.2,500.00 credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA rahul.k@okaxis (UPI 629498765432)"
HDFC_CARD_SPEND = "Spent Rs.1,249 On HDFC Bank Card 9012 At AMAZON PAY INDIA On 2026-10-21:13:05:44.Not You? To Block+Reissue Call 18002586161/SMS BLOCK CC 9012 to 7308080808"
HDFC_CARD_BILL_PAID = "Rs.1249.00 debited from HDFC Bank A/c XX4821 on 21-10-26 towards HDFC Bank Credit Card XX9012. Ref 629400000555"
ICICI_CARD_SPEND_LMT = "Rs 649.00 spent on ICICI Bank Card XX7001 on 16-Oct-26 at APOLLO PHARMACY. Avl Lmt: Rs 2,47,747.00."
HDFC_DEBIT_WITH_BAL = "Rs 2,340.00 debited via POS from HDFC Bank DC 5566 at DECATHLON on 21Oct26. Avl bal Rs 10,000.00"
HDFC_CARD_BILL_WITH_BAL = HDFC_CARD_BILL_PAID + " Avl bal Rs 8,000.00"  # a card bill paid from the bank account
ICICI_CARD_REFUND = "UBER INDIA refund of Rs 120.00 credited to ICICI Bank Credit Card XX7001 on 06-OCT-26. Revised total due Rs 9,999.00"


class BankBalanceRuleTests(unittest.TestCase):
    def test_credit_card_alerts_do_not_touch_the_bank_balance(self):
        for sms in [
            HDFC_CARD_SPEND,
            "INR 1,604.00 spent using ICICI Bank Card XX7001 on 01-Oct-26 on ZOMATO. Avl Limit: INR 2,48,396.00.",
            "Rs 649.00 spent on ICICI Bank Card XX7001 on 16-Oct-26 at APOLLO PHARMACY. Avl Lmt: Rs 2,47,747.00.",
            "Txn Rs.199.00\nOn HDFC Bank Card 9012\nAt netflix.upi@hdfcbank\nby UPI 629477778888\nOn 21-10\nNot You?\nCall 18002586161/SMS BLOCK CC 9012 to 7308080808",
            "HDFC Bank Cardmember, Online Payment of Rs.1000 vide Ref# 000XXXX was credited to your card ending 9012 On 08/OCT/2026",
            "Dear Customer, Payment of INR 1,234.00 has been received on your ICICI Bank Credit Card Account 4xxx7001 on 06-OCT-26.",
            "UBER INDIA refund of Rs 120.00 credited to ICICI Bank Credit Card XX7001 on 06-OCT-26. Revised total due Rs 9,999.00",
            "Dear Smart Pay Customer,We have successfully debited your HDFC Bank Credit card ending 9012 to pay your SpayBBPS BESCOM bill for the amount Rs 1450",
        ]:
            with self.subTest(sms=sms[:50]):
                self.assertFalse(affects_bank_balance(sms))

    def test_bank_account_and_debit_card_alerts_count(self):
        for sms in [
            HDFC_UPI_DEBIT,
            HDFC_UPI_CREDIT,
            HDFC_CARD_BILL_PAID,
            "ICICI Bank Acct XX310 debited for Rs 60.00 on 21-Oct-26; Bangalore Metro credited. UPI:629411112222.",
            "Spent Rs.2000 From HDFC Bank Card x5566 At HPCL On 2026-10-20:18:01:12 Bal Rs.8,142.26 Not You? Call 18002586161/SMS BLOCK DC 5566 to 7308080808",
            "Rs 2,340.00 debited via POS from HDFC Bank DC 5566 at DECATHLON on 21Oct26. Avl bal Rs 10,000.00",
        ]:
            with self.subTest(sms=sms[:50]):
                self.assertTrue(affects_bank_balance(sms))


class ClassifyTests(unittest.TestCase):
    def reason(self, is_parsed=True, currency="INR", held_out=False, account_ref="HDFC XX4821", raw_message=HDFC_UPI_DEBIT):
        return classify(is_parsed, currency, held_out, account_ref, raw_message, "INR")

    def test_each_reason_alone(self):
        self.assertEqual(self.reason(is_parsed=False), "unreadable")
        self.assertEqual(self.reason(currency="USD"), "other_currency")
        self.assertEqual(self.reason(held_out=True), "duplicate")
        self.assertEqual(self.reason(account_ref="Cash"), "cash")
        self.assertEqual(self.reason(raw_message=HDFC_CARD_SPEND), "credit_card")
        self.assertIsNone(self.reason())
        self.assertIsNone(self.reason(raw_message=HDFC_CARD_BILL_PAID))
        self.assertIsNone(self.reason(account_ref=None))

    def test_first_reason_wins(self):
        self.assertEqual(self.reason(is_parsed=False, currency="USD", held_out=True, account_ref="Cash"), "unreadable")
        self.assertEqual(self.reason(currency="USD", raw_message=HDFC_CARD_SPEND), "other_currency")
        self.assertEqual(self.reason(held_out=True, account_ref="Cash"), "duplicate")
        self.assertEqual(self.reason(account_ref="Cash", raw_message=HDFC_CARD_SPEND), "cash")

    def test_reason_order(self):
        self.assertEqual(SKIP_REASONS, ("unreadable", "other_currency", "duplicate", "cash", "credit_card"))


class BalanceKindTests(unittest.TestCase):
    def response(self, raw_message, balance):
        now = datetime.now(timezone.utc)
        return TransactionResponse(
            id=uuid.uuid4(), type="DEBIT", amount=100, currency="INR", merchant=None, category=None, account_ref=None,
            balance=balance, reference_id=None, sender=None, raw_message=raw_message, is_parsed=True, parsed_by="regex",
            notes=None, transaction_time=now, created_at=now, updated_at=now,
        )

    def test_kind_follows_the_balance_rule(self):
        for raw_message, balance, kind in [
            (HDFC_UPI_DEBIT, None, None),
            (ICICI_CARD_SPEND_LMT, None, None),
            (ICICI_CARD_SPEND_LMT, 247747, "card"),
            (HDFC_DEBIT_WITH_BAL, 10000, "bank"),
            (HDFC_CARD_BILL_PAID, 8000, "bank"),
        ]:
            with self.subTest(sms=raw_message[:40], balance=balance):
                self.assertEqual(self.response(raw_message, balance).model_dump()["balance_kind"], kind)

    def test_kind_is_serialized(self):
        self.assertIn('"balance_kind":"card"', self.response(ICICI_CARD_SPEND_LMT, 247747).model_dump_json())
        self.assertIn('"balance_kind":null', self.response(HDFC_UPI_DEBIT, None).model_dump_json())


class BalanceApiTests(unittest.IsolatedAsyncioTestCase):
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

    def sign_in(self):
        app.dependency_overrides[current_user] = lambda: self.owner

    async def put_balance(self, amount, **extra):
        return await self.client.put("/api/v1/balance", json={"amount": amount, **extra}, headers=ORIGIN)

    async def sms(self, message):
        response = await self.client.post("/api/v1/webhook/sms", json={"message": message}, headers=HOOK)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def add_row(self, **fields):
        """Insert a transaction (a bank debit owned by the owner unless told otherwise) and return its id."""
        now = datetime.now(timezone.utc)
        values = dict(type="DEBIT", amount=100, currency="INR", is_parsed=True, user_id=self.owner.id,
                      raw_message=HDFC_UPI_DEBIT, account_ref="HDFC XX4821", parsed_by="regex",
                      message_hash=f"row-{uuid.uuid4().hex}", transaction_time=now, created_at=now)
        values.update(fields)
        async with AsyncSessionLocal() as db:
            row = Transaction(**values)
            db.add(row)
            await db.commit()
            return row.id

    async def hold_out(self, txn_id, original_id):
        async with AsyncSessionLocal() as db:
            db.add(DuplicateFlag(transaction_id=txn_id, original_id=original_id, user_id=self.owner.id,
                                 reason="reference", status=DUPLICATE_PENDING))
            await db.commit()

    async def get_breakdown(self):
        response = await self.client.get("/api/v1/balance/breakdown")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_requires_login_and_same_origin(self):
        self.assertEqual((await self.client.get("/api/v1/balance")).status_code, 401)
        self.assertEqual((await self.put_balance(100)).status_code, 401)
        self.sign_in()
        no_origin = await self.client.put("/api/v1/balance", json={"amount": 100})
        foreign = await self.client.put("/api/v1/balance", json={"amount": 100}, headers={"Origin": "https://attacker.example"})
        delete_foreign = await self.client.delete("/api/v1/balance", headers={"Origin": "https://attacker.example"})
        self.assertEqual([no_origin.status_code, foreign.status_code, delete_foreign.status_code], [403, 403, 403])

    async def test_not_configured_returns_null_everywhere(self):
        self.sign_in()
        response = await self.client.get("/api/v1/balance")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json())
        self.assertIsNone((await self.sms(HDFC_UPI_DEBIT))["balance"])

    async def test_set_and_read_balance(self):
        self.sign_in()
        response = await self.put_balance("50000.50")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data["opening_balance"], data["current"], data["currency"], data["counted"]), (50000.5, 50000.5, "INR", 0))
        self.assertEqual((await self.client.get("/api/v1/balance")).json()["current"], 50000.5)

    async def test_webhook_debits_and_credits_move_the_balance_once(self):
        self.sign_in()
        await self.put_balance(50000)
        after_debit = await self.sms(HDFC_UPI_DEBIT)
        self.assertEqual(after_debit["status"], "success")
        self.assertEqual(after_debit["balance"]["current"], 49550)
        after_credit = await self.sms(HDFC_UPI_CREDIT)
        self.assertEqual(after_credit["balance"]["current"], 52050)
        duplicate = await self.sms(HDFC_UPI_DEBIT)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["balance"]["current"], 52050)
        ignored = await self.sms("OTP 123456 for INR 999 purchase")
        self.assertEqual(ignored["status"], "ignored")
        summary = (await self.client.get("/api/v1/balance")).json()
        self.assertEqual((summary["spent"], summary["received"], summary["counted"], summary["current"]), (450, 2500, 2, 52050))

    async def test_card_spend_counts_only_when_the_bill_is_paid(self):
        self.sign_in()
        await self.put_balance(10000)
        spend = await self.sms(HDFC_CARD_SPEND)
        self.assertEqual(spend["status"], "success")
        self.assertEqual((spend["balance"]["current"], spend["balance"]["excluded_card"]), (10000, 1))
        bill = await self.sms(HDFC_CARD_BILL_PAID)
        self.assertEqual(bill["balance"]["current"], 8751)

    async def test_counts_only_owner_parsed_same_currency_after_start(self):
        self.sign_in()
        now = datetime.now(timezone.utc)
        await self.put_balance(1000, as_of=(now - timedelta(minutes=5)).isoformat())
        async with AsyncSessionLocal() as db:
            for i, (kind, amount, minutes_ago, parsed, currency, owner) in enumerate([
                ("DEBIT", 100, 10, True, "INR", self.owner.id),  # before the starting point
                ("DEBIT", 200, 1, True, "INR", None),  # unassigned SMS belong to the owner
                ("CREDIT", 300, 2, True, "INR", self.owner.id),
                ("DEBIT", 999, 1, False, "INR", None),  # needs review
                ("DEBIT", 50, 1, True, "USD", None),  # other currency
                ("DEBIT", 777, 1, True, "INR", self.other.id),  # someone else's
            ]):
                db.add(Transaction(type=kind, amount=amount, currency=currency, is_parsed=parsed, user_id=owner,
                                   raw_message=f"Rs {amount} via a/c XX1234", message_hash=f"seed-{i}",
                                   transaction_time=now - timedelta(minutes=minutes_ago)))
            await db.commit()
        summary = (await self.client.get("/api/v1/balance")).json()
        self.assertEqual((summary["current"], summary["spent"], summary["received"], summary["counted"]), (1100, 200, 300, 2))

    async def test_saving_again_resets_the_starting_point_and_delete_clears(self):
        self.sign_in()
        await self.put_balance(1000)
        self.assertEqual((await self.sms(HDFC_UPI_DEBIT))["balance"]["current"], 550)
        reset = (await self.put_balance(5000)).json()
        self.assertEqual((reset["current"], reset["counted"]), (5000, 0))
        cleared = await self.client.delete("/api/v1/balance", headers=ORIGIN)
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone((await self.client.get("/api/v1/balance")).json())
        self.assertIsNone((await self.sms(HDFC_UPI_CREDIT))["balance"])

    async def test_validation(self):
        self.sign_in()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for body in [
            {"amount": "12.345"},
            {"amount": "1e15"},
            {"amount": 100, "currency": "inr"},
            {"amount": 100, "as_of": future},
            {"amount": 100, "as_of": "2026-10-20T10:00:00"},
        ]:
            with self.subTest(body=body):
                response = await self.client.put("/api/v1/balance", json=body, headers=ORIGIN)
                self.assertEqual(response.status_code, 422, response.text)


    async def test_breakdown_requires_login(self):
        self.assertEqual((await self.client.get("/api/v1/balance/breakdown")).status_code, 401)

    async def test_breakdown_without_balance_is_404(self):
        self.sign_in()
        response = await self.client.get("/api/v1/balance/breakdown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "No balance set.")

    async def test_breakdown_matches_summary_with_every_reason(self):
        self.sign_in()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        as_of = now - timedelta(hours=1)
        self.assertEqual((await self.put_balance(10000, as_of=as_of.isoformat())).status_code, 200)

        def at(minutes):
            return dict(transaction_time=as_of + timedelta(minutes=minutes), created_at=as_of + timedelta(minutes=minutes))

        debit = await self.add_row(amount=450, **at(1))
        credit = await self.add_row(type="CREDIT", amount=2500, raw_message=HDFC_UPI_CREDIT, **at(2))
        bill = await self.add_row(amount=1249, raw_message=HDFC_CARD_BILL_PAID, **at(3))
        unparsed = await self.add_row(amount=0, is_parsed=False, parsed_by="failed", raw_message="Garbled SMS", **at(4))
        usd = await self.add_row(amount=50, currency="USD", **at(5))
        held = await self.add_row(amount=300, **at(6))
        await self.hold_out(held, debit)
        cash = await self.add_row(amount=200, account_ref="Cash", parsed_by="manual", raw_message="Chai", **at(7))
        card = await self.add_row(amount=1249, raw_message=HDFC_CARD_SPEND, account_ref="HDFC XX9012", **at(8))
        refund = await self.add_row(type="CREDIT", amount=120, raw_message=ICICI_CARD_REFUND, account_ref="ICICI XX7001", **at(9))
        unassigned = await self.add_row(amount=100, user_id=None, **at(10))
        before = await self.add_row(amount=999, transaction_time=as_of - timedelta(minutes=1), created_at=as_of)
        someone_else = await self.add_row(amount=777, user_id=self.other.id, **at(11))

        data = await self.get_breakdown()
        summary = (await self.client.get("/api/v1/balance")).json()
        expected = Decimal(10000 - 450 + 2500 - 1249 - 100)
        self.assertEqual(Decimal(data["current"]), expected)
        self.assertEqual(Decimal(str(summary["current"])), expected)
        self.assertEqual(data["current"], "10701.00")
        self.assertEqual([row["id"] for row in data["counted"]], [str(i) for i in (debit, credit, bill, unassigned)])

        skipped = data["skipped"]
        self.assertEqual(list(skipped), list(SKIP_REASONS))
        self.assertEqual(skipped["unreadable"], {"count": 1, "total": None, "ids": [str(unparsed)]})
        self.assertEqual(skipped["other_currency"], {"count": 1, "total": None, "ids": [str(usd)]})
        self.assertEqual(skipped["duplicate"], {"count": 1, "total": "-300.00", "ids": [str(held)]})
        self.assertEqual(skipped["cash"], {"count": 1, "total": "-200.00", "ids": [str(cash)]})
        self.assertEqual(skipped["credit_card"], {"count": 2, "total": "-1129.00", "ids": [str(card), str(refund)]})

        seen = [row["id"] for row in data["counted"]] + [i for group in skipped.values() for i in group["ids"]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {str(i) for i in (debit, credit, bill, unparsed, usd, held, cash, card, refund, unassigned)})
        self.assertNotIn(str(before), seen)
        self.assertNotIn(str(someone_else), seen)

        self.assertEqual(data["currency"], "INR")
        self.assertEqual(data["start"]["amount"], "10000.00")
        self.assertEqual(datetime.fromisoformat(data["start"]["as_of"]), as_of)

    async def test_cash_and_held_out_is_duplicate_only(self):
        self.sign_in()
        now = datetime.now(timezone.utc)
        await self.put_balance(5000, as_of=(now - timedelta(hours=1)).isoformat())
        original = await self.add_row(amount=200, account_ref="Cash", parsed_by="manual", raw_message="Lunch",
                                      transaction_time=now - timedelta(minutes=30), created_at=now - timedelta(minutes=30))
        copy = await self.add_row(amount=200, account_ref="Cash", parsed_by="manual", raw_message="Lunch",
                                  transaction_time=now - timedelta(minutes=20), created_at=now - timedelta(minutes=20))
        await self.hold_out(copy, original)
        skipped = (await self.get_breakdown())["skipped"]
        self.assertEqual(skipped["duplicate"], {"count": 1, "total": "-200.00", "ids": [str(copy)]})
        self.assertEqual(skipped["cash"], {"count": 1, "total": "-200.00", "ids": [str(original)]})

    async def test_running_balance_and_order(self):
        self.sign_in()
        now = datetime.now(timezone.utc)
        await self.put_balance(1000, as_of=(now - timedelta(hours=1)).isoformat())
        t1, t2, t3 = (now - timedelta(minutes=m) for m in (50, 40, 30))
        third = await self.add_row(amount=25, transaction_time=t3, created_at=t3)
        first = await self.add_row(amount=100, transaction_time=t1, created_at=t1)
        second = await self.add_row(type="CREDIT", amount=50, raw_message=HDFC_UPI_CREDIT, transaction_time=t2, created_at=t2)
        data = await self.get_breakdown()
        self.assertEqual([row["id"] for row in data["counted"]], [str(first), str(second), str(third)])
        self.assertEqual([row["effect"] for row in data["counted"]], ["-100.00", "50.00", "-25.00"])
        self.assertEqual([row["running"] for row in data["counted"]], ["900.00", "950.00", "925.00"])
        self.assertEqual(data["current"], "925.00")

    async def test_same_time_rows_order_by_created_at_then_id(self):
        self.sign_in()
        now = datetime.now(timezone.utc)
        await self.put_balance(1000, as_of=(now - timedelta(hours=1)).isoformat())
        when = now - timedelta(minutes=30)
        later = await self.add_row(amount=10, transaction_time=when, created_at=now - timedelta(minutes=5))
        earlier = await self.add_row(amount=20, transaction_time=when, created_at=now - timedelta(minutes=10))
        self.assertEqual([row["id"] for row in (await self.get_breakdown())["counted"]], [str(earlier), str(later)])

        tie_time = now - timedelta(minutes=20)
        tied = [await self.add_row(amount=1, transaction_time=tie_time, created_at=tie_time) for _ in range(4)]
        ids = [row["id"] for row in (await self.get_breakdown())["counted"]][2:]
        self.assertEqual(ids, sorted((str(i) for i in tied), key=lambda u: uuid.UUID(u).hex))

    async def test_unreadable_and_other_currency_totals_are_null_even_when_empty(self):
        self.sign_in()
        await self.put_balance("2500.5")
        data = await self.get_breakdown()
        self.assertEqual(data["counted"], [])
        self.assertEqual(data["skipped"], {
            "unreadable": {"count": 0, "total": None, "ids": []},
            "other_currency": {"count": 0, "total": None, "ids": []},
            "duplicate": {"count": 0, "total": "0.00", "ids": []},
            "cash": {"count": 0, "total": "0.00", "ids": []},
            "credit_card": {"count": 0, "total": "0.00", "ids": []},
        })
        self.assertEqual(data["start"]["amount"], "2500.50")
        self.assertEqual(data["current"], data["start"]["amount"])

    async def test_offsetting_rows_never_show_negative_zero(self):
        self.sign_in()
        now = datetime.now(timezone.utc)
        await self.put_balance(0, as_of=(now - timedelta(hours=1)).isoformat())
        for minutes, kind, message in [(50, "DEBIT", HDFC_CARD_SPEND), (40, "CREDIT", ICICI_CARD_REFUND)]:
            await self.add_row(type=kind, amount=120, raw_message=message, transaction_time=now - timedelta(minutes=minutes),
                               created_at=now - timedelta(minutes=minutes))
        data = await self.get_breakdown()
        self.assertEqual((data["start"]["amount"], data["current"], data["skipped"]["credit_card"]["total"]),
                         ("0.00", "0.00", "0.00"))

    async def test_read_at_earlier_counts_rows_since_then(self):
        self.sign_in()
        now = datetime.now(timezone.utc)
        for hours, amount in [(10, 500), (8, 100), (5, 200), (1, 300)]:
            await self.add_row(amount=amount, transaction_time=now - timedelta(hours=hours), created_at=now - timedelta(hours=hours))
        response = await self.put_balance("45000", as_of=(now - timedelta(hours=9)).isoformat())
        self.assertEqual(response.status_code, 200, response.text)
        data = await self.get_breakdown()
        self.assertEqual([row["effect"] for row in data["counted"]], ["-100.00", "-200.00", "-300.00"])
        self.assertEqual(data["current"], "44400.00")
        self.assertEqual((await self.client.get("/api/v1/balance")).json()["current"], 44400)

    async def test_webhook_and_list_carry_balance_kind(self):
        self.sign_in()
        await self.put_balance(50000)
        kinds = {}
        for message, kind, balance in [
            (ICICI_CARD_SPEND_LMT, "card", 247747),
            (HDFC_DEBIT_WITH_BAL, "bank", 10000),
            (HDFC_CARD_BILL_WITH_BAL, "bank", 8000),
            (HDFC_UPI_DEBIT, None, None),
        ]:
            with self.subTest(sms=message[:40]):
                transaction = (await self.sms(message))["transaction"]
                self.assertEqual((transaction["balance_kind"], transaction["balance"]), (kind, balance))
                kinds[transaction["id"]] = kind
        counted = {row["id"] for row in (await self.get_breakdown())["counted"]}
        listed = (await self.client.get("/api/v1/transactions")).json()
        self.assertEqual({row["id"]: row["balance_kind"] for row in listed}, kinds)
        bills = [row["id"] for row in listed if row["raw_message"] == HDFC_CARD_BILL_WITH_BAL]
        self.assertEqual(len(bills), 1)
        self.assertIn(bills[0], counted)


if __name__ == "__main__":
    unittest.main()
