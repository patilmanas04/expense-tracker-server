"""Recurring payments (recurring.py): detection and its edges, which payments count, the window, the API and its
decisions, the query budget, and the post-save notifications. Isolated in-memory SQLite; email stays off (no Gmail
keys); Gemini stays off."""
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
import uuid
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import post_save
import recurring
from auth import current_user
from database import AsyncSessionLocal, get_db
from limits import IST
from main import app
from merchants import name_hash
from models import RecurringDecision, User
from recurring import Payment, changed, detect
from test_post_save import MINUTE, NOW, ORIGIN, PostSaveTestCase

DAY = timedelta(days=1)
JSON_ORIGIN = {**ORIGIN, "Content-Type": "application/json"}
TODAY = NOW.astimezone(IST).date()  # Wed 21 Oct 2026


def pay(amounts, days, name="Netflix") -> list[Payment]:
    return [Payment(uuid.uuid4(), day, Decimal(str(amount)), name) for amount, day in zip(amounts, days)]


def every(start: date, *gaps: int) -> list[date]:
    """`start`, then each gap after the one before."""
    days = [start]
    for gap in gaps:
        days.append(days[-1] + timedelta(days=gap))
    return days


def iso(days_from_now: int) -> str:
    return (TODAY + timedelta(days=days_from_now)).isoformat()


SEP3, OCT3, NOV3 = date(2026, 9, 3), date(2026, 10, 3), date(2026, 11, 3)


class DetectTests(unittest.TestCase):
    def spotify(self, today=NOV3):
        return detect("spotify", pay([119] * 3, [SEP3, OCT3, NOV3], "Spotify"), today)

    def test_monthly(self):
        series = self.spotify()
        self.assertEqual((series.cadence, series.period_days, series.next_expected, series.payments),
                         ("monthly", 30, date(2026, 12, 3), 3))
        self.assertEqual((series.first_date, series.last_date, series.previous_amount, series.late, series.name),
                         (SEP3, NOV3, None, False, "Spotify"))
        self.assertEqual(series.amount, Decimal("119"))
        self.assertEqual(series.status, "active")

    def test_two_monthly_payments_are_not_enough(self):
        self.assertIsNone(detect("x", pay([119] * 2, [OCT3, NOV3]), NOV3))
        self.assertIsNone(detect("x", pay([119], [NOV3]), NOV3))
        self.assertIsNone(detect("x", [], NOV3))

    def test_yearly_pair(self):
        days = [date(2025, 3, 1), date(2026, 3, 1)]
        series = detect("zee5", pay([1200, 1250], days), NOV3)
        self.assertEqual((series.cadence, series.period_days, series.next_expected, series.payments),
                         ("yearly", 365, date(2027, 3, 1), 2))
        self.assertIsNone(detect("zee5", pay([1000, 1400], days), NOV3))

    def test_weekly(self):
        days = [date(2026, 10, 1), date(2026, 10, 8), date(2026, 10, 15)]
        series = detect("dmart", pay([300] * 3, days), date(2026, 10, 15))
        self.assertEqual((series.cadence, series.period_days, series.next_expected), ("weekly", 7, date(2026, 10, 22)))

    def test_irregular_gaps(self):
        self.assertIsNone(detect("x", pay([100] * 3, every(SEP3, 20, 40)), NOV3))

    def test_amount_band(self):
        days = every(date(2026, 10, 1), 7, 7)
        self.assertIsNone(detect("x", pay([300, 450, 280], days), days[-1]))
        self.assertEqual(detect("x", pay([300, 310, 290], days), days[-1]).cadence, "weekly")

    def test_band_is_inclusive(self):
        days = every(SEP3, 30, 30)
        self.assertIsNotNone(detect("x", pay([1000, 1150, 850], days), days[-1]))

    def test_price_change(self):
        days = every(date(2026, 8, 5), 30, 30, 30)
        series = detect("netflix", pay([499, 499, 499, 649], days), days[-1])
        self.assertEqual((series.amount, series.previous_amount, series.steady_before, series.payments),
                         (Decimal("649"), Decimal("499"), True, 4))
        days = every(date(2026, 7, 6), 30, 30, 30, 30)
        series = detect("netflix", pay([499, 499, 499, 649, 649], days), days[-1])
        self.assertIsNone(series.previous_amount)
        self.assertEqual(series.amount, Decimal("649"))

    def test_one_missed_month(self):
        days = every(date(2026, 7, 1), 30, 30, 60)
        series = detect("x", pay([499] * 4, days), days[-1])
        self.assertEqual((series.cadence, series.payments, series.period_days), ("monthly", 4, 30))

    def test_only_one_missed_month_per_run(self):
        days = every(date(2026, 1, 1), 30, 60, 60, 30, 30)
        series = detect("x", pay([499] * 6, days), days[-1])
        self.assertEqual((series.payments, series.first_date), (4, days[2]))

    def test_newest_gap_missed_and_another_missed(self):
        days = every(date(2026, 6, 1), 60, 60)
        self.assertIsNone(detect("x", pay([499] * 3, days), days[-1]))

    def test_run_restarts_after_a_long_gap(self):
        days = every(date(2025, 1, 1), *([30] * 9), 100, 30, 30)
        series = detect("x", pay([499] * 13, days), days[-1])
        self.assertEqual((series.payments, series.first_date), (3, days[10]))

    def test_28_day_plan(self):
        days = every(date(2026, 8, 1), 28, 28)
        series = detect("jio", pay([299] * 3, days), days[-1])
        self.assertEqual((series.period_days, series.next_expected), (28, days[-1] + 28 * DAY))

    def test_late(self):
        self.assertFalse(self.spotify(date(2026, 12, 10)).late)
        self.assertTrue(self.spotify(date(2026, 12, 11)).late)

    def test_stopped(self):
        self.assertIsNotNone(self.spotify(NOV3 + 67 * DAY))
        self.assertIsNone(self.spotify(NOV3 + 68 * DAY))

    def test_same_day_newest_payment(self):
        self.assertIsNone(detect("x", pay([119] * 4, [SEP3, OCT3, NOV3, NOV3]), NOV3))

    def test_changed(self):
        self.assertFalse(changed(Decimal("520"), Decimal("499")))
        self.assertFalse(changed(Decimal("109"), Decimal("100")))
        self.assertTrue(changed(Decimal("211"), Decimal("200")))
        self.assertFalse(changed(Decimal("1050"), Decimal("1000")))  # exactly 5%
        self.assertTrue(changed(Decimal("649"), Decimal("499")))

    def test_electricity(self):
        days = every(SEP3, 30, 30)
        series = detect("bescom", pay([1200, 1320, 1180], days), days[-1])
        self.assertEqual((series.previous_amount, series.steady_before), (Decimal("1320"), False))

    def test_name_is_the_newest_with_spaces_collapsed(self):
        payments = pay([119] * 3, [SEP3, OCT3, NOV3], "spotify")
        payments[-1] = Payment(uuid.uuid4(), NOV3, Decimal("119"), "  SPOTIFY   India ")
        self.assertEqual(detect("spotify india", payments, NOV3).name, "SPOTIFY India")


class RecurringTestCase(PostSaveTestCase):
    async def series(self, merchant, amount, days_ago, **kwargs) -> list[uuid.UUID]:
        """Payments `days_ago` days before NOW (any order given; oldest first returned)."""
        return [await self.seed(merchant, amount, NOW - d * DAY, **kwargs) for d in sorted(days_ago, reverse=True)]

    async def get(self) -> dict:
        response = await self.client.get("/api/v1/recurring")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def names(self, part="items") -> list[str]:
        return [s["name"] for s in (await self.get())[part]]

    async def decide(self, merchant_key, cadence="monthly", status="dismissed"):
        response = await self.client.put("/api/v1/recurring/decisions", headers=ORIGIN,
                                         json={"merchant_key": merchant_key, "cadence": cadence, "status": status})
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")

    async def clear(self, merchant_key, cadence="monthly"):
        response = await self.client.post("/api/v1/recurring/decisions/clear", headers=ORIGIN,
                                          json={"merchant_key": merchant_key, "cadence": cadence})
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")

    async def decisions(self) -> list[RecurringDecision]:
        async with AsyncSessionLocal() as db:
            return list(await db.scalars(select(RecurringDecision)))

    async def recurring_notes(self):
        return [n for n in await self.notifications() if n.kind.startswith("recurring")]


class LoadingTests(RecurringTestCase):
    async def test_owner_and_filters(self):
        await self.series("Spotify", 119, [60, 30, 0])  # an SMS row: no user
        await self.series("Hotstar", 299, [61, 31, 1], user_id=self.owner.id)
        await self.series("Theirs", 119, [60, 30, 0], user_id=self.other.id)
        await self.series("Unparsed", 119, [60, 30, 0], is_parsed=False)
        await self.series("Credit Co", 119, [60, 30, 0], type="CREDIT", category="Income")
        await self.series("Usd Co", 119, [60, 30, 0], currency="USD")
        await self.series("HDFC Card", 5000, [60, 30, 0], category="Credit Card Payment")
        await self.series("Manas", 7000, [60, 30, 0], category="Self Transfer")
        for blank in ("Unknown", None, "  "):
            await self.series(blank, 119, [60, 30, 0])
        await self.series("Groww", 5000, [62, 32, 2], category="Investments & Finance")
        await self.series("Jio", 299, [63, 33, 3], category=None)
        self.assertEqual(sorted(await self.names()), ["Groww", "Hotstar", "Jio", "Spotify"])
        async with AsyncSessionLocal() as db:
            grouped = await recurring.load_payments(db, self.owner, TODAY)
        self.assertEqual(sorted(grouped), ["groww", "hotstar", "jio", "spotify"])

    async def test_held_out_row_is_ignored(self):
        original = await self.seed("Swiggy", 500, NOW - MINUTE)
        ids = await self.series("Hotstar", 299, [90, 60, 30, 0])
        [before] = (await self.get())["items"]
        self.assertEqual(before["payments"], 4)
        await self.flag(ids[-1], original)
        [after] = (await self.get())["items"]
        self.assertEqual((after["payments"], after["last_date"]), (3, iso(-30)))

    async def test_names_join_by_key(self):
        await self.seed("Netflix", 499, NOW - 60 * DAY)
        await self.seed("NETFLIX", 499, NOW - 30 * DAY)
        await self.seed("Netflix  ", 499, NOW - DAY)
        await self.seed("Netflix India", 499, NOW)  # another key
        [series] = (await self.get())["items"]
        self.assertEqual((series["merchant_key"], series["name"], series["payments"]), ("netflix", "Netflix", 3))
        await self.seed("  NETFLIX   PLUS ", 499, NOW - 61 * DAY)
        await self.seed("netflix plus", 499, NOW - 31 * DAY)
        await self.seed("NETFLIX  plus", 499, NOW - 2 * DAY)
        items = (await self.get())["items"]
        self.assertEqual(sorted((s["merchant_key"], s["name"], s["payments"]) for s in items),
                         [("netflix", "Netflix", 3), ("netflix plus", "NETFLIX plus", 3)])

    async def test_window(self):
        await self.seed("Old", 100, NOW - 856 * DAY)
        await self.seed("Old", 100, NOW - 854 * DAY)
        await self.seed("Edge", 100, NOW - 855 * DAY)
        async with AsyncSessionLocal() as db:
            grouped = await recurring.load_payments(db, self.owner, TODAY)
        self.assertEqual([p.day for p in grouped["old"]], [TODAY - 854 * DAY])
        self.assertEqual([p.day for p in grouped["edge"]], [TODAY - 855 * DAY])

    async def test_yearly_series_stay_listed(self):
        await self.series("Zee5", 699, [665, 300])
        await self.series("Prime", 1499, [854, 489])  # the older payment just inside the window: late
        await self.series("Gone", 1499, [856, 491])  # just outside
        items = (await self.get())["items"]
        self.assertEqual([(s["name"], s["cadence"], s["payments"], s["late"]) for s in items],
                         [("Prime", "yearly", 2, True), ("Zee5", "yearly", 2, False)])
        self.assertEqual(items[1]["next_expected"], iso(65))

    async def test_list_series_runs_two_statements(self):
        await self.series("Spotify", 119, [60, 30, 0])
        await self.decide("spotify", status="cancelled")

        async def action():
            async with AsyncSessionLocal() as db:
                result = await recurring.list_series(db, self.owner, TODAY)
            self.assertEqual([s.status for s in result.items], ["cancelled"])

        self.assertEqual(await self.count_statements(action), 2)


class ApiTests(RecurringTestCase):
    async def test_shape_and_order(self):
        await self.series("Spotify", 119, [85, 55, 25])
        await self.seed("Netflix", 499, NOW - 100 * DAY)
        await self.series("Netflix", 499, [70, 40])
        await self.seed("Netflix", 649, NOW - 10 * DAY)
        await self.series("Jio", 299, [100, 70, 40], category=None)  # next 10 days ago: late
        await self.series("Zee5", 699, [665, 300])
        await self.decide("zee5", "yearly", "cancelled")
        body = await self.get()
        self.assertEqual((body["currency"], body["monthly_total"], body["hidden"]), ("INR", "1067.00", []))
        self.assertEqual([(s["name"], s["late"], s["status"]) for s in body["items"]], [
            ("Jio", True, "active"), ("Spotify", False, "active"), ("Netflix", False, "active"),
            ("Zee5", False, "cancelled")])
        self.assertEqual(body["items"][1], {
            "merchant_key": "spotify", "name": "Spotify", "cadence": "monthly", "amount": "119.00",
            "previous_amount": None, "payments": 3, "first_date": iso(-85), "last_date": iso(-25),
            "next_expected": iso(5), "late": False, "status": "active"})
        netflix = body["items"][2]
        self.assertEqual((netflix["amount"], netflix["previous_amount"], netflix["payments"], netflix["next_expected"]),
                         ("649.00", "499.00", 4, iso(20)))

    async def test_monthly_total(self):
        self.assertEqual((await self.get())["monthly_total"], "0.00")
        await self.series("DMart", 100, [14, 7, 0])
        self.assertEqual((await self.get())["monthly_total"], "433.00")
        await self.series("Spotify", 119, [60, 30, 0])
        await self.series("Zee5", 1200, [365, 0])
        self.assertEqual((await self.get())["monthly_total"], "652.00")

    async def test_total_rounds_half_up_exactly(self):
        await self.series("Chai", Decimal("1.50"), [14, 7, 0])  # 1.50 × 52 / 12 = 6.50
        self.assertEqual((await self.get())["monthly_total"], "7.00")

    async def test_cuj4_total(self):
        await self.series("NoBroker Rent", 18000, [65, 35, 5], category="Rent")
        await self.series("Groww Mutual Fund", 5000, [64, 34, 4], category="Investments & Finance")
        await self.series("Netflix", 649, [63, 33, 3])
        await self.series("Spotify", 119, [62, 32, 2])
        await self.series("Jio", 299, [61, 31, 1])
        self.assertEqual((await self.get())["monthly_total"], "24067.00")

    async def test_dismiss_and_clear(self):
        await self.series("Spotify", 119, [60, 30, 0])
        await self.series("Jio", 299, [61, 31, 1])
        await self.decide("spotify")
        body = await self.get()
        self.assertEqual([s["name"] for s in body["items"]], ["Jio"])
        self.assertEqual([(s["name"], s["status"]) for s in body["hidden"]], [("Spotify", "dismissed")])
        self.assertEqual(body["monthly_total"], "299.00")
        await self.clear("spotify")
        body = await self.get()
        self.assertEqual(([s["name"] for s in body["items"]], body["hidden"], body["monthly_total"]),
                         (["Jio", "Spotify"], [], "418.00"))
        self.assertEqual(await self.decisions(), [])

    async def test_cancel(self):
        await self.series("Spotify", 119, [60, 30, 0])
        await self.series("Jio", 299, [61, 31, 1])
        await self.decide("spotify", status="cancelled")
        body = await self.get()
        self.assertEqual([(s["name"], s["status"]) for s in body["items"]],
                         [("Jio", "active"), ("Spotify", "cancelled")])
        self.assertEqual(body["monthly_total"], "299.00")

    async def test_a_payment_after_cancelling_makes_it_active_again(self):
        await self.series("Spotify", 119, [91, 61, 31])
        with patch("limits.utc_now", return_value=NOW - 5 * DAY):
            await self.decide("spotify", status="cancelled")
        self.assertEqual([s["status"] for s in (await self.get())["items"]], ["cancelled"])
        await self.seed("Spotify", 119, NOW)
        [series] = (await self.get())["items"]
        self.assertEqual((series["status"], series["last_date"]), ("active", iso(0)))
        [row] = await self.decisions()
        self.assertEqual(row.status, "cancelled")

    async def test_one_row_per_merchant_and_cadence(self):
        await self.decide("spotify", status="dismissed")
        await self.decide("Spotify", status="cancelled")
        await self.decide("spotify", "weekly", "dismissed")
        rows = sorted(await self.decisions(), key=lambda r: r.cadence)
        self.assertEqual([(r.merchant_key, r.cadence, r.status) for r in rows],
                         [("spotify", "monthly", "cancelled"), ("spotify", "weekly", "dismissed")])
        await self.clear("nothing here")
        await self.clear("spotify", "yearly")
        self.assertEqual(len(await self.decisions()), 2)

    async def test_keys_are_normalised(self):
        await self.decide("  NETFLIX  ", status="cancelled")
        [row] = await self.decisions()
        self.assertEqual((row.merchant_key, row.merchant_hash, row.user_id),
                         ("netflix", name_hash("netflix"), self.owner.id))
        self.assertEqual(row.decided_at.replace(tzinfo=None), NOW.replace(tzinfo=None))
        await self.clear(" Netflix")
        self.assertEqual(await self.decisions(), [])

    async def test_a_save_that_loses_the_race_updates_the_winner(self):
        # Load the user through the request's own session, as production does, so the rollback after the
        # IntegrityError expires it; the retry must not read it again (a lazy load under asyncio fails).
        owner_id = self.owner.id

        async def real_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
            return await db.get(User, owner_id)

        app.dependency_overrides[current_user] = real_user
        real_scalar, raced = AsyncSession.scalar, []

        async def scalar(session, *args, **kwargs):
            result = await real_scalar(session, *args, **kwargs)
            if not raced:  # another save creates the row between this SELECT and the route's commit
                raced.append(True)
                async with AsyncSessionLocal() as other:
                    other.add(RecurringDecision(user_id=owner_id, merchant_key="netflix",
                                                merchant_hash=name_hash("netflix"), cadence="monthly",
                                                status="cancelled", decided_at=NOW, created_at=NOW))
                    await other.commit()
            return result

        with patch.object(AsyncSession, "scalar", scalar):
            await self.decide("Netflix", status="dismissed")
        self.assertEqual(raced, [True])
        [row] = await self.decisions()
        self.assertEqual((row.merchant_key, row.cadence, row.status, row.user_id),
                         ("netflix", "monthly", "dismissed", owner_id))

    async def test_a_decision_without_a_series_is_ignored(self):
        await self.decide("spotify")
        body = await self.get()
        self.assertEqual((body["items"], body["hidden"]), ([], []))
        self.assertEqual(len(await self.decisions()), 1)

    async def test_auth(self):
        del app.dependency_overrides[current_user]
        self.assertEqual((await self.client.get("/api/v1/recurring")).status_code, 401)
        body = {"merchant_key": "spotify", "cadence": "monthly", "status": "dismissed"}
        response = await self.client.put("/api/v1/recurring/decisions", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, 401)
        response = await self.client.post("/api/v1/recurring/decisions/clear", json=body, headers=ORIGIN)
        self.assertEqual(response.status_code, 401)

    async def test_origin(self):
        body = {"merchant_key": "spotify", "cadence": "monthly", "status": "dismissed"}
        for headers in ({}, {"Origin": "https://evil.example"}):
            response = await self.client.put("/api/v1/recurring/decisions", json=body, headers=headers)
            self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request origin."}))
            response = await self.client.post("/api/v1/recurring/decisions/clear", json=body, headers=headers)
            self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request origin."}))
        self.assertEqual(await self.decisions(), [])

    async def test_validation(self):
        good = {"merchant_key": "spotify", "cadence": "monthly", "status": "dismissed"}
        cases = [
            ({"merchant_key": ""}, "merchant_key", "Value error, Enter a merchant name."),
            ({"merchant_key": "   "}, "merchant_key", "Value error, Enter a merchant name."),
            ({"merchant_key": " unknown "}, "merchant_key", "Value error, Enter a merchant name."),
            ({"merchant_key": "a" * 256}, "merchant_key", "Value error, Merchant names can be up to 255 characters."),
            ({"merchant_key": "Net\x00flix"}, "merchant_key",
             "Value error, Merchant names can't contain invalid characters."),
            ({"merchant_key": "Net\ud800flix"}, "merchant_key",
             "Value error, Merchant names can't contain invalid characters."),
            ({"cadence": "quarterly"}, "cadence", None),
            ({"status": "active"}, "status", None),
        ]
        for change, field, message in cases:
            with self.subTest(change=change):
                # json.dumps escapes a lone surrogate (\ud800), which httpx's own encoder can't send.
                response = await self.client.put("/api/v1/recurring/decisions", headers=JSON_ORIGIN,
                                                 content=json.dumps({**good, **change}).encode())
                self.assertEqual(response.status_code, 422, response.text)
                [error] = response.json()["detail"]
                self.assertEqual(error["loc"], ["body", field])
                if message:
                    self.assertEqual(error["msg"], message)
                else:
                    self.assertEqual(error["type"], "literal_error")
        response = await self.client.post("/api/v1/recurring/decisions/clear", headers=ORIGIN,
                                          json={"merchant_key": "Unknown", "cadence": "monthly"})
        self.assertEqual(response.json()["detail"][0]["msg"], "Value error, Enter a merchant name.")
        self.assertEqual(await self.decisions(), [])
        await self.decide("a" * 255)
        self.assertEqual(len(await self.decisions()), 1)


class NotificationTests(RecurringTestCase):
    async def fire(self, txn_id, cause="sms"):
        await post_save.run(txn_id, cause)

    async def test_cuj1_a_new_subscription(self):
        for days_ago in (61, 30):
            await self.fire(await self.seed("Spotify", 119, NOW - days_ago * DAY))
            self.assertEqual(await self.recurring_notes(), [])
        await self.fire(await self.seed("Spotify", 119, NOW))
        [note] = await self.recurring_notes()
        self.assertEqual((note.kind, note.title, note.body, note.link, note.dedupe_key, note.user_id), (
            "recurring_new", "New recurring payment: Spotify", "₹119 a month, next around 20 Nov",
            "/report/recurring", "recurring_new:monthly:" + name_hash("spotify"), self.owner.id))
        later = NOW + 29 * DAY
        with patch("limits.utc_now", return_value=later):
            await self.fire(await self.seed("Spotify", 119, later))
        self.assertEqual(len(await self.recurring_notes()), 1)

    async def test_no_burst_for_a_long_standing_series(self):
        await self.series("Netflix", 499, [30 * i for i in range(1, 13)])
        await self.fire(await self.seed("Netflix", 499, NOW))
        self.assertEqual(await self.recurring_notes(), [])

    async def test_cuj2_a_price_rise(self):
        await self.series("Netflix", 499, [90, 60, 30])
        txn_id = await self.seed("Netflix", 649, NOW)
        await self.fire(txn_id)
        await self.fire(txn_id, "edit")
        [note] = await self.recurring_notes()
        self.assertEqual((note.kind, note.title, note.body, note.link, note.dedupe_key), (
            "recurring_price", "Netflix went up: ₹649", "Was ₹499. Next around 20 Nov.", "/report/recurring",
            f"recurring_price:{txn_id}"))

    async def test_price_drop(self):
        await self.series("Netflix", 649, [90, 60, 30])
        await self.fire(await self.seed("Netflix", 499, NOW))
        [note] = await self.recurring_notes()
        self.assertEqual((note.kind, note.title, note.body),
                         ("recurring_price", "Netflix went down: ₹499", "Was ₹649. Next around 20 Nov."))

    async def test_an_unsteady_bill_sends_no_price_notification(self):
        await self.seed("BESCOM", 1250, NOW - 90 * DAY)
        await self.seed("BESCOM", 1200, NOW - 60 * DAY)
        await self.seed("BESCOM", 1320, NOW - 30 * DAY)
        await self.fire(await self.seed("BESCOM", 1180, NOW))
        self.assertEqual(await self.recurring_notes(), [])

    async def test_an_edit_to_an_old_payment(self):
        ids = await self.series("Spotify", 119, [61, 30, 0])
        await self.fire(ids[0], "edit")
        await self.fire(ids[1], "sms")  # a late SMS
        self.assertEqual(await self.recurring_notes(), [])

    async def test_a_dismissed_series(self):
        await self.decide("spotify")
        ids = await self.series("Spotify", 119, [61, 30, 0])
        await self.fire(ids[-1])
        self.assertEqual(await self.recurring_notes(), [])
        await self.decide("spotify", "weekly")  # another cadence doesn't hide this one
        await self.clear("spotify")
        await self.fire(ids[-1])
        self.assertEqual([n.kind for n in await self.recurring_notes()], ["recurring_new"])

    async def test_not_counted(self):
        cases = [
            ("Usd Co", {"currency": "USD"}),
            ("Credit Co", {"type": "CREDIT", "category": "Income"}),
            ("Manas", {"category": "Self Transfer"}),
            ("Unknown", {}),
        ]
        for merchant, kwargs in cases:
            ids = await self.series(merchant, 119, [61, 30, 0], **kwargs)
            await self.fire(ids[-1])
        ids = await self.series("Spotify", 119, [61, 30, 0])
        with patch("post_save.OWNER_EMAIL", ""):
            await self.fire(ids[-1])
        self.assertEqual(await self.recurring_notes(), [])

    async def test_held_out_then_not_a_duplicate(self):
        original = await self.seed("Swiggy", 500, NOW - MINUTE)
        ids = await self.series("Spotify", 119, [61, 30, 0])
        await self.flag(ids[-1], original)
        await self.fire(ids[-1])
        self.assertEqual(await self.recurring_notes(), [])
        response = await self.client.post(f"/api/v1/transactions/{ids[-1]}/not-duplicate", headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([n.kind for n in await self.recurring_notes()], ["recurring_new"])

    async def test_paise_are_kept(self):
        await self.series("HDFC Car Loan", Decimal("10688.10"), [61, 30])
        await self.fire(await self.seed("HDFC Car Loan", Decimal("10688.10"), NOW))
        [note] = await self.recurring_notes()
        self.assertTrue(note.body.startswith("₹10,688.10 a month"), note.body)

    async def test_long_names_are_shortened(self):
        name = "A" * 200
        await self.series(name, 119, [61, 30])
        await self.fire(await self.seed(name, 119, NOW))
        [note] = await self.recurring_notes()
        self.assertEqual(note.title, "New recurring payment: " + "A" * 119 + "…")
        self.assertLessEqual(len(note.title), 200)

    async def test_new_when_the_series_had_another_cadence_without_it(self):
        # Unreachable with real dates (a cadence change leaves a run of 2, or the old series stopped), so the
        # rule is checked with detect's answers given.
        ids = await self.series("Spotify", 119, [61, 30, 0])
        days = [TODAY - 61 * DAY, TODAY - 30 * DAY, TODAY]
        with_it = detect("spotify", [Payment(i, d, Decimal(119), "Spotify") for i, d in zip(ids, days)], TODAY)
        weekly = replace(with_it, cadence="weekly")
        with patch("recurring.detect", side_effect=[with_it, weekly]):
            await self.fire(ids[-1])
        [note] = await self.recurring_notes()
        self.assertEqual(note.kind, "recurring_new")

    async def test_weekly_and_yearly_units(self):
        await self.series("DMart", 300, [14, 7])
        await self.fire(await self.seed("DMart", 300, NOW))
        await self.series("Zee5", 699, [365])
        await self.fire(await self.seed("Zee5", 699, NOW))
        self.assertEqual([n.body for n in await self.recurring_notes()],
                         ["₹300 a week, next around 28 Oct", "₹699 a year, next around 21 Oct"])

    async def statements_for_check(self, txn_id) -> int:
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, txn_id, "sms")
            return await self.count_statements(lambda: recurring.check_after_save(ctx))

    async def test_query_budget(self):
        self.assertEqual(await self.statements_for_check(await self.seed("Croma", 700, NOW)), 1)
        await self.series("Spotify", 119, [61, 30])
        self.assertEqual(await self.statements_for_check(await self.seed("Spotify", 119, NOW)), 4)
        self.assertEqual(len(await self.recurring_notes()), 1)


if __name__ == "__main__":
    unittest.main()
