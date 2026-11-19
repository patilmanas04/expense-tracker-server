"""Custom categories (categories.py): the API, using them on transactions, rules and limits, and delete-with-move.
Isolated in-memory SQLite; GEMINI_API_KEY is empty, so nothing reaches Gemini, and no real mail is sent."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import categories
import parser
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import (CategoryLimit, CategoryRule, CustomCategory, LimitRollover, MerchantCategory, Notification,
                    Transaction, User)

ORIGIN = {"Origin": "http://localhost:3000"}
BAD_ORIGIN = {"Origin": "https://evil.example"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
IST = timezone(timedelta(hours=5, minutes=30))
# Wednesday 18 Nov 2026, 12:00 IST.
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=IST).astimezone(timezone.utc)
VET = "Happy Paws Vet"


class NameTests(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(categories.clean_name("  Kids'  school (fees) "), "Kids' school (fees)")
        self.assertEqual(categories.clean_name("Pets & Vet / Care, Inc."), "Pets & Vet / Care, Inc.")
        self.assertEqual(categories.clean_name("पालतू"), "पालतू")
        self.assertEqual(categories.clean_name("2027"), "2027")
        for value, message in [("   ", "Enter a name."), ("x" * 41, "Names can be up to 40 characters."),
                               ("Pets: vet", "Use letters, digits, spaces and & - ' . , ( ) /."),
                               ("&&", "Use letters, digits, spaces and & - ' . , ( ) /."),
                               ("Pe\x00ts", "Use letters, digits, spaces and & - ' . , ( ) /.")]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    categories.clean_name(value)
                self.assertEqual(str(caught.exception), message)

    def test_canonical(self):
        self.assertEqual(categories.canonical(" groceries ", ["Pets"]), "Groceries")
        self.assertEqual(categories.canonical("PETS", ["Pets"]), "Pets")
        self.assertEqual(categories.canonical("uncategorized"), "Uncategorized")
        self.assertIsNone(categories.canonical("Pets"))
        self.assertIsNone(categories.canonical(""))

    def test_parser_list_is_unchanged(self):
        self.assertEqual(len(parser.CATEGORIES), 19)
        self.assertNotIn("Pets", parser.CATEGORIES)


class ClientCopyTests(unittest.TestCase):
    """The app draws custom categories with the same icons and colours (lib/categories.ts)."""

    def setUp(self):
        path = Path(__file__).resolve().parents[2] / "expense-tracker-client" / "lib" / "categories.ts"
        if not path.exists():
            self.skipTest("expense-tracker-client isn't next to this repo")
        self.text = path.read_text(encoding="utf-8")

    def keys(self, name: str) -> list[str]:
        block = re.search(rf"export const {name}\b[^=]*=\s*\{{(.*?)\n\}}", self.text, re.S)
        self.assertIsNotNone(block, f"lib/categories.ts has no `export const {name} = {{...}}`")
        return re.findall(r"^\s*([A-Za-z]+)\s*[:,]", block.group(1), re.M)

    def test_icons_and_colours_match(self):
        self.assertEqual(self.keys("CUSTOM_ICONS"), list(categories.ICONS))
        self.assertEqual(self.keys("PALETTE"), list(categories.COLOURS))


class CustomCategoryTests(unittest.IsolatedAsyncioTestCase):
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
        self.addCleanup(clock.stop)
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(app.dependency_overrides.clear)
        self.counter = 0
        self.sign_in()

    def sign_in(self, user=None):
        app.dependency_overrides[current_user] = lambda: user or self.owner

    def detail(self, response):
        detail = response.json()["detail"]
        return detail[0]["msg"].removeprefix("Value error, ") if isinstance(detail, list) else detail

    async def create(self, name="Pets", icon="PawPrint", colour="orange", status=201):
        response = await self.client.post("/api/v1/categories", json={"name": name, "icon": icon, "colour": colour},
                                          headers=ORIGIN)
        self.assertEqual(response.status_code, status, response.text)
        return response

    async def sms(self, amount, merchant=VET, when=NOW):
        self.counter += 1
        message = f"Sent Rs.{amount}\nFrom HDFC Bank A/C *4821\nTo {merchant}\nOn 18/11/26\nRef 6294000{self.counter:05d}"
        response = await self.client.post("/api/v1/webhook/sms", json={"message": message, "received_at": when.isoformat()},
                                          headers=HOOK)
        self.assertEqual(response.json()["status"], "success", response.text)
        return response.json()["transaction"]

    async def patch_txn(self, txn_id, status=200, **fields):
        response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json=fields, headers=ORIGIN)
        self.assertEqual(response.status_code, status, response.text)
        return response

    async def add(self, category, status=201, merchant="Pet shop"):
        response = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": "DEBIT", "amount": "300", "merchant": merchant, "category": category,
            "transaction_time": (NOW - timedelta(hours=1)).isoformat()})
        self.assertEqual(response.status_code, status, response.text)
        return response

    async def set_limits(self, status=200, **body):
        response = await self.client.put("/api/v1/limits", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, status, response.text)
        return response

    async def rows(self, model):
        async with AsyncSessionLocal() as db:
            return (await db.scalars(select(model))).all()

    async def spend_kinds(self):
        return [n.kind for n in await self.rows(Notification) if n.kind.startswith("spend_")]

    # ---------- The API ----------

    async def test_create_and_list(self):
        pets = (await self.create()).json()
        uuid.UUID(pets["id"])
        self.assertEqual({k: pets[k] for k in ("name", "icon", "colour")}, {"name": "Pets", "icon": "PawPrint", "colour": "orange"})
        await self.create("Gym", "Dumbbell", "teal")
        listed = (await self.client.get("/api/v1/categories")).json()
        self.assertEqual(listed["built_in"], parser.CATEGORIES)
        self.assertIn("Uncategorized", listed["built_in"])
        self.assertEqual([c["name"] for c in listed["custom"]], ["Pets", "Gym"])
        self.sign_in(self.other)
        self.assertEqual((await self.client.get("/api/v1/categories")).json()["custom"], [])

    async def test_rejections(self):
        await self.create()
        for name in ["groceries", "ALL", "Other", "uncategorized", "Self Transfer", "pets", " PETS "]:
            with self.subTest(name=name):
                response = await self.create(name, status=409)
                self.assertEqual(self.detail(response), "A category with this name already exists.")
        for body, message in [({"name": "Pets: vet"}, "Use letters, digits, spaces and & - ' . , ( ) /."),
                              ({"name": "x" * 41}, "Names can be up to 40 characters."),
                              ({"name": "&&"}, "Use letters, digits, spaces and & - ' . , ( ) /."),
                              ({"name": "  "}, "Enter a name."),
                              ({"name": "Cats", "icon": "Cat"}, "Pick an icon from the list."),
                              ({"name": "Cats", "colour": "#fff"}, "Pick a colour from the list.")]:
            with self.subTest(body=body):
                self.assertEqual(self.detail(await self.create(**{"name": "Cats", **body}, status=422)), message)
        for i in range(29):
            await self.create(f"Category {i}")
        response = await self.create("One too many", status=409)
        self.assertEqual(self.detail(response), "You can have up to 30 categories.")
        self.assertEqual(len(await self.rows(CustomCategory)), 30)

    async def test_put_changes_only_icon_and_colour(self):
        pets = (await self.create()).json()
        response = await self.client.put(f"/api/v1/categories/{pets['id']}", headers=ORIGIN,
                                         json={"name": "Dogs", "icon": "Dumbbell", "colour": "teal"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"id": pets["id"], "name": "Pets", "icon": "Dumbbell", "colour": "teal"})
        response = await self.client.put(f"/api/v1/categories/{pets['id']}", headers=ORIGIN,
                                         json={"icon": "Cat", "colour": "teal"})
        self.assertEqual((response.status_code, self.detail(response)), (422, "Pick an icon from the list."))
        self.sign_in(self.other)
        response = await self.client.put(f"/api/v1/categories/{pets['id']}", headers=ORIGIN,
                                         json={"icon": "Gift", "colour": "red"})
        self.assertEqual((response.status_code, self.detail(response)), (404, "Category not found."))

    async def test_lone_surrogates_are_a_422(self):
        # Raw JSON: a lone surrogate can't be sent any other way. The 422 echoes the input, so it must be encodable.
        pets = (await self.create()).json()
        json_headers = {**ORIGIN, "Content-Type": "application/json"}
        for method, url, body, message in [
            ("POST", "/api/v1/categories", '{"name": "Gym", "icon": "\\ud800", "colour": "red"}', "Pick an icon from the list."),
            ("POST", "/api/v1/categories", '{"name": "Gym", "icon": "Dumbbell", "colour": "\\ud800"}', "Pick a colour from the list."),
            ("POST", "/api/v1/categories", '{"name": "Gym\\ud800", "icon": "Dumbbell", "colour": "red"}',
             "Use letters, digits, spaces and & - ' . , ( ) /."),
            ("PUT", f"/api/v1/categories/{pets['id']}", '{"icon": "\\ud800", "colour": "red"}', "Pick an icon from the list."),
            ("PUT", f"/api/v1/categories/{pets['id']}", '{"icon": "Gift", "colour": "\\ud800"}', "Pick a colour from the list."),
        ]:
            with self.subTest(method=method, body=body):
                response = await self.client.request(method, url, content=body, headers=json_headers)
                self.assertEqual((response.status_code, self.detail(response)), (422, message))
        self.assertEqual([(c.name, c.icon, c.colour) for c in await self.rows(CustomCategory)], [("Pets", "PawPrint", "orange")])

    # ---------- Using one ----------

    async def test_transactions_and_merchant_memory(self):
        await self.create()
        first = await self.sms("1200.00")
        self.assertEqual(first["merchant"], VET)
        moved = (await self.patch_txn(first["id"], category="pets")).json()
        self.assertEqual(moved["category"], "Pets")
        memory = await self.rows(MerchantCategory)
        self.assertEqual([(m.merchant_key, m.category) for m in memory], [(VET.casefold(), "Pets")])
        second = await self.sms("400.00")
        self.assertEqual((second["category"], second["parsed_by"]), ("Pets", "regex+user"))
        self.assertEqual((await self.add("PETS")).json()["category"], "Pets")
        self.assertEqual((await self.add("Uncategorized", merchant="Something")).json()["category"], "Uncategorized")
        refused = await self.add("Nope", status=422)
        self.assertEqual(refused.json()["detail"], "Unknown category.")
        refused = await self.patch_txn(first["id"], status=422, category="Nope")
        self.assertEqual(refused.json()["detail"], "Unknown category.")
        self.sign_in(self.other)  # someone else's categories aren't theirs
        self.assertEqual((await self.add("Pets", status=422)).json()["detail"], "Unknown category.")

    async def test_limits(self):
        await self.create()
        await self.create("Gym", "Dumbbell", "teal")
        first = await self.sms("1200.00")
        await self.patch_txn(first["id"], category="Pets")
        summary = (await self.set_limits(category="pets", monthly_limit="3000")).json()
        await self.set_limits(category="gym", weekly_limit="500")
        await self.set_limits(category="Food & Dining", monthly_limit="9000")
        self.assertEqual([r.category for r in await self.rows(CategoryLimit)], ["Pets", "Gym", "Food & Dining"])
        self.assertEqual(summary["categories"][0]["monthly"]["spent"], 1200)
        summary = (await self.client.get("/api/v1/limits")).json()
        self.assertEqual([c["category"] for c in summary["categories"]], ["Food & Dining", "Gym", "Pets"])
        self.assertEqual(summary["categories"][2]["monthly"]["spent"], 1200)
        response = await self.client.put("/api/v1/limits/rollover", json={"category": "PETS", "enabled": True},
                                         headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r.category for r in await self.rows(LimitRollover)], ["Pets"])
        await self.sms("1300.00")  # 2,500: still under (a warning only)
        self.assertEqual(await self.spend_kinds(), ["spend_warning"])
        await self.sms("500.00")  # 3,000: reached
        reached = [n for n in await self.rows(Notification) if n.kind == "spend_limit"]
        self.assertEqual(len(reached), 1)
        self.assertIn("on Pets", reached[0].body)
        await self.sms("10.00")
        self.assertEqual(sorted(await self.spend_kinds()), ["spend_limit", "spend_warning"])
        for body, path, message in [
            ({"category": "Nope", "monthly_limit": "1"}, "/api/v1/limits", "Pick a spending category, or All."),
            ({"category": "Salary", "monthly_limit": "1"}, "/api/v1/limits", "Pick a spending category, or All."),
            ({"category": "Nope", "enabled": True}, "/api/v1/limits/rollover", "Pick a spending category, or All."),
            ({"merchant": "New Shop", "monthly_limit": "1", "category": "Nope"}, "/api/v1/limits/merchants",
             "Pick a spending category."),
        ]:
            with self.subTest(path=path, body=body):
                response = await self.client.put(path, json=body, headers=ORIGIN)
                self.assertEqual((response.status_code, response.json()["detail"]), (422, message))
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                         json={"merchant": "Bark Box", "monthly_limit": "500", "category": "pets"})
        self.assertEqual(response.status_code, 200, response.text)
        memory = {m.merchant_key: m.category for m in await self.rows(MerchantCategory)}
        self.assertEqual(memory["bark box"], "Pets")

    async def test_rules(self):
        await self.create()
        response = await self.client.post("/api/v1/rules", headers=ORIGIN,
                                          json={"merchant_words": "Paws", "category": "pets"})
        self.assertEqual(response.status_code, 201, response.text)
        rule = response.json()["rule"]
        self.assertEqual(rule["category"], "Pets")
        response = await self.client.post("/api/v1/rules/preview", headers=ORIGIN,
                                          json={"merchant_words": "Paws", "category": "PETS"})
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.put(f"/api/v1/rules/{rule['id']}", headers=ORIGIN,
                                         json={"merchant_words": "Paws", "category": "pets"})
        self.assertEqual((response.status_code, response.json()["rule"]["category"]), (200, "Pets"))
        for path in ("/api/v1/rules", "/api/v1/rules/preview"):
            response = await self.client.post(path, headers=ORIGIN, json={"merchant_words": "Paws", "category": "Nope"})
            self.assertEqual((response.status_code, response.json()["detail"]), (422, "Pick a category from the list."))
        response = await self.client.put(f"/api/v1/rules/{rule['id']}", headers=ORIGIN,
                                         json={"merchant_words": "Paws", "category": "Nope"})
        self.assertEqual((response.status_code, response.json()["detail"]), (422, "Pick a category from the list."))
        sms = await self.sms("250.00")
        self.assertEqual((sms["category"], sms["parsed_by"]), ("Pets", "regex+rule"))

    # ---------- Delete with move ----------

    async def test_delete_moves_everything(self):
        pets = (await self.create()).json()
        first = await self.sms("1200.00")
        await self.patch_txn(first["id"], category="Pets")
        second = await self.sms("300.00")
        self.assertEqual(second["category"], "Pets")
        await self.set_limits(category="Pets", monthly_limit="3000")
        await self.client.put("/api/v1/limits/rollover", json={"category": "Pets", "enabled": True}, headers=ORIGIN)
        await self.client.post("/api/v1/rules", headers=ORIGIN, json={"merchant_words": "Paws", "category": "Pets"})
        await self.client.post("/api/v1/rules", headers=ORIGIN, json={"merchant_words": "Bark", "category": "Shopping"})
        before = {t.id: t.updated_at for t in await self.rows(Transaction)}
        for move_to in ("pets", "Nope"):
            response = await self.client.delete(f"/api/v1/categories/{pets['id']}", params={"move_to": move_to},
                                                headers=ORIGIN)
            self.assertEqual((response.status_code, self.detail(response)),
                             (422, "Pick another category to move its payments to."))
        response = await self.client.delete(f"/api/v1/categories/{pets['id']}", params={"move_to": "shopping"},
                                            headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()), (200, {"moved": 2}))
        rows = await self.rows(Transaction)
        self.assertEqual({t.category for t in rows}, {"Shopping"})
        self.assertTrue(all(t.updated_at > before[t.id] for t in rows))
        self.assertEqual([m.category for m in await self.rows(MerchantCategory)], ["Shopping"])
        self.assertEqual(await self.rows(CategoryLimit), [])
        self.assertEqual(await self.rows(LimitRollover), [])
        self.assertEqual([r.category for r in await self.rows(CategoryRule)], ["Shopping"])
        self.assertEqual(await self.rows(CustomCategory), [])
        third = await self.sms("90.00")
        self.assertEqual(third["category"], "Shopping")
        response = await self.client.delete(f"/api/v1/categories/{pets['id']}", headers=ORIGIN)
        self.assertEqual((response.status_code, self.detail(response)), (404, "Category not found."))

    async def test_delete_ends_set_by_rule_on_moved_payments(self):
        pets = (await self.create()).json()
        response = await self.client.post("/api/v1/rules", headers=ORIGIN, json={"merchant_words": "Paws", "category": "Pets"})
        self.assertEqual(response.status_code, 201, response.text)
        by_rule = await self.sms("250.00")
        self.assertEqual((by_rule["category"], by_rule["parsed_by"]), ("Pets", "regex+rule"))
        other = await self.sms("80.00", merchant="Kirana Store")
        response = await self.client.delete(f"/api/v1/categories/{pets['id']}", params={"move_to": "shopping"},
                                            headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()), (200, {"moved": 1}))
        self.assertEqual(await self.rows(CategoryRule), [])
        rows = {str(t.id): t for t in await self.rows(Transaction)}
        # Its rule is gone, so the moved payment no longer says a rule set it; others keep their parsed_by.
        self.assertEqual((rows[by_rule["id"]].category, rows[by_rule["id"]].parsed_by), ("Shopping", "regex+user"))
        self.assertEqual(rows[other["id"]].parsed_by, other["parsed_by"])

    async def test_delete_to_uncategorized_forgets_the_merchant(self):
        gym = (await self.create("Gym", "Dumbbell", "teal")).json()
        await self.create("Sport")
        txn = (await self.add("Gym", merchant="Cult")).json()
        self.assertEqual([m.category for m in await self.rows(MerchantCategory)], ["Gym"])
        response = await self.client.delete(f"/api/v1/categories/{gym['id']}", headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()), (200, {"moved": 1}))
        self.assertEqual(await self.rows(MerchantCategory), [])
        self.assertEqual((await self.rows(Transaction))[0].category, "Uncategorized")
        self.assertEqual([c.name for c in await self.rows(CustomCategory)], ["Sport"])
        self.assertEqual(txn["category"], "Gym")

    async def test_delete_can_move_to_another_custom_category(self):
        gym = (await self.create("Gym", "Dumbbell", "teal")).json()
        await self.create("Sport")
        await self.add("Gym", merchant="Cult")
        response = await self.client.delete(f"/api/v1/categories/{gym['id']}", params={"move_to": "sport"},
                                            headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()), (200, {"moved": 1}))
        self.assertEqual((await self.rows(Transaction))[0].category, "Sport")
        self.assertEqual([m.category for m in await self.rows(MerchantCategory)], ["Sport"])

    async def test_auth_and_ownership(self):
        pets = (await self.create()).json()
        theirs_owner = self.other
        for request in (
            self.client.post("/api/v1/categories", json={"name": "X", "icon": "Gift", "colour": "red"}, headers=BAD_ORIGIN),
            self.client.put(f"/api/v1/categories/{pets['id']}", json={"icon": "Gift", "colour": "red"}, headers=BAD_ORIGIN),
            self.client.delete(f"/api/v1/categories/{pets['id']}", headers=BAD_ORIGIN),
        ):
            self.assertEqual((await request).status_code, 403)
        self.sign_in(theirs_owner)
        response = await self.client.delete(f"/api/v1/categories/{pets['id']}", headers=ORIGIN)
        self.assertEqual((response.status_code, self.detail(response)), (404, "Category not found."))
        app.dependency_overrides.clear()
        for request in (
            self.client.get("/api/v1/categories"),
            self.client.post("/api/v1/categories", json={"name": "X", "icon": "Gift", "colour": "red"}, headers=ORIGIN),
            self.client.put(f"/api/v1/categories/{pets['id']}", json={"icon": "Gift", "colour": "red"}, headers=ORIGIN),
            self.client.delete(f"/api/v1/categories/{pets['id']}", headers=ORIGIN),
        ):
            self.assertEqual((await request).status_code, 401)
        self.assertEqual(len(await self.rows(CustomCategory)), 1)
