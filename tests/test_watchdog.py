"""SMS watchdog (watchdog.py): quiet pipeline (CUJ 1), quiet bank (CUJ 2), late salary (CUJ 3), unreadable SMS,
once per episode, settings, statement counts, email (mocked) and the end-to-end trigger. Isolated in-memory
SQLite; no mail, no Gemini."""
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

import html
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

import jobs
import limits
import mailer
import pay_periods
import settings_store
import watchdog
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import Notification, Transaction, User

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)  # Fri 20 Nov, 21:00 IST
TODAY = date(2026, 11, 20)
DAY = timedelta(days=1)
MINUTE = timedelta(minutes=1)
HDFC_SENDER, ICICI_SENDER = "VM-HDFCBK", "AD-ICICIB"
HDFC_TEXT = "Sent Rs.450.00\nFrom HDFC Bank A/C *4821\nTo SWIGGY\nOn 21/10/26\nRef 629412345678"
ICICI_TEXT = "ICICI Bank Acct XX123 debited for Rs 250.00 on 20-Nov-26; ZOMATO credited. UPI:629400002222"
OTHER_TEXT = "Your OTP for login is 123456. Do not share it with anyone."
QUIET_BODY = ("The last bank SMS reached Folio on {}. On your iPhone, open Shortcuts → Automation and check that the "
              "bank-SMS automation is still on and runs without asking.")


def at_2100(day: date) -> datetime:
    """21:00 IST on `day` (when the daily job runs), in UTC."""
    return datetime(day.year, day.month, day.day, 21, 0, tzinfo=IST).astimezone(timezone.utc)


class WatchdogTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSessionLocal() as db:
            self.owner = User(email="owner@example.com", name="Owner")
            self.other = User(email="other@example.com", name="Other")
            db.add_all([self.owner, self.other])
            await db.commit()
        self.counter = 0

    async def sms(self, when: datetime, *, sender=HDFC_SENDER, text=HDFC_TEXT, is_parsed=True, parsed_by="regex",
                  user="owner", type="DEBIT", category="Food & Dining"):
        """A row received at `when` (created_at), its times stored in UTC."""
        self.counter += 1
        user_id = {"owner": self.owner.id, "other": self.other.id, None: None}[user]
        when = when.astimezone(timezone.utc)
        async with AsyncSessionLocal() as db:
            db.add(Transaction(type=type, amount=Decimal("450"), currency="INR", category=category, user_id=user_id,
                               is_parsed=is_parsed, parsed_by=parsed_by, raw_message=text, sender=sender,
                               message_hash=f"seed-{self.counter}", transaction_time=when, created_at=when))
            await db.commit()

    async def salary(self, day: date):
        await self.sms(datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST), type="CREDIT", category="Salary")

    async def unreadable(self, when: datetime, count: int = 1, **kwargs):
        for _ in range(count):
            await self.sms(when, is_parsed=False, parsed_by="failed", **kwargs)

    async def watch(self, today: date = TODAY, moment: datetime = NOW):
        with patch.object(jobs, "now", return_value=moment):
            async with AsyncSessionLocal() as db:
                await watchdog.daily(db, self.owner, today)

    async def counted(self, today: date = TODAY, moment: datetime = NOW) -> int:
        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await self.watch(today, moment)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        return len(statements)

    async def warnings(self, prefix: str = "watchdog:") -> list[tuple]:
        async with AsyncSessionLocal() as db:
            rows = await db.scalars(select(Notification).where(Notification.kind == "watchdog")
                                    .order_by(Notification.dedupe_key))
            return [(n.dedupe_key, n.title, n.body, n.link) for n in rows if n.dedupe_key.startswith(prefix)]

    async def keys(self, prefix: str = "watchdog:") -> list[str]:
        return [key for key, *_ in await self.warnings(prefix)]

    async def settings(self, **changes):
        async with AsyncSessionLocal() as db:
            await settings_store.save(db, self.owner.id, changes)


class QuietPipelineTests(WatchdogTestCase):
    async def test_shortcut_stopped(self):  # CUJ 1
        await self.sms(datetime(2026, 11, 16, 10, 0, tzinfo=IST))  # Monday 10:00
        await self.watch()
        self.assertEqual(await self.warnings(), [(
            "watchdog:quiet:2026-11-16", "No bank SMS for 4 days — check your iPhone Shortcut",
            QUIET_BODY.format("16 Nov"), "/settings#alerts",
        )])

    async def test_threshold(self):
        for newest, fires in ((NOW - 4 * DAY - MINUTE, True), (NOW - 4 * DAY, False),
                              (NOW - 3 * DAY - MINUTE, False)):
            with self.subTest(newest=newest):
                await self.asyncSetUp()
                await self.sms(newest)
                await self.watch()
                self.assertEqual(len(await self.warnings()), int(fires))

    async def test_what_counts_as_received(self):
        cases = (
            ("manual", dict(parsed_by="manual"), True),  # added by hand: doesn't count
            ("unreadable", dict(is_parsed=False, parsed_by="failed"), False),
            ("edited", dict(parsed_by="edited"), False),
            ("ai", dict(parsed_by="ai"), False),
            ("other user", dict(user="other"), True),
            ("unassigned", dict(user=None), False),
        )
        for name, recent, fires in cases:
            with self.subTest(name):
                await self.asyncSetUp()
                await self.sms(NOW - 5 * DAY)
                await self.sms(NOW - timedelta(hours=1), **recent)
                await self.watch()
                self.assertEqual(await self.keys(), ["watchdog:quiet:2026-11-15"] if fires else [])

    async def test_no_sms_ever(self):
        self.assertEqual(await self.counted(), 4)  # settings, newest, salary, unreadable: no bank query
        self.assertEqual(await self.warnings(), [])

    async def test_once_per_episode(self):
        await self.sms(NOW - 5 * DAY)
        await self.watch()
        await self.watch()
        self.assertEqual(await self.keys(), ["watchdog:quiet:2026-11-15"])
        await self.sms(NOW - timedelta(hours=1))  # the Shortcut works again
        await self.watch()
        self.assertEqual(len(await self.warnings()), 1)
        await self.watch(TODAY + 5 * DAY, NOW + 5 * DAY)  # and stops again
        self.assertEqual(await self.keys(), ["watchdog:quiet:2026-11-15", "watchdog:quiet:2026-11-20"])
        [_, (_, title, body, _)] = await self.warnings()
        self.assertEqual(title, "No bank SMS for 5 days — check your iPhone Shortcut")
        self.assertEqual(body, QUIET_BODY.format("20 Nov"))

    async def test_quiet_days_setting(self):
        await self.settings(**{"watchdog.quiet_days": 6})
        await self.sms(NOW - 5 * DAY)
        await self.watch()
        self.assertEqual(await self.warnings(), [])
        await self.watch(moment=NOW + 2 * DAY)
        [(key, title, _, _)] = await self.warnings()
        self.assertEqual((key, title), ("watchdog:quiet:2026-11-15", "No bank SMS for 7 days — check your iPhone Shortcut"))


class QuietBankTests(WatchdogTestCase):
    async def daily(self, last: datetime, *, days=20, **kwargs):
        for n in range(days):
            await self.sms(last - n * DAY, **kwargs)

    async def icici(self, last: datetime = NOW - timedelta(hours=1), days=20):
        await self.daily(last, days=days, sender=ICICI_SENDER, text=ICICI_TEXT)

    async def test_one_bank_quiet(self):  # CUJ 2
        await self.daily(NOW - 5 * DAY)
        await self.icici()
        await self.watch()
        self.assertEqual(await self.warnings(), [(
            "watchdog:quiet:HDFC:2026-11-15", "No HDFC SMS for 5 days",
            "ICICI SMS still arrive, but the last HDFC SMS reached Folio on 15 Nov. Check that your iPhone "
            "automation still forwards HDFC's messages.", "/settings#alerts",
        )])
        await self.watch()
        self.assertEqual(len(await self.warnings()), 1)

    async def test_threshold(self):
        for newest, fires in ((NOW - 4 * DAY, False), (NOW - 4 * DAY - MINUTE, True)):
            with self.subTest(newest=newest):
                await self.asyncSetUp()
                await self.daily(newest)
                await self.icici()
                await self.watch()
                self.assertEqual(await self.keys(), ["watchdog:quiet:HDFC:2026-11-16"] if fires else [])
        [(_, title, _, _)] = await self.warnings()
        self.assertEqual(title, "No HDFC SMS for 4 days")

    async def test_needs_three_sms_in_60_days(self):
        await self.daily(NOW - 5 * DAY, days=2)
        await self.daily(NOW - 61 * DAY, days=5)  # outside the window
        await self.icici()
        await self.watch()
        self.assertEqual(await self.warnings(), [])
        await self.sms(NOW - 7 * DAY)
        await self.watch()
        self.assertEqual(await self.keys(), ["watchdog:quiet:HDFC:2026-11-15"])

    async def test_a_bank_that_rarely_sends_sms_is_judged_by_its_own_gaps(self):
        # The seed's ICICI salary account: salary on the 31st, a transfer on the 1st, two SIPs on the 10th.
        for day in (date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 10), date(2026, 10, 10), date(2026, 10, 31),
                    date(2026, 11, 1), date(2026, 11, 10), date(2026, 11, 10)):
            await self.sms(at_2100(day) - 11 * timedelta(hours=1), sender=ICICI_SENDER, text=ICICI_TEXT)
        await self.daily(NOW - timedelta(hours=1))  # HDFC every day
        await self.watch()  # 10 days quiet, but its longest gap (10 Oct → 31 Oct) is 21 days
        self.assertEqual(await self.warnings(), [])

    async def test_quiet_after_twice_its_longest_gap(self):
        for gap, fires in ((5 * DAY, False), (5 * DAY - MINUTE, True)):  # HDFC silent for 10 days
            with self.subTest(gap=gap):
                await self.asyncSetUp()
                for when in (NOW - 10 * DAY, NOW - 10 * DAY - gap, NOW - 11 * DAY - gap):
                    await self.sms(when)
                await self.icici()
                await self.watch()
                self.assertEqual(await self.keys(), ["watchdog:quiet:HDFC:2026-11-10"] if fires else [])
        [(_, title, _, _)] = await self.warnings()
        self.assertEqual(title, "No HDFC SMS for 10 days")

    async def test_gaps_before_the_window_do_not_count(self):
        await self.daily(NOW - 5 * DAY)
        await self.sms(NOW - 61 * DAY)  # would make the longest gap 37 days if it counted
        await self.icici()
        await self.watch()
        self.assertEqual(await self.keys(), ["watchdog:quiet:HDFC:2026-11-15"])

    async def test_bank_from_the_text_when_the_sender_is_missing(self):
        await self.daily(NOW - 5 * DAY, days=3, sender=None)
        await self.icici(days=1)
        await self.watch()
        self.assertEqual(await self.keys(), ["watchdog:quiet:HDFC:2026-11-15"])

    async def test_rows_with_no_bank_are_ignored(self):
        await self.daily(NOW - 5 * DAY, days=5, sender="VM-OTPSVC", text=OTHER_TEXT)
        await self.daily(NOW - 5 * DAY, days=3)
        await self.sms(NOW - timedelta(hours=1), sender="VM-OTPSVC", text=OTHER_TEXT)  # no other bank is active
        await self.watch()
        self.assertEqual(await self.warnings(), [])
        await self.icici(days=1)
        await self.watch()
        self.assertEqual(await self.keys(), ["watchdog:quiet:HDFC:2026-11-15"])

    async def test_skipped_while_everything_is_quiet(self):
        await self.daily(NOW - 5 * DAY)
        await self.icici(NOW - 6 * DAY)
        # settings, newest, 2 for the new notification, salary, unreadable: no bank query
        self.assertEqual(await self.counted(), 6)
        self.assertEqual(await self.keys(), ["watchdog:quiet:2026-11-15"])

    async def test_statements_when_nothing_fires(self):
        await self.daily(NOW - timedelta(hours=2), days=3)
        await self.icici(days=3)
        self.assertEqual(await self.counted(), 5)
        await self.settings(**{"watchdog.salary_check": False})
        self.assertEqual(await self.counted(), 4)
        self.assertEqual(await self.warnings(), [])


class SalaryTests(WatchdogTestCase):
    async def cuj3(self):
        for day in (date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 31)):
            await self.salary(day)

    async def watch_on(self, day: date):
        await self.watch(day, at_2100(day))

    async def test_salary_late(self):  # CUJ 3
        await self.cuj3()
        await self.watch_on(date(2026, 12, 3))
        self.assertEqual(await self.warnings("watchdog:salary:"), [])
        await self.watch_on(date(2026, 12, 4))
        self.assertEqual(await self.warnings("watchdog:salary:"), [(
            "watchdog:salary:2026-11-30", "Salary usually arrives around 30 Nov; not seen yet",
            "No credit in the Salary category since 31 Oct. If it has arrived, set its category to Salary in Activity.",
            "/settings#alerts",
        )])
        await self.watch_on(date(2026, 12, 5))
        self.assertEqual(len(await self.warnings("watchdog:salary:")), 1)

    async def test_a_new_salary_starts_a_new_episode(self):
        await self.cuj3()
        await self.salary(date(2026, 12, 2))
        await self.watch_on(date(2026, 12, 4))  # expected now 31 Dec
        self.assertEqual(await self.keys("watchdog:salary:"), [])
        await self.watch_on(date(2027, 1, 3))
        self.assertEqual(await self.keys("watchdog:salary:"), [])
        await self.watch_on(date(2027, 1, 4))
        [(key, title, body, _)] = await self.warnings("watchdog:salary:")
        self.assertEqual((key, title), ("watchdog:salary:2026-12-31", "Salary usually arrives around 31 Dec; not seen yet"))
        self.assertIn("since 2 Dec.", body)

    async def test_needs_two_salaries_in_120_days(self):
        await self.salary(date(2026, 5, 18))  # about 200 days before
        await self.salary(date(2026, 10, 31))
        await self.watch_on(date(2026, 12, 4))
        self.assertEqual(await self.keys("watchdog:salary:"), [])

    async def test_salary_check_off(self):
        await self.cuj3()
        await self.settings(**{"watchdog.salary_check": False})
        await self.watch_on(date(2026, 12, 4))
        self.assertEqual(await self.keys("watchdog:salary:"), [])
        self.assertEqual(await self.keys(), ["watchdog:quiet:2026-10-31"])  # the other checks still ran

    async def test_a_failing_check_does_not_stop_the_others(self):
        await self.unreadable(NOW - timedelta(hours=1), 5)
        with patch.object(pay_periods, "salary_days", AsyncMock(side_effect=RuntimeError("boom"))), \
                self.assertLogs("watchdog", "ERROR") as logs:
            await self.watch()
        self.assertIn("Watchdog check _salary failed", logs.output[0])
        self.assertEqual(await self.keys(), ["watchdog:unreadable:2026-11-20"])


class UnreadableTests(WatchdogTestCase):
    async def test_five_unreadable_sms(self):
        await self.unreadable(NOW - timedelta(hours=1), 5)
        await self.watch()
        await self.watch()
        self.assertEqual(await self.warnings(), [(
            "watchdog:unreadable:2026-11-20", "5 SMS couldn't be read today",
            "They're in Activity under Needs review. If your bank changed its SMS format, more will fail until "
            "Folio's parser is updated.", "/activity?kind=review",
        )])

    async def test_below_the_threshold_or_outside_24_hours(self):
        await self.unreadable(NOW - timedelta(hours=1), 4)
        await self.unreadable(NOW - timedelta(hours=25))
        await self.unreadable(NOW - timedelta(hours=1), user="other")
        await self.watch()
        self.assertEqual(await self.warnings(), [])
        await self.unreadable(NOW - timedelta(hours=23), user=None)
        await self.watch()
        [(_, title, _, _)] = await self.warnings()
        self.assertEqual(title, "5 SMS couldn't be read today")


class SettingsTests(WatchdogTestCase):
    async def test_off(self):
        await self.settings(**{"watchdog.enabled": False})
        await self.sms(NOW - 10 * DAY)
        await self.unreadable(NOW - timedelta(hours=1), 6)
        self.assertEqual(await self.counted(), 1)
        self.assertEqual(await self.warnings(), [])


class EmailTests(WatchdogTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.send = AsyncMock(return_value="id")
        patcher = patch.object(mailer, "send_email", self.send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configured(self):
        return patch.object(mailer, "configured", return_value=True)

    async def test_a_new_warning_is_emailed_once(self):
        await self.sms(NOW - 5 * DAY)
        with self.configured():
            await self.watch()
            await self.watch()
        title = "No bank SMS for 5 days — check your iPhone Shortcut"
        body = QUIET_BODY.format("15 Nov")
        self.send.assert_awaited_once_with(
            "owner@example.com", title, f"{body}\n\nOpen Folio: Settings → Alerts.",
            limits._html_email(html.escape(title), f"<p>{html.escape(body)}</p>"),
        )

    async def test_not_emailed_when_off_or_not_configured(self):
        await self.sms(NOW - 5 * DAY)
        await self.watch()  # a new warning, but Gmail isn't configured in tests
        await self.settings(**{"watchdog.email": False})
        await self.unreadable(NOW + 2 * DAY - timedelta(hours=1), 5)
        with self.configured():
            await self.watch(TODAY + 2 * DAY, NOW + 2 * DAY)  # a new warning, configured, but watchdog.email is off
        self.assertEqual(await self.keys(), ["watchdog:quiet:2026-11-15", "watchdog:unreadable:2026-11-22"])
        self.send.assert_not_awaited()

    async def test_a_failed_email_is_told_in_the_bell_once_a_day(self):
        self.send.side_effect = mailer.MailError("Google rejected the Gmail refresh token")
        for n in range(3):  # quiet HDFC
            await self.sms(NOW - 5 * DAY - n * DAY)
        await self.unreadable(NOW - timedelta(hours=1), 5, sender=ICICI_SENDER, text=ICICI_TEXT)  # ICICI active
        with self.configured(), self.assertLogs("watchdog", "WARNING") as logs:
            await self.watch()
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("Watchdog email failed", logs.output[0])
        self.assertEqual(await self.warnings("watchdog:email-failed:"), [(
            "watchdog:email-failed:2026-11-20", "Email alerts aren't being sent",
            "Gmail refused the last email. Run scripts/gmail_authorize.py again (see the server README).",
            "/settings#alerts",
        )])
        self.assertEqual(await self.keys(), ["watchdog:email-failed:2026-11-20", "watchdog:quiet:HDFC:2026-11-15",
                                             "watchdog:unreadable:2026-11-20"])


class EndToEndTests(WatchdogTestCase):
    async def test_trigger_creates_the_warning(self):
        await self.sms(NOW - 5 * DAY)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")
        self.addAsyncCleanup(client.aclose)
        self.addCleanup(app.dependency_overrides.clear)
        app.dependency_overrides[current_user] = lambda: self.owner
        headers = {"X-Jobs-Secret": "test-jobs-secret"}
        with patch.dict(os.environ, {"JOBS_SECRET": "test-jobs-secret"}), patch.object(jobs, "now", return_value=NOW):
            response = await client.post("/api/v1/jobs/daily", params={"force": "true"}, headers=headers)
            self.assertEqual((response.status_code, response.json()), (200, {"ran": ["watchdog", "upcoming_reminders"]}))
            again = await client.post("/api/v1/jobs/daily", params={"force": "true"}, headers=headers)
            self.assertEqual(again.json(), {"ran": ["watchdog", "upcoming_reminders"]})
        inbox = (await client.get("/api/v1/notifications")).json()
        self.assertEqual([(n["kind"], n["title"], n["link"], n["actions"]) for n in inbox["items"]], [
            ("watchdog", "No bank SMS for 5 days — check your iPhone Shortcut", "/settings#alerts", []),
        ])


if __name__ == "__main__":
    unittest.main()
