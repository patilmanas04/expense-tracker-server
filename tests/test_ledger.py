"""Lend & borrow (ledger.py): splits and loans, counted amounts in limits, and the transaction guards. Isolated
in-memory SQLite; the fake Google server from test_alerts stands in for Gmail, so no real mail is sent."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateTable

import limits
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import LedgerEntry, MerchantCategory, Notification, Transaction, User
from file_db import use_file_db
from test_alerts import GoogleTestCase

ORIGIN = {"Origin": "http://localhost:3000"}
BAD_ORIGIN = {"Origin": "https://evil.example"}
IST = timezone(timedelta(hours=5, minutes=30))
# Wednesday 18 Nov 2026, 12:00 IST.
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=IST).astimezone(timezone.utc)
EARLIER = datetime(2026, 11, 10, 20, 0, tzinfo=IST)


class LedgerTests(GoogleTestCase):
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
        self.now = NOW
        for target in ("limits.utc_now", "ledger.utc_now"):
            clock = patch(target, side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(app.dependency_overrides.clear)
        self.counter = 0
        self.sign_in()

    def sign_in(self, user=None):
        app.dependency_overrides[current_user] = lambda: user or self.owner

    def tick(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    async def add(self, kind="DEBIT", amount="1800", category="Food & Dining", merchant="Barbeque Nation",
                  when=EARLIER, currency="INR", parsed=True, owner="owner") -> uuid.UUID:
        self.counter += 1
        user_id = {"owner": self.owner.id, "other": self.other.id}[owner]
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=kind, amount=amount, currency=currency, merchant=merchant, category=category,
                              is_parsed=parsed, user_id=user_id, raw_message="fixture", message_hash=f"seed-{self.counter}",
                              transaction_time=when.astimezone(timezone.utc), created_at=NOW - timedelta(days=1),
                              updated_at=NOW - timedelta(days=1))
            db.add(txn)
            await db.commit()
            return txn.id

    async def put_entries(self, txn_id, entries, status=200, headers=ORIGIN):
        body = {"entries": [{"note": None, **entry} for entry in entries]}
        response = await self.client.put(f"/api/v1/transactions/{txn_id}/ledger", json=body, headers=headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def cash(self, person, kind, amount, day=None, status=201, note=None):
        body = {"person": person, "kind": kind, "amount": amount,
                "day": (day or NOW.astimezone(IST).date()).isoformat(), "note": note}
        response = await self.client.post("/api/v1/ledger", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def ledger(self):
        response = await self.client.get("/api/v1/ledger")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["people"]

    async def balances(self):
        return {p["person"]: p["balance"] for p in await self.ledger()}

    async def summary(self):
        response = await self.client.get("/api/v1/limits")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def set_limits(self, weekly=None, monthly=None, category=None):
        body = {"weekly_limit": weekly, "monthly_limit": monthly, **({"category": category} if category else {})}
        response = await self.client.put("/api/v1/limits", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)

    async def txn_row(self, txn_id):
        rows = (await self.client.get("/api/v1/transactions")).json()
        return next(row for row in rows if row["id"] == str(txn_id))

    async def rows(self, model):
        async with AsyncSessionLocal() as db:
            return (await db.scalars(select(model))).all()

    async def spend_notifications(self):
        return [n for n in await self.rows(Notification) if n.kind == "spend_limit"]

    def detail(self, response_json):
        detail = response_json["detail"]
        if isinstance(detail, list):
            return detail[0]["msg"].removeprefix("Value error, ")
        return detail

    def lent(self, person, amount, kind="lent"):
        return {"person": person, "kind": kind, "amount": amount}

    # ---------- CUJs ----------

    async def test_split_counts_only_your_share(self):
        await self.set_limits(monthly="10000")
        await self.set_limits(monthly="5000", category="Food & Dining")
        dinner = await self.add()
        saved = await self.put_entries(dinner, [self.lent("Rahul", "600"), self.lent("Priya", 600)])
        self.assertEqual(saved["linked_amount"], 1200.0)
        self.assertEqual([(e["person"], e["kind"], e["amount"]) for e in saved["entries"]],
                         [("Rahul", "lent", 600.0), ("Priya", "lent", 600.0)])
        self.assertEqual(saved["entries"][0]["day"], "2026-11-10")
        self.assertEqual(saved["entries"][0]["merchant"], "Barbeque Nation")
        self.assertEqual(saved["entries"][0]["transaction_id"], str(dinner))
        row = await self.txn_row(dinner)
        self.assertEqual((row["linked_amount"], row["category"]), (1200.0, "Food & Dining"))
        summary = await self.summary()
        self.assertEqual(summary["monthly"]["spent"], 600)
        self.assertEqual(summary["categories"][0]["monthly"]["spent"], 600)
        self.assertEqual(await self.rows(MerchantCategory), [])
        listed = (await self.client.get(f"/api/v1/transactions/{dinner}/ledger")).json()
        self.assertEqual([e["person"] for e in listed], ["Rahul", "Priya"])

    async def test_merchant_limit_counts_the_share(self):
        await self.add()
        dinner = await self.add(amount="1800", when=EARLIER + timedelta(hours=1))
        await self.put_entries(dinner, [self.lent("Rahul", "1200")])
        response = await self.client.put("/api/v1/limits/merchants", headers=ORIGIN,
                                         json={"merchant": "Barbeque Nation", "monthly_limit": "5000"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["merchants"][0]["monthly"]["spent"], 2400)
        known = (await self.client.get("/api/v1/limits/merchants/known")).json()
        self.assertEqual(known, [{"name": "Barbeque Nation", "payments": 2}])

    async def test_repayment_settles_and_orders_people(self):
        dinner = await self.add()
        await self.put_entries(dinner, [self.lent("Rahul", "600"), self.lent("Priya", "600")])
        await self.add(kind="CREDIT", amount="1000", merchant="Someone", when=EARLIER - timedelta(days=3))
        repaid = await self.add(kind="CREDIT", amount="600", merchant="Rahul K", when=EARLIER + timedelta(days=1))
        await self.put_entries(repaid, [self.lent("Rahul", "600", "repaid_to_me")])
        people = await self.ledger()
        self.assertEqual([(p["person"], p["key"], p["balance"]) for p in people],
                         [("Priya", "priya", 600.0), ("Rahul", "rahul", 0.0)])
        self.assertEqual([e["kind"] for e in people[1]["entries"]], ["repaid_to_me", "lent"])  # newest first
        self.assertEqual(people[1]["entries"][0]["day"], "2026-11-11")

    async def test_partial_repayment_counts_the_rest_as_income(self):
        dinner = await self.add()
        await self.put_entries(dinner, [self.lent("Priya", "600")])
        credit = await self.add(kind="CREDIT", amount="1000", merchant="Priya S", category="Income")
        saved = await self.put_entries(credit, [self.lent("Priya", "600", "repaid_to_me")])
        self.assertEqual(saved["linked_amount"], 600.0)
        self.assertEqual(await self.balances(), {"Priya": 0.0})

    async def test_rejections(self):
        dinner = await self.add()
        credit = await self.add(kind="CREDIT", amount="1000", merchant="Priya")
        unparsed = await self.add(amount="0", parsed=False)
        usd = await self.add(amount="50", currency="USD")
        cases = [
            (credit, [self.lent("Rahul", "100")], "On money received, use Repayment from or Borrowed from."),
            (dinner, [self.lent("Rahul", "100", "repaid_to_me")], "On a payment, use Lent to or Repaid to."),
            (dinner, [self.lent("Rahul", "1000"), self.lent("Priya", "900")], "Shares add up to more than the payment."),
            (unparsed, [self.lent("Rahul", "1")], "Only payments Folio has read can have lend & borrow entries."),
            (usd, [self.lent("Rahul", "1")], "Lend & borrow is in rupees only."),
            (dinner, [self.lent(f"P{i}", "1") for i in range(21)], "Up to 20 entries per payment."),
            (dinner, [self.lent("   ", "1")], "Enter a name."),
            (dinner, [self.lent("R" * 61, "1")], "Names can be up to 60 characters."),
            (dinner, [self.lent("Ra\x00hul", "1")], "Names can't contain invalid characters."),
            (dinner, [self.lent("Rahul", "0")], "Enter an amount above zero."),
            (dinner, [self.lent("Rahul", "1.005")], "Enter an amount above zero."),
            (dinner, [{**self.lent("Rahul", "1"), "note": "x" * 201}], "Notes can be up to 200 characters."),
        ]
        for txn_id, entries, message in cases:
            with self.subTest(message=message):
                self.assertEqual(self.detail(await self.put_entries(txn_id, entries, 422)), message)
        await self.put_entries(dinner, [self.lent("Rahul", "1000"), self.lent("Priya", "800.00")])  # equal is fine
        saved = await self.put_entries(dinner, [{**self.lent("  Rahul   K ", 20), "note": "  "}])
        self.assertEqual((saved["entries"][0]["person"], saved["entries"][0]["note"]), ("Rahul K", None))
        self.assertEqual((await self.ledger())[0]["key"], "rahul k")

    async def test_put_empty_clears_and_updated_at_moves(self):
        dinner = await self.add()
        first = await self.put_entries(dinner, [self.lent("Rahul", "600")])
        self.tick()
        second = await self.put_entries(dinner, [self.lent("Rahul", "600"), self.lent("Priya", "300")])
        self.tick()
        cleared = await self.put_entries(dinner, [])
        self.assertEqual((cleared["linked_amount"], cleared["entries"]), (0.0, []))
        self.assertLess(first["updated_at"], second["updated_at"])
        self.assertLess(second["updated_at"], cleared["updated_at"])
        self.assertEqual(datetime.fromisoformat(cleared["updated_at"]).utcoffset(), timedelta(0))  # aware UTC
        row = await self.txn_row(dinner)
        self.assertEqual(row["linked_amount"], 0)
        self.assertEqual(datetime.fromisoformat(row["updated_at"]).replace(tzinfo=timezone.utc),
                         datetime.fromisoformat(cleared["updated_at"]))
        self.assertEqual(await self.rows(LedgerEntry), [])

    async def test_edits_are_guarded_while_entries_exist(self):
        dinner = await self.add()
        await self.put_entries(dinner, [self.lent("Rahul", "600"), self.lent("Priya", "600")])

        async def patch_txn(body, status):
            response = await self.client.patch(f"/api/v1/transactions/{dinner}", json=body, headers=ORIGIN)
            self.assertEqual(response.status_code, status, response.text)
            return response.json()

        refused = await patch_txn({"amount": "1000"}, 409)
        self.assertEqual(refused["detail"], "The amount can't be less than the ₹1,200.00 in lend & borrow entries.")
        self.assertEqual((await patch_txn({"amount": "1500"}, 200))["linked_amount"], 1200.0)
        self.assertEqual((await patch_txn({"type": "CREDIT"}, 409))["detail"], "Remove its lend & borrow entries first.")
        await patch_txn({"type": "DEBIT"}, 200)
        moved = await patch_txn({"category": "Groceries"}, 200)
        self.assertEqual((moved["category"], moved["linked_amount"]), ("Groceries", 1200.0))
        self.assertEqual(len(await self.rows(LedgerEntry)), 2)

    async def test_deleting_the_transaction_removes_its_entries(self):
        dinner = await self.add()
        await self.put_entries(dinner, [self.lent("Rahul", "600")])
        await self.cash("Rahul", "lent", "500")
        response = await self.client.delete(f"/api/v1/transactions/{dinner}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204, response.text)
        entries = await self.rows(LedgerEntry)
        self.assertEqual([(e.transaction_id, e.amount) for e in entries], [(None, 500)])
        self.assertEqual(await self.balances(), {"Rahul": 500.0})

    async def test_cash_entries_group_settle_and_reject_the_future(self):
        dinner = await self.add()
        await self.put_entries(dinner, [self.lent("Rahul", "600")])
        self.tick()
        out = await self.cash("rahul ", "lent", "500", note=" taxi ")  # Rahul's spelling: see test_spelling_*
        self.assertEqual((out["person"], out["day"], out["transaction_id"], out["merchant"], out["note"], out["amount"]),
                         ("Rahul", "2026-11-18", None, None, "taxi", 500.0))
        people = await self.ledger()
        self.assertEqual([(p["person"], p["key"], p["balance"]) for p in people], [("Rahul", "rahul", 1100.0)])
        self.tick()
        await self.cash("Rahul", "repaid_to_me", 1100)
        self.assertEqual(await self.balances(), {"Rahul": 0.0})
        tomorrow = NOW.astimezone(IST).date() + timedelta(days=1)
        refused = await self.cash("Rahul", "lent", "5", day=tomorrow, status=422)
        self.assertEqual(self.detail(refused), "The date can't be in the future.")

    async def test_overpayment_turns_the_balance_around(self):
        await self.cash("Rahul", "lent", "600", day=date(2026, 11, 1))
        await self.cash("Rahul", "repaid_to_me", "800")
        self.assertEqual(await self.balances(), {"Rahul": -200.0})

    async def test_people_order(self):
        await self.cash("Asha", "lent", "100", day=date(2026, 11, 1))
        await self.cash("Asha", "repaid_to_me", "100", day=date(2026, 11, 2))
        await self.cash("Bina", "lent", "50", day=date(2026, 11, 5))
        await self.cash("Bina", "repaid_to_me", "50", day=date(2026, 11, 6))
        await self.cash("Zed", "borrowed", "300", day=date(2026, 11, 3))
        await self.cash("Carl", "lent", "300", day=date(2026, 11, 3))
        await self.cash("Dev", "lent", "1000", day=date(2026, 11, 3))
        self.assertEqual([p["person"] for p in await self.ledger()], ["Dev", "Carl", "Zed", "Bina", "Asha"])

    async def test_deleting_a_linked_entry_bumps_and_counts_again(self):
        await self.set_limits(monthly="10000")
        dinner = await self.add()
        saved = await self.put_entries(dinner, [self.lent("Rahul", "600"), self.lent("Priya", "600")])
        self.assertEqual((await self.summary())["monthly"]["spent"], 600)
        self.tick()
        response = await self.client.delete(f"/api/v1/ledger/{saved['entries'][0]['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204, response.text)
        row = await self.txn_row(dinner)
        self.assertEqual(row["linked_amount"], 600.0)
        self.assertGreater(datetime.fromisoformat(row["updated_at"]).replace(tzinfo=timezone.utc),
                           datetime.fromisoformat(saved["updated_at"]))
        self.assertEqual((await self.summary())["monthly"]["spent"], 1200)
        again = await self.client.delete(f"/api/v1/ledger/{saved['entries'][0]['id']}", headers=ORIGIN)
        self.assertEqual((again.status_code, again.json()["detail"]), (404, "Entry not found."))

    async def test_limit_check_uses_counted_amounts(self):
        await self.set_limits(monthly="1000")
        dinner = await self.add(when=NOW - timedelta(hours=1))  # inserted directly: no after-save check ran
        await self.put_entries(dinner, [self.lent("Rahul", "600"), self.lent("Priya", "600")])
        self.assertEqual(await self.spend_notifications(), [])
        await self.put_entries(dinner, [])
        notifications = await self.spend_notifications()
        self.assertEqual(len(notifications), 1)
        self.assertIn("₹1,800.00 of ₹1,000.00", notifications[0].body)
        self.assertEqual((await self.summary())["monthly"]["spent"], 1800)

    async def test_deleting_a_share_runs_the_limit_check(self):
        await self.set_limits(monthly="1000")
        dinner = await self.add(when=NOW - timedelta(hours=1))
        saved = await self.put_entries(dinner, [self.lent("Rahul", "1200")])
        self.assertEqual(await self.spend_notifications(), [])
        await self.client.delete(f"/api/v1/ledger/{saved['entries'][0]['id']}", headers=ORIGIN)
        self.assertEqual(len(await self.spend_notifications()), 1)

    async def test_auth_and_ownership(self):
        dinner = await self.add()
        theirs = await self.add(owner="other")
        saved = await self.put_entries(dinner, [self.lent("Rahul", "600")])
        entry_id = saved["entries"][0]["id"]
        cash_body = {"person": "Rahul", "kind": "lent", "amount": "5", "day": "2026-11-18"}
        put_body = {"entries": []}
        app.dependency_overrides.clear()
        requests = [
            self.client.get("/api/v1/ledger"),
            self.client.post("/api/v1/ledger", json=cash_body, headers=ORIGIN),
            self.client.delete(f"/api/v1/ledger/{entry_id}", headers=ORIGIN),
            self.client.get(f"/api/v1/transactions/{dinner}/ledger"),
            self.client.put(f"/api/v1/transactions/{dinner}/ledger", json=put_body, headers=ORIGIN),
        ]
        for request in requests:
            self.assertEqual((await request).status_code, 401)
        self.sign_in()
        for request in (
            self.client.post("/api/v1/ledger", json=cash_body, headers=BAD_ORIGIN),
            self.client.delete(f"/api/v1/ledger/{entry_id}", headers=BAD_ORIGIN),
            self.client.put(f"/api/v1/transactions/{dinner}/ledger", json=put_body, headers=BAD_ORIGIN),
        ):
            self.assertEqual((await request).status_code, 403)
        for request in (
            self.client.get(f"/api/v1/transactions/{theirs}/ledger"),
            self.client.put(f"/api/v1/transactions/{theirs}/ledger", json=put_body, headers=ORIGIN),
        ):
            response = await request
            self.assertEqual((response.status_code, response.json()["detail"]), (404, "Transaction not found."))
        self.sign_in(self.other)
        response = await self.client.delete(f"/api/v1/ledger/{entry_id}", headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Entry not found."))
        self.assertEqual(await self.ledger(), [])
        self.sign_in()
        self.assertEqual(len(await self.ledger()), 1)

    async def test_spelling_of_an_existing_person_is_kept(self):
        # E4: Rahul's share was typed "Rahul"; a cash entry typed "rahul " doesn't relabel his history.
        dinner = await self.add()
        await self.put_entries(dinner, [self.lent("Rahul", "600")])
        self.tick()
        await self.cash("rahul ", "lent", "500")
        [rahul] = await self.ledger()
        self.assertEqual((rahul["person"], rahul["balance"]), ("Rahul", 1100.0))
        self.assertEqual({e["person"] for e in rahul["entries"]}, {"Rahul"})
        # A linked entry takes it too, and so does a second spelling of a new person in one save.
        lunch = await self.add(amount="900", merchant="Truffles")
        self.tick()
        saved = await self.put_entries(lunch, [self.lent("RAHUL", "100"), self.lent("priya", "100"),
                                               self.lent("PRIYA", "100")])
        self.assertEqual([e["person"] for e in saved["entries"]], ["Rahul", "priya", "priya"])
        self.assertEqual({p["person"]: p["balance"] for p in await self.ledger()}, {"Rahul": 1200.0, "priya": 200.0})
        # Each user's people are their own.
        self.sign_in(self.other)
        self.assertEqual((await self.cash("RAHUL", "lent", "5"))["person"], "RAHUL")
        self.sign_in()
        # A person whose only entries are the ones replaced can still be renamed.
        self.tick()
        renamed = await self.put_entries(lunch, [self.lent("Rahul", "100"), self.lent("Priya", "200")])
        self.assertEqual([e["person"] for e in renamed["entries"]], ["Rahul", "Priya"])
        self.assertEqual({p["person"]: p["balance"] for p in await self.ledger()}, {"Rahul": 1200.0, "Priya": 200.0})

    async def test_lone_surrogates_are_a_422(self):
        # Raw JSON: a lone surrogate can't be sent any other way. The 422 echoes the input, so it must be encodable.
        dinner = await self.add()
        entry = '{"person": "\\ud800", "kind": "lent", "amount": "1"}'
        json_headers = {**ORIGIN, "Content-Type": "application/json"}
        for body, message in [
            ('{"entries": [%s]}' % ", ".join([entry] * 21), "Up to 20 entries per payment."),
            ('{"entries": [%s]}' % entry, "Names can't contain invalid characters."),
            ('{"entries": ["\\ud800"]}', None),
            ('{"entries": "\\ud800"}', None),
            ('{"entries": [{"person": "Rahul", "kind": "\\ud800", "amount": "1"}]}', None),
        ]:
            with self.subTest(body=body[:60]):
                response = await self.client.put(f"/api/v1/transactions/{dinner}/ledger", content=body,
                                                  headers=json_headers)
                self.assertEqual(response.status_code, 422, response.text)
                if message:
                    self.assertEqual(self.detail(response.json()), message)
        self.assertEqual(await self.rows(LedgerEntry), [])

    def test_created_at_keeps_microseconds_on_mysql(self):
        # TiDB/MySQL: a plain DATETIME drops fractional seconds, and one PUT's entries (a microsecond apart) would
        # tie and sort by their random ids. SQLite keeps microseconds, so only the DDL shows it.
        ddl = str(CreateTable(LedgerEntry.__table__).compile(dialect=mysql.dialect()))
        self.assertIn("created_at DATETIME(6) NOT NULL", ddl)
        self.assertIn("TIMESTAMP WITH TIME ZONE", str(CreateTable(LedgerEntry.__table__).compile(dialect=postgresql.dialect())))

    async def test_entries_keep_their_order_within_a_save(self):
        dinner = await self.add()
        people = ["Rahul", "Priya", "Asha", "Bina", "Carl", "Dev"]
        saved = await self.put_entries(dinner, [self.lent(name, "100") for name in people])
        self.assertEqual([e["person"] for e in saved["entries"]], people)
        listed = (await self.client.get(f"/api/v1/transactions/{dinner}/ledger")).json()
        self.assertEqual([e["person"] for e in listed], people)
        stamps = sorted(entry.created_at for entry in await self.rows(LedgerEntry))
        self.assertEqual(len(set(stamps)), len(people))

    async def test_limits_summary_is_still_one_query(self):
        await self.set_limits(weekly="500", monthly="800")
        await self.set_limits(monthly="5000", category="Food & Dining")
        dinner = await self.add(when=NOW - timedelta(hours=1))
        await self.put_entries(dinner, [self.lent("Rahul", "600")])
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
        self.assertEqual(len(statements), 3, statements)  # the limits, the pay-period setting, every meter
        self.assertEqual((summary.weekly.spent, summary.monthly.spent, summary.categories[0].monthly.spent),
                         (1200, 1200, 1200))

    # ---------- Races (a double-tapped Save, a retry) ----------

    async def file_txn(self, sessions, amount="1000"):
        """The owner and one payment in a file database (use_file_db), for requests that really run side by side."""
        async with sessions() as db:
            db.add(User(id=self.owner.id, email=self.owner.email, name=self.owner.name))
            txn = Transaction(type="DEBIT", amount=amount, currency="INR", merchant="Barbeque Nation",
                              category="Food & Dining", is_parsed=True, user_id=self.owner.id, raw_message="fixture",
                              message_hash="race", transaction_time=EARLIER.astimezone(timezone.utc),
                              created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1))
            db.add(txn)
            await db.commit()
            return txn.id

    async def test_saves_at_once_replace_each_other(self):
        sessions = await use_file_db(self)
        dinner = await self.file_txn(sessions)
        bodies = [{"entries": [{"person": person, "kind": "lent", "amount": "600"}]} for person in "ABAB"]
        responses = await asyncio.gather(*(self.client.put(f"/api/v1/transactions/{dinner}/ledger", json=body,
                                                           headers=ORIGIN) for body in bodies))
        self.assertEqual([r.status_code for r in responses], [200] * 4, [r.text for r in responses])
        self.assertEqual({r.json()["linked_amount"] for r in responses}, {600.0})  # each replaced the one before
        async with sessions() as db:
            entries = (await db.scalars(select(LedgerEntry))).all()
            txn = await db.get(Transaction, dinner)
        self.assertEqual(len(entries), 1)
        self.assertEqual(float(txn.linked_amount), 600.0)

    async def test_a_smaller_amount_and_a_save_at_once_keep_the_shares_within_it(self):
        sessions = await use_file_db(self)
        dinner = await self.file_txn(sessions)
        put = self.client.put(f"/api/v1/transactions/{dinner}/ledger", headers=ORIGIN,
                              json={"entries": [{"person": "A", "kind": "lent", "amount": "900"}]})
        patch_ = self.client.patch(f"/api/v1/transactions/{dinner}", json={"amount": "500"}, headers=ORIGIN)
        saved, edited = await asyncio.gather(put, patch_)
        # Whichever went second sees the first: a 500 payment refuses 900 of shares, or 900 of shares refuse 500.
        self.assertIn((saved.status_code, edited.status_code), {(200, 409), (422, 200)}, [saved.text, edited.text])
        async with sessions() as db:
            txn = await db.get(Transaction, dinner)
        self.assertLessEqual(Decimal(str(txn.linked_amount)), Decimal(str(txn.amount)))

    async def test_a_save_and_an_edit_lock_the_transaction_before_checking(self):
        # On Postgres and TiDB a DELETE can't see entries another save hasn't committed yet, so without the lock two
        # saves at once both keep theirs (₹1,200 of shares on a ₹1,000 payment). The row lock comes first, and the
        # checks read the transaction after it.
        dinner = await self.add(amount="1000")

        async def statements_of(request):
            statements = []

            def record(conn, cursor, statement, *args):
                statements.append(" ".join(statement.split()[:2]))

            event.listen(engine.sync_engine, "before_cursor_execute", record)
            try:
                response = await request
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record)
            self.assertEqual(response.status_code, 200, response.text)
            return statements

        put = await statements_of(self.client.put(f"/api/v1/transactions/{dinner}/ledger", headers=ORIGIN,
                                                  json={"entries": [self.lent("A", "600")]}))
        self.assertEqual(put[:3], ["UPDATE transactions", "SELECT transactions.id,", "DELETE FROM"], put)
        patch_ = await statements_of(self.client.patch(f"/api/v1/transactions/{dinner}", json={"amount": "900"},
                                                       headers=ORIGIN))
        self.assertEqual(patch_[:3], ["SELECT transactions.id,", "UPDATE transactions", "SELECT transactions.id,"],
                         patch_)

    async def test_names_that_grow_when_case_folded(self):
        # "ß" folds to "ss": 36 of them are 72 characters of person_key (String(60)), so they're refused, not a 500.
        dinner = await self.add()
        self.assertEqual(self.detail(await self.put_entries(dinner, [self.lent("ß" * 36, "5")], 422)),
                         "Names can be up to 60 characters.")
        body = {"person": "ß" * 36, "kind": "lent", "amount": 5, "day": "2026-11-18"}
        response = await self.client.post("/api/v1/ledger", json=body, headers=ORIGIN)
        self.assertEqual((response.status_code, self.detail(response.json())),
                         (422, "Names can be up to 60 characters."))
        saved = await self.cash("Straße", "lent", 5)
        self.assertEqual(saved["person"], "Straße")
        self.assertEqual((await self.ledger())[0]["key"], "strasse")
        await self.cash("ß" * 30, "lent", 5)  # 60 once folded: the most there's room for
