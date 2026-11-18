"""The post-save runner (post_save.py): registration, order and causes, failures, skips, the query budget, the
real registry and every call site. Isolated in-memory SQLite; email stays off (no Gmail keys); Gemini is faked."""
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
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text

import limits
import main
import post_save
import settings_store
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import DUPLICATE_PENDING, DuplicateFlag, Notification, Transaction, User
from notifications import create_once

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)  # Wed 21 Oct, 12:00 IST
MINUTE = timedelta(minutes=1)
ALL_CAUSES = {"sms", "manual", "edit", "ai_retry", "restored"}

SWIGGY_UPI = "Sent Rs.500.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629400100001"
SWIGGY_DEBIT = ("Rs.500.00 debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA swiggy@icici (UPI Ref No 629400100001). "
                "Not you? Call 18002586161")
DMART = ("Rs.1200.00 debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA dmart@ybl (UPI Ref No 629400100777). "
         "Not you? Call 18002586161")


class PostSaveTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared by tests/test_payment_alerts.py; has no tests of its own."""

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

    async def sms(self, message: str, when: datetime = NOW) -> dict:
        response = await self.client.post("/api/v1/webhook/sms", json={"message": message, "received_at": when.isoformat()},
                                          headers=HOOK)
        self.assertEqual(response.json()["status"], "success", response.text)
        return response.json()["transaction"]

    async def manual(self, amount, merchant: str, when: datetime = NOW, type: str = "DEBIT") -> dict:
        response = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
            "type": type, "amount": str(amount), "merchant": merchant, "transaction_time": when.isoformat(),
            "category": "Shopping"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def settings(self, **changes) -> dict:
        response = await self.client.put("/api/v1/settings", json={k.replace("__", "."): v for k, v in changes.items()},
                                         headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def seed(self, merchant="Swiggy", amount=500, when=NOW, *, category="Food & Dining", currency="INR",
                   type="DEBIT", is_parsed=True, user_id=None, parsed_by="regex") -> uuid.UUID:
        self.counter += 1
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=type, amount=Decimal(str(amount)), currency=currency, merchant=merchant,
                              category=category, user_id=user_id, is_parsed=is_parsed, parsed_by=parsed_by,
                              raw_message="fixture", message_hash=f"seed-{self.counter}", transaction_time=when,
                              created_at=NOW + timedelta(seconds=self.counter), updated_at=NOW)
            db.add(txn)
            await db.commit()
            return txn.id

    async def flag(self, transaction_id, original_id) -> None:
        async with AsyncSessionLocal() as db:
            db.add(DuplicateFlag(transaction_id=transaction_id, original_id=original_id, user_id=self.owner.id,
                                 reason="reference", status=DUPLICATE_PENDING, created_at=NOW))
            await db.commit()

    async def notifications(self, kind=None) -> list[Notification]:
        async with AsyncSessionLocal() as db:
            query = select(Notification).order_by(Notification.created_at)
            if kind:
                query = query.where(Notification.kind == kind)
            return list(await db.scalars(query))

    async def count_statements(self, action) -> int:
        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await action()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        return len(statements)


class RegisterTests(unittest.TestCase):
    def test_unknown_cause_and_duplicate_name(self):
        async def check(ctx):
            pass

        with patch.object(post_save, "_CHECKS", []):
            with self.assertRaisesRegex(ValueError, "saved"):
                post_save.register("x", check, {"sms", "saved"})
            post_save.register("x", check, {"sms"})
            with self.assertRaisesRegex(ValueError, "already registered"):
                post_save.register("x", check, {"edit"})
            self.assertEqual(post_save._CHECKS, [("x", check, frozenset({"sms"}))])

    def test_the_real_registry(self):
        self.assertIs(main.post_save, post_save)
        self.assertEqual([(name, set(causes)) for name, _, causes in post_save._CHECKS], [
            ("limits", ALL_CAUSES),
            ("payment_alerts", ALL_CAUSES - {"edit"}),
        ])
        self.assertIs(post_save._CHECKS[0][1], limits.post_save_check)
        self.assertEqual(post_save.CAUSES, ("sms", "manual", "edit", "ai_retry", "restored"))


class RunnerTests(PostSaveTestCase):
    def registry(self):
        registry = patch.object(post_save, "_CHECKS", [])
        registry.start()
        self.addCleanup(registry.stop)

    async def test_checks_run_for_their_causes_in_registration_order(self):
        self.registry()
        await self.settings(alerts__unusual=False)
        txn_id = await self.seed()
        seen = []

        def recorder(name):
            async def check(ctx):
                seen.append((name, ctx.cause, ctx.txn.id, ctx.owner.email, ctx.settings))
            return check

        post_save.register("a", recorder("a"), {"sms", "edit"})
        post_save.register("b", recorder("b"), {"manual"})
        post_save.register("c", recorder("c"), {"edit"})
        await post_save.run(txn_id, "edit")
        settings = {"alerts.large_amount": None, "alerts.unusual": False, "limits.warn_percent": 80}
        self.assertEqual(seen, [("a", "edit", txn_id, "owner@example.com", settings),
                                ("c", "edit", txn_id, "owner@example.com", settings)])
        seen.clear()
        await post_save.run(txn_id, "manual")
        self.assertEqual([name for name, *_ in seen], ["b"])
        seen.clear()
        await post_save.run(txn_id, "restored")
        self.assertEqual(seen, [])

    async def test_a_failing_check_never_stops_the_next(self):
        self.registry()
        txn_id = await self.seed()
        later = Mock()

        async def broken(ctx):
            raise RuntimeError("bug")

        async def bad_sql(ctx):
            await ctx.db.execute(text("SELECT nope FROM nowhere"))

        async def queries(ctx):
            later(await ctx.db.scalar(select(User.email).where(User.id == ctx.owner.id)))

        post_save.register("broken", broken, {"sms"})
        post_save.register("bad_sql", bad_sql, {"sms"})
        post_save.register("queries", queries, {"sms"})
        with self.assertLogs("post_save", "ERROR") as logs:
            await post_save.run(txn_id, "sms")
        self.assertIn("Post-save check broken failed", logs.output[0])
        self.assertIn("Post-save check bad_sql failed", logs.output[1])
        later.assert_called_once_with("owner@example.com")

    async def test_skips(self):
        self.registry()
        check = AsyncMock()
        post_save.register("check", check, ALL_CAUSES)
        a = await self.seed()
        b = await self.seed()
        await self.flag(b, a)
        theirs = await self.seed(user_id=self.other.id)
        await post_save.run(b, "sms")  # held out
        await post_save.run(uuid.uuid4(), "sms")  # no such transaction
        await post_save.run(theirs, "sms")  # another user's
        with patch("post_save.OWNER_EMAIL", ""):
            await post_save.run(a, "sms")  # no owner configured
        with patch("post_save.OWNER_EMAIL", "nobody@example.com"):
            await post_save.run(a, "sms")  # no owner row
        check.assert_not_awaited()
        mine = await self.seed(user_id=self.owner.id)
        await post_save.run(a, "sms")  # an unassigned SMS row
        await post_save.run(mine, "sms")
        self.assertEqual([c.args[0].txn.id for c in check.await_args_list], [a, mine])

    async def test_run_never_raises(self):
        self.registry()
        check = AsyncMock()
        post_save.register("check", check, ALL_CAUSES)
        txn_id = await self.seed()
        with patch("settings_store.load", side_effect=RuntimeError("down")), \
                self.assertLogs("post_save", "ERROR") as logs:
            await post_save.run(txn_id, "sms")
        self.assertIn("Post-save checks failed", logs.output[0])
        check.assert_not_awaited()

    async def test_four_statements(self):
        self.registry()
        txn_id = await self.seed()
        self.assertEqual(await self.count_statements(lambda: post_save.run(txn_id, "sms")), 4)

        async def load_only():
            async with AsyncSessionLocal() as db:
                self.assertIsNotNone(await post_save.load(db, txn_id, "sms"))

        self.assertEqual(await self.count_statements(load_only), 4)

    async def test_owner_and_transaction_survive_a_lost_notification_race(self):
        self.registry()
        txn_id = await self.seed(amount=321)
        read = []

        async def check(ctx):
            await create_once(ctx.db, ctx.owner.id, kind="test", title="t", body="b", link=None, dedupe_key="k")
            # A simultaneous SMS made it first: the existence check misses it and the commit fails.
            with patch.object(ctx.db, "scalar", AsyncMock(return_value=None)):
                created = await create_once(ctx.db, ctx.owner.id, kind="test", title="t", body="b", link=None,
                                            dedupe_key="k")
            read.append((created, ctx.txn.amount, ctx.txn.merchant, ctx.owner.email))

        post_save.register("check", check, {"sms"})
        await post_save.run(txn_id, "sms")
        self.assertEqual(read, [(False, Decimal("321.00"), "Swiggy", "owner@example.com")])
        self.assertEqual(len(await self.notifications("test")), 1)

    async def test_ctx_is_frozen(self):
        txn_id = await self.seed()
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, txn_id, "sms")
            with self.assertRaises(Exception):
                ctx.cause = "edit"
            self.assertEqual(ctx.settings, await settings_store.load(db, self.owner.id))


class CallSiteTests(PostSaveTestCase):
    def runs(self, mock) -> list[tuple[str, str]]:
        return [(str(c.args[0]), c.args[1]) for c in mock.await_args_list]

    async def test_webhook_and_hand_add(self):
        with patch("post_save.run", new=AsyncMock()) as run:
            sms = await self.sms(DMART)
            hand = await self.manual(700, "Croma", NOW - 3 * 24 * 60 * MINUTE)
        self.assertEqual(self.runs(run), [(sms["id"], "sms"), (hand["id"], "manual")])

    async def test_edits(self):
        debit = await self.seed()
        credit = await self.seed(type="CREDIT", category="Refunds & Cashback")
        unparsed = await self.seed(is_parsed=False, amount=0)
        with patch("post_save.run", new=AsyncMock()) as run:
            for txn_id, body in ((debit, {"notes": "lunch"}), (credit, {"amount": "900"}), (debit, {"amount": "900"})):
                response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json=body, headers=ORIGIN)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.runs(run), [(str(debit), "edit")])
        with patch("post_save.run", new=AsyncMock()) as run:
            response = await self.client.patch(f"/api/v1/transactions/{unparsed}", json={"amount": "250"}, headers=ORIGIN)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.runs(run), [(str(unparsed), "edit")])  # giving it an amount makes it count

    async def test_duplicates(self):
        with patch("post_save.run", new=AsyncMock()) as run:
            first = await self.sms(SWIGGY_UPI)
            second = await self.sms(SWIGGY_DEBIT, NOW + MINUTE)
            self.assertEqual(second["duplicate_of"], first["id"])
            self.assertEqual(self.runs(run), [(first["id"], "sms")])  # the flagged SMS gets no checks
            response = await self.client.post(f"/api/v1/transactions/{second['id']}/not-duplicate", headers=ORIGIN)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.runs(run)[-1], (second["id"], "restored"))

    async def test_deleting_the_original_restores_the_held_out_row(self):
        with patch("post_save.run", new=AsyncMock()) as run:
            first = await self.sms(SWIGGY_UPI)
            second = await self.sms(SWIGGY_DEBIT, NOW + MINUTE)
            response = await self.client.delete(f"/api/v1/transactions/{first['id']}", headers=ORIGIN)
            self.assertEqual(response.status_code, 204)
        self.assertEqual(self.runs(run), [(first["id"], "sms"), (second["id"], "restored")])

    async def test_ai_retry(self):
        message = "Sent Rs.150.00\nFrom HDFC Bank A/C *4821\nTo QWIKLY HUB\nOn 21/10/26\nRef 629400099001"
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("categorize.RETRY_DELAYS", (0,)), \
                patch("parser.categorize_with_gemini", new_callable=AsyncMock, side_effect=TimeoutError), \
                patch("categorize.categorize_with_gemini", new_callable=AsyncMock, return_value="Food & Dining"), \
                patch("post_save.run", new=AsyncMock()) as run:
            sms = await self.sms(message)
        self.assertEqual(sms["category"], "Uncategorized")
        self.assertEqual(self.runs(run), [(sms["id"], "sms"), (sms["id"], "ai_retry")])

    async def test_check_spend_limits_still_works_alone(self):
        await self.client.put("/api/v1/limits", json={"monthly_limit": 1}, headers=ORIGIN)
        await self.settings(alerts__large_amount=1)
        a = await self.seed(amount=500)
        b = await self.seed(amount=500)
        await self.flag(b, a)
        await limits.check_spend_limits(b)
        self.assertEqual(await self.notifications(), [])
        await limits.check_spend_limits(a)
        [notification] = await self.notifications()
        self.assertEqual(notification.kind, "spend_limit")  # limits only: no payment alert
        self.assertTrue(notification.body.startswith("₹500.00 of ₹1.00"), notification.body)


if __name__ == "__main__":
    unittest.main()
