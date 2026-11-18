"""Settings (settings_store.py): defaults, missing vs stored null, partial PUT, validation with nothing written,
stored garbage, the first-save race, auth and the table. Isolated in-memory SQLite; no mail, no Gemini."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError

import settings_store
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import User, UserSetting

ORIGIN = {"Origin": "http://localhost:3000"}
DEFAULTS = {"alerts.large_amount": None, "alerts.unusual": True, "limits.warn_percent": 80}
AMOUNT_ERROR = "alerts.large_amount: Enter a whole number of rupees from 1 to 1,00,00,000, or turn it off."
UNUSUAL_ERROR = "alerts.unusual: Choose on or off."


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SettingsTestCase(unittest.IsolatedAsyncioTestCase):
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
        app.dependency_overrides[current_user] = lambda: self.owner

    async def get(self):
        response = await self.client.get("/api/v1/settings")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def put(self, body, status=200, headers=ORIGIN):
        response = await self.client.put("/api/v1/settings", json=body, headers=headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def rows(self, user=None) -> list[UserSetting]:
        async with AsyncSessionLocal() as db:
            return list(await db.scalars(select(UserSetting).where(UserSetting.user_id == (user or self.owner).id)
                                         .order_by(UserSetting.key)))

    async def store(self, key, value, user=None):
        async with AsyncSessionLocal() as db:
            db.add(UserSetting(user_id=(user or self.owner).id, key=key, value=value,
                               updated_at=datetime.now(timezone.utc)))
            await db.commit()


class DefaultsTests(SettingsTestCase):
    async def test_defaults_in_keys_order(self):
        data = await self.get()
        self.assertEqual(data, DEFAULTS)
        self.assertEqual(list(data), list(settings_store.KEYS))
        async with AsyncSessionLocal() as db:
            loaded = await settings_store.load(db, self.owner.id)
            self.assertEqual(list(loaded.items()), list(DEFAULTS.items()))
            self.assertIsNone(await settings_store.get(db, self.owner.id, "alerts.large_amount"))
            self.assertIs(await settings_store.get(db, self.owner.id, "alerts.unusual"), True)
            with self.assertRaises(KeyError):
                await settings_store.get(db, self.owner.id, "nope")
        self.assertEqual(await self.rows(), [])

    async def test_missing_row_and_stored_null_differ(self):
        await self.put({"alerts.large_amount": 5000})
        self.assertEqual((await self.get())["alerts.large_amount"], 5000)
        data = await self.put({"alerts.large_amount": None})
        self.assertEqual(data, DEFAULTS)
        [row] = await self.rows()
        self.assertEqual((row.key, row.value), ("alerts.large_amount", "null"))
        self.assertIsNone((await self.get())["alerts.large_amount"])
        # A stored false for unusual (a value) vs its missing row (the default, true).
        await self.put({"alerts.unusual": False})
        self.assertIs((await self.get())["alerts.unusual"], False)
        async with AsyncSessionLocal() as db:
            await db.execute(delete(UserSetting).where(UserSetting.key == "alerts.unusual"))
            await db.commit()
            self.assertIs(await settings_store.get(db, self.owner.id, "alerts.unusual"), True)
        self.assertIs((await self.get())["alerts.unusual"], True)

    async def test_partial_put_keeps_the_other_key(self):
        await self.put({"alerts.large_amount": 5000})
        data = await self.put({"alerts.unusual": False})
        self.assertEqual(data, {"alerts.large_amount": 5000, "alerts.unusual": False, "limits.warn_percent": 80})
        self.assertEqual(list(data), list(settings_store.KEYS))
        self.assertEqual(await self.get(), data)

    async def test_a_second_put_updates_the_same_row(self):
        await self.put({"alerts.large_amount": 5000})
        [first] = await self.rows()
        await self.put({"alerts.large_amount": 7000})
        [second] = await self.rows()
        self.assertEqual((second.key, second.value), ("alerts.large_amount", "7000"))
        self.assertGreater(_utc(second.updated_at), _utc(first.updated_at))

    async def test_empty_body_writes_nothing(self):
        self.assertEqual(await self.put({}), DEFAULTS)
        self.assertEqual(await self.rows(), [])


class ValidationTests(SettingsTestCase):
    async def rejected(self, body, detail):
        data = await self.put(body, status=422)
        self.assertEqual(data, {"detail": detail})
        self.assertEqual(await self.rows(), [])

    async def test_unknown_key(self):
        await self.rejected({"alerts.nope": 1}, "Unknown setting: alerts.nope")

    async def test_bad_amounts(self):
        for value in (0, 10000001, 5000.5, "5000", True, False, -5, [5000], {"a": 1}):
            with self.subTest(value=value):
                await self.rejected({"alerts.large_amount": value}, AMOUNT_ERROR)

    async def test_bad_unusual(self):
        for value in (None, 1, 0, "yes", "true"):
            with self.subTest(value=value):
                await self.rejected({"alerts.unusual": value}, UNUSUAL_ERROR)

    async def test_nothing_is_written_when_a_later_key_is_bad(self):
        await self.rejected({"alerts.large_amount": 5000, "alerts.unusual": "yes"}, UNUSUAL_ERROR)
        await self.rejected({"alerts.unusual": False, "alerts.what": 1}, "Unknown setting: alerts.what")

    async def test_the_first_bad_key_in_request_order_is_reported(self):
        await self.rejected({"alerts.unusual": "yes", "alerts.large_amount": 0}, UNUSUAL_ERROR)
        await self.rejected({"alerts.large_amount": 0, "alerts.unusual": "yes"}, AMOUNT_ERROR)

    async def test_a_lone_surrogate_key_gets_a_readable_422(self):
        response = await self.client.put("/api/v1/settings", content='{"\\ud800x": 1}',
                                         headers={**ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(json.loads(response.content.decode("utf-8")), {"detail": "Unknown setting: ?x"})
        self.assertEqual(await self.rows(), [])

    async def test_a_long_unknown_key_is_cut_to_64(self):
        key = "k" * 100
        await self.rejected({key: 1}, f"Unknown setting: {'k' * 64}")

    async def test_accepted_amounts(self):
        for value, stored in ((1, "1"), (10000000, "10000000"), (5000.0, "5000")):
            with self.subTest(value=value):
                data = await self.put({"alerts.large_amount": value})
                self.assertEqual(data["alerts.large_amount"], int(stored))
                self.assertIs(type(data["alerts.large_amount"]), int)
                [row] = await self.rows()
                self.assertEqual(row.value, stored)

    async def test_a_body_that_is_not_an_object(self):
        for body in ([], 5, "x", None):
            with self.subTest(body=body):
                response = await self.client.put("/api/v1/settings", content=json.dumps(body),
                                                 headers={**ORIGIN, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], list)
        self.assertEqual(await self.rows(), [])

    def test_validators(self):
        amount = settings_store.KEYS["alerts.large_amount"].validate
        for value in (float("nan"), float("inf"), 0.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                amount(value)
        self.assertEqual((amount(None), amount(1), amount(2.0)), (None, 1, 2))
        strict = settings_store.whole_number(1, 5, nullable=False, message="m")
        with self.assertRaisesRegex(ValueError, "^m$"):
            strict(None)
        self.assertTrue(issubclass(settings_store.SettingError, ValueError))


class StoredValueTests(SettingsTestCase):
    async def test_garbage_falls_back_to_the_default(self):
        await self.store("alerts.large_amount", "not json")
        await self.store("alerts.unusual", '"x"')
        with self.assertLogs("settings_store", "WARNING") as logs:
            self.assertEqual(await self.get(), DEFAULTS)
        self.assertTrue(any("Ignoring stored setting alerts.large_amount" in line for line in logs.output), logs.output)
        self.assertTrue(any("Ignoring stored setting alerts.unusual" in line for line in logs.output), logs.output)
        with self.assertLogs("settings_store", "WARNING"):
            async with AsyncSessionLocal() as db:
                self.assertIs(await settings_store.get(db, self.owner.id, "alerts.unusual"), True)

    async def test_a_value_outside_todays_rules_falls_back(self):
        await self.store("alerts.large_amount", "0")
        with self.assertLogs("settings_store", "WARNING"):
            self.assertIsNone((await self.get())["alerts.large_amount"])

    async def test_an_unknown_key_row_is_ignored(self):
        await self.store("alerts.retired", "5")
        self.assertEqual(await self.get(), DEFAULTS)

    async def test_another_users_rows_are_never_read(self):
        await self.store("alerts.large_amount", "100", user=self.other)
        await self.store("alerts.unusual", "false", user=self.other)
        self.assertEqual(await self.get(), DEFAULTS)
        await self.put({"alerts.large_amount": 5000})
        self.assertEqual([(r.key, r.value) for r in await self.rows(self.other)],
                         [("alerts.large_amount", "100"), ("alerts.unusual", "false")])
        async with AsyncSessionLocal() as db:
            self.assertEqual(await settings_store.load(db, self.other.id),
                             {"alerts.large_amount": 100, "alerts.unusual": False, "limits.warn_percent": 80})


class RaceAndTableTests(SettingsTestCase):
    async def test_a_lost_first_save_race_updates_the_winner(self):
        await self.store("alerts.large_amount", "100")  # the other save won
        real = settings_store._existing
        calls = []

        async def stale_first(db, user_id, keys):
            calls.append(keys)
            return {} if len(calls) == 1 else await real(db, user_id, keys)

        with patch("settings_store._existing", side_effect=stale_first):
            data = await self.put({"alerts.large_amount": 5000})
        self.assertEqual(len(calls), 2)
        self.assertEqual(data["alerts.large_amount"], 5000)
        [row] = await self.rows()
        self.assertEqual(row.value, "5000")

    async def test_a_second_failure_raises(self):
        await self.store("alerts.large_amount", "100")
        with patch("settings_store._existing", return_value={}):
            async with AsyncSessionLocal() as db:
                with self.assertRaises(IntegrityError):
                    await settings_store.save(db, self.owner.id, {"alerts.large_amount": 5000})
        [row] = await self.rows()
        self.assertEqual(row.value, "100")

    async def test_the_table(self):
        def describe(connection):
            inspector = inspect(connection)
            return ([c["name"] for c in inspector.get_columns("user_settings")],
                    inspector.get_pk_constraint("user_settings")["constrained_columns"],
                    [(fk["referred_table"], fk["options"].get("ondelete")) for fk in inspector.get_foreign_keys("user_settings")])

        async with engine.connect() as connection:
            columns, pk, fks = await connection.run_sync(describe)
        self.assertEqual(columns, ["user_id", "key", "value", "updated_at"])
        self.assertEqual(pk, ["user_id", "key"])
        self.assertEqual(fks, [("users", "CASCADE")])
        await self.store("alerts.unusual", "true")
        with self.assertRaises(IntegrityError):
            await self.store("alerts.unusual", "false")
        await self.store("alerts.unusual", "false", user=self.other)  # the pair, not the key, is unique


class AuthTests(SettingsTestCase):
    async def test_sign_in_and_origin(self):
        app.dependency_overrides.clear()
        self.assertEqual((await self.client.get("/api/v1/settings")).status_code, 401)
        response = await self.client.put("/api/v1/settings", json={"alerts.unusual": False}, headers=ORIGIN)
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["detail"].startswith("Please sign in"), response.text)
        app.dependency_overrides[current_user] = lambda: self.owner
        response = await self.client.put("/api/v1/settings", json={"alerts.unusual": False},
                                         headers={"Origin": "https://attacker.example"})
        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request origin."}))
        response = await self.client.put("/api/v1/settings", json={"alerts.unusual": False})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(await self.rows(), [])


if __name__ == "__main__":
    unittest.main()
