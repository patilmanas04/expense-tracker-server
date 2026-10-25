"""In-app notifications for spend limits. In-memory SQLite; email stays off (no Gmail keys)."""
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
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import Notification, SpendAlert, User

ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)  # Wed 21 Oct, 12:00 IST


class NotificationTests(unittest.IsolatedAsyncioTestCase):
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

    async def set_limits(self, weekly=None, monthly=None):
        response = await self.client.put("/api/v1/limits", json={"weekly_limit": weekly, "monthly_limit": monthly}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)

    async def debit(self, amount, when=NOW, merchant="SWIGGY"):
        self.counter += 1
        message = f"Sent Rs.{amount}\nFrom HDFC Bank A/C *4821\nTo {merchant}\nOn 21/10/26\nRef 6294000{self.counter:05d}"
        response = await self.client.post("/api/v1/webhook/sms", json={"message": message, "received_at": when.isoformat()}, headers=HOOK)
        self.assertEqual(response.json()["status"], "success", response.text)

    async def inbox(self):
        response = await self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_reaching_a_limit_creates_one_unread_notification_without_email(self):
        await self.set_limits(weekly="500")
        await self.debit("300.00")
        self.assertEqual(await self.inbox(), {"unread": 0, "items": []})
        await self.debit("250.00")
        inbox = await self.inbox()
        self.assertEqual(inbox["unread"], 1)
        [item] = inbox["items"]
        self.assertEqual((item["kind"], item["title"], item["link"], item["read"]),
                         ("spend_limit", "Weekly spend limit crossed", "/settings#limits", False))
        for expected in ["₹550.00", "₹500.00", "110%", "19 Oct", "25 Oct"]:
            self.assertIn(expected, item["body"])
        await self.debit("100.00")  # already notified this week
        self.assertEqual(len((await self.inbox())["items"]), 1)
        async with AsyncSessionLocal() as db:
            self.assertEqual((await db.scalars(select(SpendAlert))).all(), [])  # email is on hold
        limits = (await self.client.get("/api/v1/limits")).json()
        self.assertTrue(limits["weekly"]["alerted"])

    async def test_touching_the_limit_exactly_and_monthly_limits(self):
        await self.set_limits(weekly="5000", monthly="500")
        await self.debit("500.00")
        [item] = (await self.inbox())["items"]
        self.assertEqual(item["title"], "Monthly spend limit reached")
        self.assertIn("this month", item["body"])

    async def test_raising_the_limit_notifies_again_newest_first(self):
        await self.set_limits(weekly="500")
        await self.debit("600.00")
        await self.set_limits(weekly="1000")
        await self.debit("500.00")
        items = (await self.inbox())["items"]
        self.assertEqual(len(items), 2)
        self.assertIn("₹1,000.00", items[0]["body"])  # newest first
        self.assertIn("₹500.00", items[1]["body"])

    async def test_credits_and_late_sms_never_notify(self):
        await self.set_limits(weekly="100")
        credit = await self.client.post("/api/v1/webhook/sms", headers=HOOK, json={
            "message": "Credit Alert!\nRs.5,000.00 credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA a@okaxis (UPI 629400099999)",
            "received_at": NOW.isoformat()})
        self.assertEqual(credit.json()["status"], "success")
        await self.debit("900.00", when=NOW - timedelta(days=7))
        self.assertEqual(await self.inbox(), {"unread": 0, "items": []})

    async def test_mark_read_by_id_or_all(self):
        await self.set_limits(weekly="100", monthly="200")
        await self.debit("300.00")
        items = (await self.inbox())["items"]
        self.assertEqual(len(items), 2)
        one = await self.client.post("/api/v1/notifications/read", json={"ids": [items[0]["id"]]}, headers=ORIGIN)
        self.assertEqual(one.json(), {"unread": 1})
        inbox = await self.inbox()
        self.assertEqual([i["read"] for i in inbox["items"]], [True, False])
        everything = await self.client.post("/api/v1/notifications/read", json={}, headers=ORIGIN)
        self.assertEqual(everything.json(), {"unread": 0})

    async def test_notifications_are_private_and_writes_need_same_origin(self):
        async with AsyncSessionLocal() as db:
            db.add(Notification(user_id=self.other.id, kind="spend_limit", title="Someone else's", body="x",
                                link="/settings#limits", dedupe_key="other", created_at=NOW))
            await db.commit()
            theirs = (await db.scalar(select(Notification))).id
        self.assertEqual(await self.inbox(), {"unread": 0, "items": []})
        await self.client.post("/api/v1/notifications/read", json={"ids": [str(theirs)]}, headers=ORIGIN)
        async with AsyncSessionLocal() as db:
            self.assertIsNone((await db.get(Notification, theirs)).read_at)
        foreign = await self.client.post("/api/v1/notifications/read", json={}, headers={"Origin": "https://attacker.example"})
        self.assertEqual(foreign.status_code, 403)
        app.dependency_overrides.clear()
        self.assertEqual((await self.client.get("/api/v1/notifications")).status_code, 401)

    async def test_swipe_away_one_and_clear_all(self):
        await self.set_limits(weekly="100", monthly="200")
        await self.debit("300.00")
        first, second = (await self.inbox())["items"]
        gone = await self.client.delete(f"/api/v1/notifications/{first['id']}", headers=ORIGIN)
        self.assertEqual(gone.status_code, 204)
        inbox = await self.inbox()
        self.assertEqual(([i["id"] for i in inbox["items"]], inbox["unread"]), ([second["id"]], 1))
        again = await self.client.delete(f"/api/v1/notifications/{first['id']}", headers=ORIGIN)
        self.assertEqual(again.status_code, 204)  # already gone: nothing to do
        missing = await self.client.delete("/api/v1/notifications/00000000-0000-0000-0000-000000000000", headers=ORIGIN)
        self.assertEqual(missing.status_code, 404)
        cleared = await self.client.delete("/api/v1/notifications", headers=ORIGIN)
        self.assertEqual(cleared.status_code, 204)
        self.assertEqual(await self.inbox(), {"unread": 0, "items": []})

    async def test_cleared_alerts_are_not_sent_again(self):
        await self.set_limits(weekly="100")
        await self.debit("300.00")
        await self.client.delete("/api/v1/notifications", headers=ORIGIN)
        await self.debit("50.00")  # still over the same limit: no repeat
        self.assertEqual(await self.inbox(), {"unread": 0, "items": []})
        limits = (await self.client.get("/api/v1/limits")).json()
        self.assertTrue(limits["weekly"]["alerted"])  # the meter still says you were notified
        await self.set_limits(weekly="400")  # a new limit, crossed: that's a new alert
        await self.debit("60.00")
        self.assertEqual([i["title"] for i in (await self.inbox())["items"]], ["Weekly spend limit crossed"])

    async def test_clearing_is_private_and_needs_same_origin(self):
        async with AsyncSessionLocal() as db:
            db.add(Notification(user_id=self.other.id, kind="spend_limit", title="Someone else's", body="x",
                                link="/settings#limits", dedupe_key="other", created_at=NOW))
            await db.commit()
            theirs = (await db.scalar(select(Notification))).id
        self.assertEqual((await self.client.delete(f"/api/v1/notifications/{theirs}", headers=ORIGIN)).status_code, 404)
        await self.client.delete("/api/v1/notifications", headers=ORIGIN)
        app.dependency_overrides[current_user] = lambda: self.other
        self.assertEqual(len((await self.inbox())["items"]), 1)  # theirs untouched by my Clear
        foreign = {"Origin": "https://attacker.example"}
        self.assertEqual((await self.client.delete("/api/v1/notifications", headers=foreign)).status_code, 403)
        self.assertEqual((await self.client.delete(f"/api/v1/notifications/{theirs}", headers=foreign)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
