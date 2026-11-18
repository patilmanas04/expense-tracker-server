"""What counts as spending (spending.py): the category sets, the app's copy of them, card-bill
recognition, spend limits, merchant memory and the one-time recategorisation script.
In-memory SQLite; email stays off (no Gmail keys); Gemini is mocked."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import contextlib
import io
import re
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import categorize
import parser
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import CategoryLimit, MerchantCategory, Notification, Transaction, User
from parser import CATEGORIES, Recalled, is_card_bill_debit, normalize_category, parse_sms_with_regex
from scripts import recategorise_card_payments as script
from spending import EXCLUDED_FROM_SPENDING, INVESTED, NOT_COUNTED, counts_as_spending

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)  # Wed 21 Oct, 12:00 IST

HDFC_CARD_BILL_PAID = "Rs.1249.00 debited from HDFC Bank A/c XX4821 on 21-10-26 towards HDFC Bank Credit Card XX9012. Ref 629400000555"
AMAZON_PAY_BILL = ("Rs 5,000.00 debited from ICICI Bank Acct XX310 on 05-Nov-26 towards Amazon Pay ICICI Credit Card XX7001. "
                   "Ref 629400000777")
NEFT_CARD_BILL = ("Amt Deducted! Rs.20000.00 from your HDFC Bank A/c XX4821 for NEFT txn towards credit card XX7001 via "
                  "HDFC Bank Online Banking")
HDFC_CARD_PAYMENT_RECEIVED = ("DEAR HDFCBANK CARDMEMBER, PAYMENT OF Rs. 12,000.00 RECEIVED TOWARDS YOUR CREDIT CARD ENDING WITH "
                              "9012 ON 17-10-2026.YOUR AVAILABLE LIMIT IS RS. 1,40,000.00")
ICICI_CARD_PAYMENT_RECEIVED = ("Payment of Rs 25,000.00 has been received on your ICICI Bank Credit Card XX7001 through Bharat "
                               "Bill Payment System on 21-OCT-26.")
HDFC_CARD_SPEND = ("Spent Rs.1,249 On HDFC Bank Card 9012 At AMAZON PAY INDIA On 2026-10-21:13:05:44.Not You? To Block+Reissue "
                   "Call 18002586161/SMS BLOCK CC 9012 to 7308080808")
ICICI_CARD_SPENDS = [
    "INR 1,604.00 spent using ICICI Bank Card XX7001 on 01-Oct-26 on ZOMATO. Avl Limit: INR 2,48,396.00.",
    "Rs 649.00 spent on ICICI Bank Card XX7001 on 16-Oct-26 at APOLLO PHARMACY. Avl Lmt: Rs 2,47,747.00.",
]
CARD_PAID_TO = "Rs 1200 paid to ABC TRADERS using HDFC Bank Credit Card XX9012"
NETFLIX_AUTOPAY = "Payment of Rs 649.00 for NETFLIX debited on your HDFC Bank Credit Card XX9012 via autopay"
SMART_PAY = ("Dear Smart Pay Customer,We have successfully debited your HDFC Bank Credit card ending 9012 to pay your "
             "SpayBBPS  BESCOM bill for the amount Rs 1450")
PPF = "Alert!\nRs. 5,000.00 transferred to your PPF/SSY A/c No. XX7788 via HDFC Bank Online Banking.\nNot you?Call 18002586161"


class SetsTests(unittest.TestCase):
    def test_members(self):
        self.assertEqual(NOT_COUNTED, {"Credit Card Payment", "Self Transfer"})
        self.assertEqual(INVESTED, {"Investments & Finance"})
        self.assertEqual(EXCLUDED_FROM_SPENDING, {"Credit Card Payment", "Self Transfer", "Investments & Finance"})
        for category in EXCLUDED_FROM_SPENDING:
            self.assertIn(category, CATEGORIES)

    def test_counts_as_spending(self):
        for category in [None, "Uncategorized", "Transfer", "Cash Withdrawal", "Food & Dining"]:
            self.assertTrue(counts_as_spending(category), category)
        for category in EXCLUDED_FROM_SPENDING:
            self.assertFalse(counts_as_spending(category), category)

    def test_self_transfer_category(self):
        self.assertEqual(CATEGORIES.index("Self Transfer") + 1, CATEGORIES.index("Transfer"))
        self.assertEqual(normalize_category("self transfer"), "Self Transfer")


class ClientCopyTests(unittest.TestCase):
    """The app keeps its own copy of the sets and the category list (lib/categories.ts)."""

    def setUp(self):
        path = Path(__file__).resolve().parents[2] / "expense-tracker-client" / "lib" / "categories.ts"
        if not path.exists():
            self.skipTest("expense-tracker-client isn't next to this repo")
        self.text = path.read_text(encoding="utf-8")

    def client_set(self, name: str) -> set[str]:
        match = re.search(rf"export const {name}\b[^=]*=\s*new Set(?:<[^>]*>)?\(\s*\[(.*?)\]\s*\)", self.text, re.S)
        self.assertIsNotNone(match, f"lib/categories.ts has no `export const {name} = new Set([...])`")
        return set(re.findall(r'"([^"]+)"', match.group(1)))

    def test_sets_match(self):
        self.assertEqual(self.client_set("NOT_COUNTED"), set(NOT_COUNTED))
        self.assertEqual(self.client_set("INVESTED"), set(INVESTED))

    def test_category_order_matches(self):
        block = re.search(r"const META[^=]*=\s*\{(.*?)\n\};", self.text, re.S)
        self.assertIsNotNone(block, "lib/categories.ts has no META block")
        keys = [quoted or bare for quoted, bare in re.findall(r'^\s*(?:"([^"]+)"|([A-Za-z]+)):\s*\{', block.group(1), re.M)]
        self.assertEqual(keys, [c for c in CATEGORIES if c != "Uncategorized"])


class CardBillParserTests(unittest.TestCase):
    def category(self, sms: str) -> str:
        return parse_sms_with_regex(sms).category

    def test_bank_side_card_bills(self):
        for sms in [HDFC_CARD_BILL_PAID, AMAZON_PAY_BILL, NEFT_CARD_BILL]:
            self.assertTrue(is_card_bill_debit(sms), sms)
            parsed = parse_sms_with_regex(sms)
            self.assertEqual((parsed.type, parsed.category), ("DEBIT", "Credit Card Payment"), sms)

    def test_card_side_payment_received_still_card_payment(self):
        for sms in [HDFC_CARD_PAYMENT_RECEIVED, ICICI_CARD_PAYMENT_RECEIVED]:
            parsed = parse_sms_with_regex(sms)
            self.assertEqual((parsed.type, parsed.category), ("CREDIT", "Credit Card Payment"), sms)

    def test_card_spends_are_not_bills(self):
        for sms in [HDFC_CARD_SPEND, *ICICI_CARD_SPENDS, CARD_PAID_TO, NETFLIX_AUTOPAY]:
            self.assertFalse(is_card_bill_debit(sms), sms)
            self.assertNotEqual(self.category(sms), "Credit Card Payment", sms)
        self.assertEqual(self.category(SMART_PAY), "Bills & Utilities")

    def test_card_account_is_not_a_bank_account(self):
        self.assertFalse(is_card_bill_debit(
            "Dear Customer, Payment of INR 1,234.00 has been received on your ICICI Bank Credit Card Account 4xxx7001"))


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_parse_card_bill_overrides_gemini(self):
        answer = parser._AIParse(is_transaction=True, type="DEBIT", amount=1249.0, category="Shopping")
        with patch("parser._ask_gemini", new=AsyncMock(return_value=answer)) as ask:
            parsed = await parser.parse_sms_with_gemini(HDFC_CARD_BILL_PAID)
        self.assertEqual(parsed.category, "Credit Card Payment")
        prompt = ask.await_args.args[0]
        for text in [parser.TRANSFER_GUIDANCE, parser.INVESTMENT_GUIDANCE, '"Self Transfer"']:
            self.assertIn(text, prompt)
        credit = parser._AIParse(is_transaction=True, type="CREDIT", amount=1249.0, category="Shopping")
        with patch("parser._ask_gemini", new=AsyncMock(return_value=credit)):
            self.assertEqual((await parser.parse_sms_with_gemini(HDFC_CARD_BILL_PAID)).category, "Shopping")

    async def test_categorize_prompt(self):
        with patch("parser._ask_gemini", new=AsyncMock(return_value=parser._AICategory(category="Transfer"))) as ask:
            self.assertEqual(await parser.categorize_with_gemini("Sent Rs.10 to Manas P", "Manas P", "DEBIT"), "Transfer")
        prompt = ask.await_args.args[0]
        for text in [parser.TRANSFER_GUIDANCE, parser.INVESTMENT_GUIDANCE, '"Self Transfer"']:
            self.assertIn(text, prompt)
        self.assertNotIn("own-account transfers are", prompt)


class SpendingTestCase(unittest.IsolatedAsyncioTestCase):
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
        return response.json()["transaction"]

    async def add(self, category, amount, *, type="DEBIT", merchant=None, when=NOW, user_id=None, parsed=True,
                  raw="fixture", currency="INR") -> uuid.UUID:
        self.counter += 1
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=type, amount=Decimal(str(amount)), currency=currency, merchant=merchant, category=category,
                              user_id=user_id, is_parsed=parsed, raw_message=raw, message_hash=f"fixture-{self.counter}",
                              parsed_by="regex", transaction_time=when, created_at=when, updated_at=when)
            db.add(txn)
            await db.commit()
            return txn.id

    async def notifications(self) -> list[Notification]:
        async with AsyncSessionLocal() as db:
            return list((await db.scalars(select(Notification))).all())

    async def put(self, url: str, body: dict, expected: int = 200) -> dict:
        response = await self.client.put(url, json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()


class SpendingLimitTests(SpendingTestCase):
    async def test_spent_excludes_card_bills_self_transfers_and_investments(self):
        await self.add("Food & Dining", 100, merchant="Acme")
        await self.add("Credit Card Payment", 1000)
        await self.add("Self Transfer", 2000)
        await self.add("Investments & Finance", 5000, merchant="Acme")
        await self.add("Cash Withdrawal", 300)
        await self.add("Transfer", 400)
        await self.add(None, 50)
        await self.add("Uncategorized", 20)
        await self.add("Income", 999, type="CREDIT")
        await self.put("/api/v1/limits", {"monthly_limit": 100000})
        await self.put("/api/v1/limits", {"category": "Cash Withdrawal", "monthly_limit": 100000})
        await self.put("/api/v1/limits/merchants", {"merchant": "Acme", "monthly_limit": 100000})
        limits = (await self.client.get("/api/v1/limits")).json()
        self.assertEqual(limits["monthly"]["spent"], 870)
        [cash] = limits["categories"]
        self.assertEqual((cash["category"], cash["monthly"]["spent"]), ("Cash Withdrawal", 300))
        [acme] = limits["merchants"]
        self.assertEqual((acme["merchant"], acme["monthly"]["spent"]), ("Acme", 100))

    async def test_card_bills_and_investments_never_notify(self):
        await self.put("/api/v1/limits", {"monthly_limit": 500})
        self.assertEqual((await self.sms(HDFC_CARD_BILL_PAID))["category"], "Credit Card Payment")
        self.assertEqual(await self.notifications(), [])
        self.assertEqual((await self.sms(PPF))["category"], "Investments & Finance")
        self.assertEqual(await self.notifications(), [])
        await self.sms("Sent Rs.600.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629400012345")
        [notification] = await self.notifications()
        self.assertTrue(notification.body.startswith("₹600.00 of ₹500.00"), notification.body)

    async def test_excluded_categories_cannot_have_limits(self):
        for category in sorted(EXCLUDED_FROM_SPENDING):
            response = await self.client.put("/api/v1/limits", json={"category": category, "monthly_limit": 100}, headers=ORIGIN)
            self.assertEqual(response.status_code, 422, category)
            self.assertIn("Pick a spending category, or All.", response.text)
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN, json={
            "merchant": "New Shop", "monthly_limit": 100, "category": "Self Transfer"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Pick a spending category.", response.text)

    async def test_old_limits_on_excluded_categories_are_ignored(self):
        async with AsyncSessionLocal() as db:
            db.add_all([
                CategoryLimit(user_id=self.owner.id, category="Investments & Finance", currency="INR", monthly_limit=10),
                CategoryLimit(user_id=self.owner.id, category="Credit Card Payment", currency="INR", monthly_limit=10),
            ])
            await db.commit()
        self.assertEqual((await self.client.get("/api/v1/limits")).json()["categories"], [])
        await self.sms(PPF)
        self.assertEqual(await self.notifications(), [])

    async def test_merchant_paid_only_in_excluded_categories_is_not_known(self):
        await self.add("Investments & Finance", 5000, merchant="Groww")
        known = (await self.client.get("/api/v1/limits/merchants/known")).json()
        self.assertNotIn("Groww", [m["name"] for m in known])
        preview = await self.client.get("/api/v1/limits/merchants/preview", params={"merchant": "Groww"})
        self.assertEqual(preview.json(), {"known": False, "counts": []})


class RecallTests(SpendingTestCase):
    async def test_self_transfer_you_pick_is_remembered(self):
        txn_id = await self.add("Transfer", 75000, merchant="Manas Patil", user_id=self.owner.id)
        response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json={"category": "Self Transfer"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        async with AsyncSessionLocal() as db:
            rule = await db.scalar(select(MerchantCategory).where(MerchantCategory.user_id == self.owner.id))
            self.assertEqual((rule.type, rule.category), ("DEBIT", "Self Transfer"))
            self.assertEqual(await categorize.recall_category(db, "Manas Patil", "DEBIT"), Recalled("Self Transfer", "user"))

    async def test_history_never_carries_self_transfer(self):
        await self.add("Transfer", 100, merchant="Manas P", when=datetime(2026, 10, 1, tzinfo=timezone.utc))
        await self.add("Self Transfer", 200, merchant="Manas P", when=datetime(2026, 10, 10, tzinfo=timezone.utc))
        async with AsyncSessionLocal() as db:
            self.assertEqual(await categorize.recall_category(db, "Manas P", "DEBIT"), Recalled("Transfer", "history"))

    async def test_only_self_transfer_history_recalls_nothing(self):
        await self.add("Self Transfer", 200, merchant="Manas P")
        async with AsyncSessionLocal() as db:
            self.assertIsNone(await categorize.recall_category(db, "Manas P", "DEBIT"))


class RecategoriseScriptTests(SpendingTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        owner = self.owner.id
        self.selected = [
            await self.add("Shopping", 5000, user_id=owner, raw=AMAZON_PAY_BILL),
            await self.add(None, 1249, user_id=owner, raw=HDFC_CARD_BILL_PAID),
            await self.add("Transfer", 20000, raw=NEFT_CARD_BILL),  # unassigned SMS row
        ]
        self.not_selected = [
            await self.add("Credit Card Payment", 1249, user_id=owner, raw=HDFC_CARD_BILL_PAID),
            await self.add("Shopping", 1249, user_id=owner, raw=HDFC_CARD_SPEND),
            await self.add("Shopping", 0, user_id=owner, raw=AMAZON_PAY_BILL, parsed=False),
            await self.add("Shopping", 5000, user_id=self.other.id, raw=AMAZON_PAY_BILL),
            await self.add("Credit Card Payment", 12000, type="CREDIT", user_id=owner, raw=HDFC_CARD_PAYMENT_RECEIVED),
        ]
        self.suspicious = await self.add("Credit Card Payment", 649, user_id=owner, raw=NETFLIX_AUTOPAY)
        async with AsyncSessionLocal() as db:
            self.investment_limit = CategoryLimit(user_id=owner, category="Investments & Finance", currency="INR",
                                                  monthly_limit=10000)
            self.food_limit = CategoryLimit(user_id=owner, category="Food & Dining", currency="INR", monthly_limit=5000)
            db.add_all([self.investment_limit, self.food_limit])
            await db.commit()

    async def categories(self) -> dict:
        async with AsyncSessionLocal() as db:
            return {t.id: t.category for t in (await db.scalars(select(Transaction))).all()}

    async def limit_ids(self) -> set:
        async with AsyncSessionLocal() as db:
            return set((await db.scalars(select(CategoryLimit.id))).all())

    async def run_main(self, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await script.main(["--email", "owner@example.com", *args])
        return out.getvalue()

    async def test_selectors(self):
        async with AsyncSessionLocal() as db:
            owner = await script.load_owner(db, "OWNER@example.com")
            self.assertEqual(owner.id, self.owner.id)
            self.assertEqual({t.id for t in await script.bill_debits_to_fix(db, owner)}, set(self.selected))
            self.assertEqual([t.id for t in await script.suspicious_card_payments(db, owner)], [self.suspicious])
            self.assertEqual([limit.id for limit in await script.excluded_limits(db, owner)], [self.investment_limit.id])

    async def test_apply_changes_is_idempotent(self):
        async with AsyncSessionLocal() as db:
            changed = await script.apply_changes(db, list(self.selected), [self.investment_limit.id])
        self.assertEqual(changed, (3, 1))
        async with AsyncSessionLocal() as db:
            for txn_id in self.selected:
                txn = await db.get(Transaction, txn_id)
                self.assertEqual(txn.category, "Credit Card Payment")
                self.assertGreater(txn.updated_at.replace(tzinfo=timezone.utc), NOW)
            self.assertEqual(await script.bill_debits_to_fix(db, await script.load_owner(db, "owner@example.com")), [])
        self.assertEqual(await self.limit_ids(), {self.food_limit.id})

    async def test_dry_run_changes_nothing(self):
        before = await self.categories()
        output = await self.run_main()
        self.assertIn("Dry run: nothing changed", output)
        for txn_id in [*self.selected, self.suspicious, self.investment_limit.id]:
            self.assertIn(str(txn_id), output)
        self.assertIn("Owner: owner@example.com · database host: sqlite", output)
        self.assertIn("Investments & Finance  weekly —  monthly ₹10,000.00", output)
        self.assertEqual(await self.categories(), before)
        self.assertEqual(await self.limit_ids(), {self.investment_limit.id, self.food_limit.id})

    async def test_apply_only_chosen_ids(self):
        before = await self.categories()
        output = await self.run_main("--apply", "--ids", str(self.selected[0]))
        self.assertIn("Changed 1 transactions to Credit Card Payment and deleted 1 category limits.", output)
        after = await self.categories()
        self.assertEqual(after[self.selected[0]], "Credit Card Payment")
        self.assertEqual({k: v for k, v in after.items() if k != self.selected[0]},
                         {k: v for k, v in before.items() if k != self.selected[0]})
        self.assertEqual(await self.limit_ids(), {self.food_limit.id})

    async def test_bad_arguments_change_nothing(self):
        before = await self.categories()
        with self.assertRaises(SystemExit):
            await self.run_main("--apply", "--ids", str(self.not_selected[1]))
        with self.assertRaises(SystemExit):
            await self.run_main("--ids", str(self.selected[0]))
        self.assertEqual(await self.categories(), before)
        self.assertEqual(len(await self.limit_ids()), 2)

    async def test_production_needs_yes(self):
        before = await self.categories()
        with patch.object(script, "is_local", return_value=False), patch("builtins.input", return_value="no"):
            with self.assertRaises(SystemExit):
                await self.run_main("--apply")
        self.assertEqual(await self.categories(), before)
        with patch.object(script, "is_local", return_value=False), patch("builtins.input", return_value="yes"):
            output = await self.run_main("--apply")
        self.assertIn("Changed 3 transactions", output)
        after = await self.categories()
        self.assertTrue(all(after[txn_id] == "Credit Card Payment" for txn_id in self.selected))

    def test_is_local(self):
        for url in ["sqlite+aiosqlite:///:memory:", "postgresql+asyncpg://u:p@localhost:5432/x",
                    "postgresql+asyncpg://u:p@127.0.0.1/x"]:
            self.assertTrue(script.is_local(url), url)
        self.assertFalse(script.is_local("mysql+aiomysql://u:p@gateway01.example.com:4000/x"))


if __name__ == "__main__":
    unittest.main()
