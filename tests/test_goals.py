"""Savings goals (goals.py): tag normalisation, the API, one active goal per tag. Isolated in-memory SQLite."""
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
import unicodedata
import unittest
from datetime import datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import goals
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import SavingsGoal, User
from file_db import use_file_db

ORIGIN = {"Origin": "http://localhost:3000"}
BAD_ORIGIN = {"Origin": "https://evil.example"}


class CleanTagTests(unittest.TestCase):
    def test_examples(self):
        # The same examples as doc 9 / scripts/check-tags.mjs.
        good = {
            "#goa": "goa",
            "#Goa": "goa",
            "goa": "goa",
            " #Goa ": "goa",
            "#दिल्ली": "दिल्ली",
            "#2024-goa": "2024-goa",
            "#trip_2027": "trip_2027",
            "#" + "a" * 30: "a" * 30,
            "#ÉTÉ": "été",
            unicodedata.normalize("NFD", "#café"): unicodedata.normalize("NFC", "café"),
        }
        for value, expected in good.items():
            with self.subTest(value=value):
                self.assertEqual(goals.clean_tag(value), expected)
        bad = {
            "#2024": "A tag needs at least one letter.",
            "#" + "a" * 31: "Tags can be up to 30 characters.",
            # 20 code points, but "İ" lower-cases to two ("i̇"): 40 stored, more than String(30) holds.
            "#" + "İ" * 20: "Tags can be up to 30 characters.",
            "ph one": "Tags use letters, digits, _ and -.",
            "#goa!": "Tags use letters, digits, _ and -.",
            "##goa": "Tags use letters, digits, _ and -.",
            "#": "Enter a tag.",
            "  ": "Enter a tag.",
        }
        for value, message in bad.items():
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    goals.clean_tag(value)
                self.assertEqual(str(caught.exception), message)


class GoalApiTests(unittest.IsolatedAsyncioTestCase):
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
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(app.dependency_overrides.clear)
        self.sign_in()

    def sign_in(self, user=None):
        app.dependency_overrides[current_user] = lambda: user or self.owner

    def goal(self, **fields):
        return {"name": "New phone", "target": "60000", "target_month": "2027-03", "tag": "#Phone", **fields}

    async def create(self, status=201, **fields):
        response = await self.client.post("/api/v1/goals", json=self.goal(**fields), headers=ORIGIN)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def update(self, goal_id, status=200, headers=ORIGIN, **fields):
        response = await self.client.put(f"/api/v1/goals/{goal_id}", json=self.goal(**fields), headers=headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def listed(self):
        response = await self.client.get("/api/v1/goals")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def detail(self, response_json):
        detail = response_json["detail"]
        return detail[0]["msg"].removeprefix("Value error, ") if isinstance(detail, list) else detail

    async def test_create_and_one_active_goal_per_tag(self):
        phone = await self.create()
        self.assertEqual({k: phone[k] for k in ("name", "target", "target_month", "tag", "manual_saved", "archived")},
                         {"name": "New phone", "target": 60000.0, "target_month": "2027-03", "tag": "phone",
                          "manual_saved": 0.0, "archived": False})
        self.assertEqual(datetime.fromisoformat(phone["created_at"]).utcoffset().total_seconds(), 0)
        refused = await self.create(409, name="Another phone", tag="phone")
        self.assertEqual(refused["detail"], "Another goal uses #phone.")
        await self.update(phone["id"], archived=True)
        second = await self.create(name="Phone again", tag="PHONE")
        self.assertEqual(second["tag"], "phone")
        refused = await self.update(phone["id"], 409, archived=False)
        self.assertEqual(refused["detail"], "Another goal uses #phone.")
        await self.update(second["id"], name="Phone, renamed")  # its own tag isn't "another goal"
        await self.create(name="Archived twin", tag="phone", archived=True)  # archived goals may share it

    async def test_validation(self):
        phone = await self.create()
        updated = await self.update(phone["id"], manual_saved=5000)
        self.assertEqual(updated["manual_saved"], 5000.0)
        self.assertEqual(updated["target_month"], "2027-03")
        cases = [
            ({"manual_saved": -1}, "Saved so far can't be below zero."),
            ({"target": 0}, "Enter a target above zero."),
            ({"target": "1.005"}, "Enter a target above zero."),
            ({"target_month": "2027-13"}, "Pick a month like 2027-03."),
            ({"target_month": "2027-3"}, "Pick a month like 2027-03."),
            ({"name": "   "}, "Enter a name."),
            ({"name": "n" * 61}, "Names can be up to 60 characters."),
            ({"name": "a\x00b"}, "Names can't contain invalid characters."),
            ({"tag": "#2024"}, "A tag needs at least one letter."),
            ({"tag": "#"}, "Enter a tag."),
        ]
        for fields, message in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.detail(await self.update(phone["id"], 422, **fields)), message)
        cleared = await self.update(phone["id"], target_month=None)
        self.assertEqual((cleared["target_month"], cleared["manual_saved"]), (None, 0.0))  # PUT replaces every field

    async def test_a_tag_that_grows_when_lower_cased_is_a_422(self):
        refused = await self.create(422, tag="#" + "İ" * 20)
        self.assertEqual(self.detail(refused), "Tags can be up to 30 characters.")
        self.assertEqual((await self.create(tag="#" + "İ" * 15))["tag"], "i̇" * 15)  # 30 once lower-cased

    async def file_db(self):
        """A file database (use_file_db) with the owner in it, for requests that really run side by side."""
        sessions = await use_file_db(self)
        async with sessions() as db:
            db.add(User(id=self.owner.id, email=self.owner.email, name=self.owner.name))
            await db.commit()
        return sessions

    async def active_tags(self, sessions):
        async with sessions() as db:
            return sorted((await db.scalars(select(SavingsGoal.tag).where(SavingsGoal.archived.is_(False)))).all())

    async def test_a_double_submit_makes_one_goal(self):
        sessions = await self.file_db()
        responses = await asyncio.gather(*(self.client.post("/api/v1/goals", json=self.goal(tag="#race"),
                                                            headers=ORIGIN) for _ in range(3)))
        self.assertEqual(sorted(r.status_code for r in responses), [201, 409, 409], [r.text for r in responses])
        self.assertEqual({r.json()["detail"] for r in responses if r.status_code == 409}, {"Another goal uses #race."})
        self.assertEqual(await self.active_tags(sessions), ["race"])

    async def test_unarchiving_two_at_once_keeps_one_active(self):
        sessions = await self.file_db()
        first = await self.create(tag="#trip", archived=True)
        second = await self.create(tag="#trip", archived=True)
        responses = await asyncio.gather(*(self.client.put(f"/api/v1/goals/{goal['id']}", json=self.goal(tag="#trip"),
                                                           headers=ORIGIN) for goal in (first, second)))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409], [r.text for r in responses])
        self.assertEqual(await self.active_tags(sessions), ["trip"])

    async def test_order(self):
        first = await self.create(name="First", tag="one")
        second = await self.create(name="Second", tag="two")
        third = await self.create(name="Third", tag="three")
        await self.update(first["id"], name="First", tag="one", archived=True)
        self.assertEqual([g["name"] for g in await self.listed()], ["Second", "Third", "First"])
        self.assertEqual([g["id"] for g in await self.listed()], [second["id"], third["id"], first["id"]])

    async def test_delete_ownership_and_auth(self):
        phone = await self.create()
        response = await self.client.delete(f"/api/v1/goals/{phone['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204, response.text)
        response = await self.client.delete(f"/api/v1/goals/{phone['id']}", headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Goal not found."))
        mine = await self.create()
        self.sign_in(self.other)
        self.assertEqual(await self.listed(), [])
        self.assertEqual((await self.update(mine["id"], 404))["detail"], "Goal not found.")
        response = await self.client.delete(f"/api/v1/goals/{mine['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 404)
        await self.create()  # their own #phone doesn't clash with mine
        self.sign_in()
        for request in (
            self.client.post("/api/v1/goals", json=self.goal(), headers=BAD_ORIGIN),
            self.client.put(f"/api/v1/goals/{mine['id']}", json=self.goal(), headers=BAD_ORIGIN),
            self.client.delete(f"/api/v1/goals/{mine['id']}", headers=BAD_ORIGIN),
        ):
            self.assertEqual((await request).status_code, 403)
        app.dependency_overrides.clear()
        for request in (
            self.client.get("/api/v1/goals"),
            self.client.post("/api/v1/goals", json=self.goal(), headers=ORIGIN),
            self.client.put(f"/api/v1/goals/{mine['id']}", json=self.goal(), headers=ORIGIN),
            self.client.delete(f"/api/v1/goals/{mine['id']}", headers=ORIGIN),
        ):
            self.assertEqual((await request).status_code, 401)
