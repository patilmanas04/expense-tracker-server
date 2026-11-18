"""Pay periods (pay_periods.py): the name rule, building periods, the expected last day, the monthly and previous
windows, salary days, loading with its statement count, and GET /api/v1/periods. Isolated in-memory SQLite; no
mail, no Gemini."""
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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

import limits
import pay_periods
import settings_store
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import DUPLICATE_PENDING, DuplicateFlag, Transaction, User
from pay_periods import PayPeriod, build, expected_last_day, monthly_window, previous_window, salary_month

ORIGIN = {"Origin": "http://localhost:3000"}
IST = timezone(timedelta(hours=5, minutes=30))
d = date


def noon(day: date) -> datetime:
    """12:00 IST on `day`, as an aware UTC datetime."""
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=IST).astimezone(timezone.utc)


def ist_day(moment: datetime) -> date:
    return moment.astimezone(IST).date()


class NameTests(unittest.TestCase):
    def test_salary_month(self):
        for day, name in ((d(2026, 11, 15), "2026-11"), (d(2026, 11, 16), "2026-12"), (d(2026, 12, 31), "2027-01"),
                          (d(2027, 1, 1), "2027-01"), (d(2026, 10, 31), "2026-11"), (d(2026, 11, 2), "2026-11"),
                          (d(2027, 1, 16), "2027-02")):
            with self.subTest(day=day):
                self.assertEqual(salary_month(day), name)


class BuildTests(unittest.TestCase):
    def periods(self, *days):
        return [(p.month, p.start, p.end) for p in build(list(days))]

    def test_salary_on_the_30th_and_31st(self):  # CUJ 1
        self.assertEqual(self.periods(d(2026, 9, 30), d(2026, 10, 31)), [
            ("2026-10", d(2026, 9, 30), d(2026, 10, 30)),
            ("2026-11", d(2026, 10, 31), d(2026, 12, 15)),
        ])

    def test_late_salary(self):  # CUJ 2
        self.assertEqual(self.periods(d(2026, 10, 31), d(2026, 12, 3)), [
            ("2026-11", d(2026, 10, 31), d(2026, 12, 2)),
            ("2026-12", d(2026, 12, 3), d(2027, 1, 15)),
        ])

    def test_early_then_late(self):
        self.assertEqual(self.periods(d(2026, 9, 30), d(2026, 11, 14), d(2026, 11, 30)), [
            ("2026-10", d(2026, 9, 30), d(2026, 11, 13)),
            ("2026-11", d(2026, 11, 14), d(2026, 11, 29)),
            ("2026-12", d(2026, 11, 30), d(2027, 1, 15)),
        ])

    def test_later_salaries_with_the_same_name_start_nothing(self):  # split salary or bonus
        self.assertEqual(self.periods(d(2026, 10, 31), d(2026, 11, 2), d(2026, 11, 10)), [
            ("2026-11", d(2026, 10, 31), d(2026, 12, 15)),
        ])

    def test_salary_on_the_16th(self):
        self.assertEqual(self.periods(d(2027, 1, 16), d(2027, 2, 16), d(2027, 3, 16)), [
            ("2027-02", d(2027, 1, 16), d(2027, 2, 15)),
            ("2027-03", d(2027, 2, 16), d(2027, 3, 15)),
            ("2027-04", d(2027, 3, 16), d(2027, 5, 15)),
        ])

    def test_first_salary_inside_its_named_month_starts_on_the_1st(self):
        self.assertEqual(self.periods(d(2026, 11, 5)), [("2026-11", d(2026, 11, 1), d(2026, 12, 15))])

    def test_first_salary_before_its_named_month(self):  # 1–19 Nov stay calendar "Nov"
        self.assertEqual(self.periods(d(2026, 11, 20)), [("2026-12", d(2026, 11, 20), d(2027, 1, 15))])

    def test_a_drifting_payday_skips_a_name(self):
        self.assertEqual(self.periods(d(2026, 10, 16), d(2026, 11, 15), d(2026, 12, 16)), [
            ("2026-11", d(2026, 10, 16), d(2026, 12, 15)),
            ("2027-01", d(2026, 12, 16), d(2027, 2, 15)),
        ])

    def test_no_salary(self):
        self.assertEqual(build([]), [])

    def test_order_and_repeats_do_not_matter(self):
        self.assertEqual(build([d(2026, 10, 31), d(2026, 9, 30), d(2026, 10, 31)]),
                         build([d(2026, 9, 30), d(2026, 10, 31)]))
        self.assertEqual(self.periods(d(2026, 10, 31), d(2026, 9, 30), d(2026, 10, 31)), [
            ("2026-10", d(2026, 9, 30), d(2026, 10, 30)),
            ("2026-11", d(2026, 10, 31), d(2026, 12, 15)),
        ])

    def test_salary_stops_then_returns(self):  # 16–31 Dec is a calendar piece
        self.assertEqual(self.periods(d(2026, 10, 31), d(2027, 1, 2)), [
            ("2026-11", d(2026, 10, 31), d(2026, 12, 15)),
            ("2027-01", d(2027, 1, 1), d(2027, 2, 15)),
        ])

    def test_periods_never_overlap_and_names_increase(self):
        days = [d(2026, 1, 1) + timedelta(days=n * 29 + (n * 7) % 11) for n in range(30)]
        periods = build(days)
        for before, after in zip(periods, periods[1:]):
            self.assertLess(before.month, after.month)
            self.assertLess(before.end, after.start)
            self.assertLessEqual(after.start, after.end)


class ExpectedLastDayTests(unittest.TestCase):
    def test_cases(self):
        for start, last in ((d(2026, 10, 31), d(2026, 11, 29)), (d(2026, 12, 3), d(2027, 1, 2)),
                            (d(2027, 1, 31), d(2027, 2, 27)), (d(2028, 1, 31), d(2028, 2, 28)),
                            (d(2026, 9, 30), d(2026, 10, 29))):
            with self.subTest(start=start):
                self.assertEqual(expected_last_day(start), last)


class WindowTests(unittest.TestCase):
    def window(self, salaries, day=None, moment=None):
        first, start, end = monthly_window(build(salaries), moment or noon(day))
        self.assertEqual(start.utcoffset(), timedelta(0))
        self.assertEqual(start, datetime(first.year, first.month, first.day, tzinfo=IST).astimezone(timezone.utc))
        return first, ist_day(end)

    def test_salary_31_oct_only(self):
        salaries = [d(2026, 10, 31)]
        for day, first, end in ((d(2026, 11, 10), d(2026, 10, 31), d(2026, 11, 30)),  # expected last day 29 Nov
                                (d(2026, 12, 2), d(2026, 10, 31), d(2026, 12, 3)),  # late: never before today
                                (d(2026, 12, 20), d(2026, 12, 16), d(2027, 1, 1)),  # after the latest end
                                (d(2027, 1, 5), d(2027, 1, 1), d(2027, 2, 1)),  # a plain calendar month
                                (d(2026, 10, 15), d(2026, 10, 1), d(2026, 10, 31))):  # cut before the period
            with self.subTest(day=day):
                self.assertEqual(self.window(salaries, day), (first, end))

    def test_a_closed_period_ends_on_its_real_end(self):
        self.assertEqual(self.window([d(2026, 9, 30), d(2026, 10, 31)], d(2026, 10, 20)),
                         (d(2026, 9, 30), d(2026, 10, 31)))

    def test_the_day_is_the_ist_day(self):
        moment = datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc)  # 31 Oct 00:30 IST
        self.assertEqual(self.window([d(2026, 10, 31)], moment=moment)[0], d(2026, 10, 31))

    def test_no_periods_is_the_calendar_month(self):
        for moment in (noon(d(2026, 10, 21)), datetime(2026, 10, 31, 18, 31, tzinfo=timezone.utc),
                       datetime(2027, 2, 28, 18, 29, tzinfo=timezone.utc)):
            with self.subTest(moment=moment):
                self.assertEqual(monthly_window([], moment), limits.period_window("monthly", moment))


class PreviousWindowTests(unittest.TestCase):
    def test_the_period_before(self):
        periods = build([d(2026, 9, 30), d(2026, 10, 31)])
        first, start, end = previous_window(periods, d(2026, 10, 31))
        self.assertEqual((first, ist_day(start), ist_day(end)), (d(2026, 9, 30), d(2026, 9, 30), d(2026, 10, 31)))
        self.assertEqual(end, datetime(2026, 10, 31, tzinfo=IST).astimezone(timezone.utc))

    def test_none_without_a_period_before(self):
        self.assertIsNone(previous_window(build([d(2026, 10, 31)]), d(2026, 10, 31)))

    def test_none_for_a_calendar_piece(self):
        periods = build([d(2026, 10, 31)])
        self.assertIsNone(previous_window(periods, d(2026, 12, 16)))  # 16 Dec starts a piece after "Nov"
        self.assertIsNone(previous_window(periods, d(2026, 10, 1)))


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def seed(self, when: datetime, *, type="CREDIT", category="Salary", currency="INR", is_parsed=True,
                   user="owner", parsed_by="regex"):
        """A row saved directly, its time stored in UTC."""
        self.counter += 1
        user_id = {"owner": self.owner.id, "other": self.other.id, None: None}[user]
        async with AsyncSessionLocal() as db:
            txn = Transaction(type=type, amount=Decimal("50000"), currency=currency, category=category,
                              user_id=user_id, is_parsed=is_parsed, parsed_by=parsed_by, raw_message="fixture",
                              message_hash=f"seed-{self.counter}", transaction_time=when.astimezone(timezone.utc))
            db.add(txn)
            await db.commit()
            return txn.id

    async def salary(self, day: date, user="owner"):
        return await self.seed(datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST), user=user)

    async def flag(self, transaction_id, original_id) -> None:
        async with AsyncSessionLocal() as db:
            db.add(DuplicateFlag(transaction_id=transaction_id, original_id=original_id, user_id=self.owner.id,
                                 reason="reference", status=DUPLICATE_PENDING, created_at=datetime.now(timezone.utc)))
            await db.commit()

    async def payday_on(self):
        async with AsyncSessionLocal() as db:
            await settings_store.save(db, self.owner.id, {"periods.payday": True})

    async def counted(self, action):
        """(result, statements run) for an action given a fresh session."""
        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        async with AsyncSessionLocal() as db:
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            try:
                result = await action(db)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record)
        return result, len(statements)


class SalaryDaysTests(DatabaseTestCase):
    async def days(self, user=None):
        async with AsyncSessionLocal() as db:
            return await pay_periods.salary_days(db, user or self.owner)

    async def test_which_credits_are_salary_days(self):
        original = await self.salary(d(2026, 8, 31))  # owner, parsed INR Salary credit
        await self.salary(d(2026, 9, 30), user=None)  # no owner
        await self.seed(datetime(2026, 10, 31, 9, 0, tzinfo=IST), parsed_by="manual")  # hand-added
        await self.seed(datetime(2026, 7, 1, 9, 0, tzinfo=IST), type="DEBIT")
        await self.seed(datetime(2026, 7, 2, 9, 0, tzinfo=IST), is_parsed=False)
        await self.seed(datetime(2026, 7, 3, 9, 0, tzinfo=IST), currency="USD")
        await self.seed(datetime(2026, 7, 4, 9, 0, tzinfo=IST), category="Income")
        await self.salary(d(2026, 7, 5), user="other")
        held_out = await self.salary(d(2026, 7, 6))
        await self.flag(held_out, original)
        self.assertEqual(await self.days(), [d(2026, 8, 31), d(2026, 9, 30), d(2026, 10, 31)])
        # The other user sees their own and the row with no owner, as every reader does (dashboard.ownership).
        self.assertEqual(await self.days(self.other), [d(2026, 7, 5), d(2026, 9, 30)])

    async def test_ist_dates_distinct_and_ascending(self):
        await self.seed(datetime(2026, 11, 30, 20, 0, tzinfo=timezone.utc))  # 1 Dec 01:30 IST
        await self.salary(d(2026, 10, 31))
        await self.seed(datetime(2026, 10, 31, 18, 0, tzinfo=IST))  # same IST day, later
        await self.seed(datetime(2026, 9, 30, 18, 29, tzinfo=timezone.utc))  # 30 Sep 23:59 IST
        self.assertEqual(await self.days(), [d(2026, 9, 30), d(2026, 10, 31), d(2026, 12, 1)])


class LoadTests(DatabaseTestCase):
    async def test_statements(self):
        await self.salary(d(2026, 10, 31))
        result, count = await self.counted(lambda db: pay_periods.load(db, self.owner))
        self.assertEqual((result, count), (None, 1))  # off: the settings read only
        result, count = await self.counted(lambda db: pay_periods.load(db, self.owner, enabled=False))
        self.assertEqual((result, count), (None, 0))
        result, count = await self.counted(lambda db: pay_periods.load(db, self.owner, enabled=True))
        self.assertEqual((result, count), ([PayPeriod("2026-11", d(2026, 10, 31), d(2026, 12, 15))], 1))
        await self.payday_on()
        result, count = await self.counted(lambda db: pay_periods.load(db, self.owner))
        self.assertEqual((result, count), ([PayPeriod("2026-11", d(2026, 10, 31), d(2026, 12, 15))], 2))

    async def test_on_with_no_salary_is_an_empty_list(self):
        await self.payday_on()
        result, _ = await self.counted(lambda db: pay_periods.load(db, self.owner))
        self.assertEqual(result, [])


class ApiTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(app.dependency_overrides.clear)

    def sign_in(self, user=None):
        app.dependency_overrides[current_user] = lambda: user or self.owner

    async def periods(self):
        response = await self.client.get("/api/v1/periods")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_requires_a_session(self):
        response = await self.client.get("/api/v1/periods")
        self.assertEqual(response.status_code, 401, response.text)

    async def test_off_on_and_periods(self):
        self.sign_in()
        await self.salary(d(2026, 9, 30))
        self.assertEqual(await self.periods(), {"enabled": False, "periods": []})
        await self.salary(d(2026, 10, 31), user="other")
        response = await self.client.put("/api/v1/settings", json={"periods.payday": True}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["periods.payday"], True)
        await self.salary(d(2026, 10, 31))
        self.assertEqual(await self.periods(), {"enabled": True, "periods": [
            {"month": "2026-10", "start": "2026-09-30", "end": "2026-10-30"},
            {"month": "2026-11", "start": "2026-10-31", "end": "2026-12-15"},
        ]})

    async def test_on_with_no_salary(self):
        self.sign_in()
        response = await self.client.put("/api/v1/settings", json={"periods.payday": True}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.periods(), {"enabled": True, "periods": []})

    async def test_another_users_salaries_never_appear(self):
        await self.salary(d(2026, 9, 30), user="other")
        await self.salary(d(2026, 10, 31), user="other")
        await self.payday_on()
        self.sign_in()
        self.assertEqual(await self.periods(), {"enabled": True, "periods": []})
        async with AsyncSessionLocal() as db:
            await settings_store.save(db, self.other.id, {"periods.payday": True})
        self.sign_in(self.other)
        self.assertEqual(len((await self.periods())["periods"]), 2)


if __name__ == "__main__":
    unittest.main()
