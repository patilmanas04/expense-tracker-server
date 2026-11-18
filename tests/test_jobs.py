"""Daily jobs (jobs.py): the 21:00 IST slot, registration, the claim (due, skipped, forced, raced), failures,
no owner, the scheduler loop, the lifespan task and POST /api/v1/jobs/daily. Isolated in-memory SQLite; no mail,
no Gemini."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests
os.environ.pop("JOBS_SECRET", None)

import asyncio
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

import jobs
import main
from database import AsyncSessionLocal, Base, engine
from main import app
from models import JobRun, User

IST = timezone(timedelta(hours=5, minutes=30))
SLOT = datetime(2026, 11, 19, 15, 30, tzinfo=timezone.utc)  # 21:00 IST, 19 Nov
BEFORE = SLOT - timedelta(minutes=1)
AFTER = SLOT + timedelta(hours=2)
JOBS_SECRET = "test-jobs-secret"


def ist(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


class SlotTests(unittest.TestCase):
    def test_slot_edges(self):
        cases = (
            (ist(2026, 11, 19, 20, 59), ist(2026, 11, 18, 21)),
            (ist(2026, 11, 19, 21, 0), ist(2026, 11, 19, 21)),
            (ist(2026, 11, 19, 23, 30), ist(2026, 11, 19, 21)),
            (ist(2026, 11, 20, 0, 30), ist(2026, 11, 19, 21)),  # 19:00 UTC on the 19th
        )
        for moment, expected in cases:
            with self.subTest(moment=moment):
                result = jobs.slot(moment.astimezone(timezone.utc))
                self.assertEqual(result, expected)
                self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(jobs.slot(SLOT), SLOT)

    def test_register_rejects_a_repeated_or_long_name(self):
        async def job(db, owner, today):
            pass

        with patch.object(jobs, "_JOBS", []):
            jobs.register("a", job)
            with self.assertRaises(ValueError):
                jobs.register("a", job)
            with self.assertRaises(ValueError):
                jobs.register("x" * 65, job)
            jobs.register("y" * 64, job)
            self.assertEqual([name for name, _ in jobs._JOBS], ["a", "y" * 64])


class JobsTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSessionLocal() as db:
            self.owner = User(email="owner@example.com", name="Owner")
            self.other = User(email="other@example.com", name="Other")
            db.add_all([self.owner, self.other])
            await db.commit()
        self.calls = []
        registry = patch.object(jobs, "_JOBS", [])
        registry.start()
        self.addCleanup(registry.stop)

    def clock(self, moment: datetime) -> None:
        patcher = patch.object(jobs, "now", return_value=moment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorder(self, name: str, fail: bool = False):
        async def job(db, owner, today):
            self.calls.append((name, owner.email, today, id(db)))
            if fail:
                raise RuntimeError(f"{name} broke")

        return job

    def add(self, name: str, fail: bool = False) -> None:
        jobs.register(name, self.recorder(name, fail))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    async def rows(self) -> dict[str, datetime]:
        async with AsyncSessionLocal() as db:
            return {row.name: jobs._utc(row.last_run) for row in await db.scalars(select(JobRun))}


class RunDueTests(JobsTestCase):
    async def test_runs_a_never_run_job(self):
        self.add("a")
        self.clock(AFTER)
        self.assertEqual(await jobs.run_due(), ["a"])
        self.assertEqual(len(self.calls), 1)
        _, email, today, _ = self.calls[0]
        self.assertEqual((email, today), ("owner@example.com", date(2026, 11, 19)))
        self.assertEqual(await self.rows(), {"a": AFTER})

    async def test_today_is_the_ist_date(self):
        self.add("a")
        self.clock(datetime(2026, 11, 19, 19, 0, tzinfo=timezone.utc))  # 00:30 IST, 20 Nov
        await jobs.run_due()
        self.assertEqual(self.calls[0][2], date(2026, 11, 20))

    async def test_skips_a_job_already_run_since_the_last_2100(self):
        self.add("a")
        with patch.object(jobs, "now", return_value=AFTER):
            self.assertEqual(await jobs.run_due(), ["a"])
        with patch.object(jobs, "now", return_value=AFTER + timedelta(hours=1)):
            self.assertEqual(await jobs.run_due(), [])
        self.assertEqual(self.names(), ["a"])
        self.assertEqual(await self.rows(), {"a": AFTER})

    async def test_runs_again_after_the_next_2100(self):
        self.add("a")
        for moment, expected in ((BEFORE, ["a"]), (SLOT, ["a"]), (SLOT + timedelta(hours=23), []),
                                 (SLOT + timedelta(days=1), ["a"])):
            with self.subTest(moment=moment), patch.object(jobs, "now", return_value=moment):
                self.assertEqual(await jobs.run_due(), expected)
        self.assertEqual(self.names(), ["a", "a", "a"])
        self.assertEqual(await self.rows(), {"a": SLOT + timedelta(days=1)})

    async def test_force_runs_again(self):
        self.add("a")
        with patch.object(jobs, "now", return_value=AFTER):
            await jobs.run_due()
        later = AFTER + timedelta(minutes=1)
        with patch.object(jobs, "now", return_value=later):
            self.assertEqual(await jobs.run_due(), [])
            self.assertEqual(await jobs.run_due(force=True), ["a"])
        self.assertEqual(self.names(), ["a", "a"])
        self.assertEqual(await self.rows(), {"a": later})

    async def test_two_runs_at_once_run_the_job_once(self):
        self.add("a")
        self.clock(AFTER)
        results = await asyncio.gather(jobs.run_due(), jobs.run_due())
        self.assertEqual(sorted(results), [[], ["a"]])
        self.assertEqual(self.names(), ["a"])

    async def test_insert_race_skips(self):
        self.add("a")
        self.clock(AFTER)
        async with AsyncSessionLocal() as db:
            db.add(JobRun(name="a", last_run=AFTER))  # another worker inserted it after our update found nothing
            await db.commit()
        with patch.object(jobs, "_exists", AsyncMock(return_value=False)):
            self.assertEqual(await jobs.run_due(), [])
        self.assertEqual(self.calls, [])

    async def test_a_failing_job_does_not_stop_the_others(self):
        self.add("a", fail=True)
        self.add("b")
        self.clock(AFTER)
        with self.assertLogs("jobs", "ERROR") as logs:
            self.assertEqual(await jobs.run_due(), ["b"])
        self.assertIn("Daily job a failed", logs.output[0])
        self.assertEqual(self.names(), ["a", "b"])
        self.assertEqual(await self.rows(), {"a": AFTER, "b": AFTER})  # both claimed: a waits for the next slot
        self.assertEqual(await jobs.run_due(), [])
        self.assertEqual(self.names(), ["a", "b"])

    async def test_jobs_run_in_registration_order(self):
        for name in ("c", "a", "b"):
            self.add(name)
        self.clock(AFTER)
        self.assertEqual(await jobs.run_due(), ["c", "a", "b"])
        self.assertEqual(self.names(), ["c", "a", "b"])

    async def test_no_owner_runs_nothing(self):
        self.add("a")
        self.clock(AFTER)
        with patch.object(jobs, "OWNER_EMAIL", ""):
            self.assertEqual(await jobs.run_due(), [])
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.email == "owner@example.com"))
            await db.commit()
        self.assertEqual(await jobs.run_due(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(await self.rows(), {})

    async def test_each_job_gets_its_own_session(self):
        sessions = []

        async def keep(db, owner, today):
            sessions.append(db)

        jobs.register("a", keep)
        jobs.register("b", keep)
        self.clock(AFTER)
        self.assertEqual(await jobs.run_due(), ["a", "b"])
        self.assertIsNot(sessions[0], sessions[1])


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_catch_up_then_sleeps_until_2100(self):
        run_due = AsyncMock(return_value=[])
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        with patch.object(jobs, "run_due", run_due), patch.object(jobs, "_sleep", sleep), \
                patch.object(jobs, "now", return_value=BEFORE):
            with self.assertRaises(asyncio.CancelledError):
                await jobs.scheduler()
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [120, 61, 61])
        self.assertEqual(run_due.await_count, 2)

    async def test_a_failing_run_is_logged_and_the_loop_goes_on(self):
        run_due = AsyncMock(side_effect=[RuntimeError("database down"), []])
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        with patch.object(jobs, "run_due", run_due), patch.object(jobs, "_sleep", sleep), \
                patch.object(jobs, "now", return_value=BEFORE):
            with self.assertLogs("jobs", "ERROR") as logs, self.assertRaises(asyncio.CancelledError):
                await jobs.scheduler()
        self.assertIn("Daily jobs failed", logs.output[0])
        self.assertEqual(run_due.await_count, 2)
        self.assertEqual(sleep.await_count, 3)

    async def test_lifespan_starts_and_cancels_the_scheduler(self):
        started, events = asyncio.Event(), []

        async def fake_scheduler():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("cancelled")
                raise

        with patch.object(jobs, "scheduler", fake_scheduler):
            async with main.app.router.lifespan_context(main.app):
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual(events, [])
        self.assertEqual(events, ["cancelled"])


class TriggerTests(JobsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.add("a")
        self.clock(AFTER)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")
        self.addAsyncCleanup(self.client.aclose)

    async def post(self, headers=None, **params):
        return await self.client.post("/api/v1/jobs/daily", headers=headers or {}, params=params)

    async def test_not_configured(self):
        with patch.dict(os.environ, {"JOBS_SECRET": ""}):
            response = await self.post({"X-Jobs-Secret": "anything"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "JOBS_SECRET is not configured."})
        self.assertEqual(self.calls, [])

    async def test_bad_secrets(self):
        with patch.dict(os.environ, {"JOBS_SECRET": JOBS_SECRET}):
            for headers in ({}, {"X-Jobs-Secret": "wrong"}, {"X-Jobs-Secret": "test-webhook-secret"},
                            {"X-Webhook-Secret": "test-webhook-secret"}, {"X-Jobs-Secret": ""}):
                with self.subTest(headers=headers):
                    response = await self.post(headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json(), {"detail": "Invalid or missing jobs secret."})
        self.assertEqual(self.calls, [])

    async def test_the_webhook_secret_is_refused_even_as_the_jobs_secret(self):
        with patch.dict(os.environ, {"JOBS_SECRET": "test-webhook-secret"}):
            response = await self.post({"X-Jobs-Secret": "test-webhook-secret"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.calls, [])

    async def test_runs_due_jobs(self):
        headers = {"X-Jobs-Secret": JOBS_SECRET}
        with patch.dict(os.environ, {"JOBS_SECRET": JOBS_SECRET}):
            first = await self.post(headers)
            second = await self.post(headers)
            forced = await self.post(headers, force="true")
        self.assertEqual((first.status_code, first.json()), (200, {"ran": ["a"]}))
        self.assertEqual((second.status_code, second.json()), (200, {"ran": []}))
        self.assertEqual((forced.status_code, forced.json()), (200, {"ran": ["a"]}))
        self.assertEqual(self.names(), ["a", "a"])

    async def test_other_methods(self):
        with patch.dict(os.environ, {"JOBS_SECRET": JOBS_SECRET}):
            response = await self.client.get("/api/v1/jobs/daily", headers={"X-Jobs-Secret": JOBS_SECRET})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.calls, [])


class RegistryTests(unittest.TestCase):
    def test_the_real_registry(self):
        import upcoming
        import watchdog

        self.assertIs(main.jobs, jobs)
        self.assertEqual(jobs._JOBS, [("watchdog", watchdog.daily), ("upcoming_reminders", upcoming.daily)])
        self.assertEqual(list(main.app.openapi()["paths"]["/api/v1/jobs/daily"]), ["post"])


if __name__ == "__main__":
    unittest.main()
