"""Upcoming payments (upcoming.py): the paid / not-seen rules, the list with its notices, expected items, merge,
leaves_bank and order, the query budget, the API and dismiss, and the card-bill reminder job. Isolated in-memory
SQLite; email stays off (no Gmail keys); Gemini stays off."""
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
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, patch

import jobs
import upcoming
from database import AsyncSessionLocal
from limits import IST, _utc
from main import app
from models import Transaction, UpcomingPayment
from test_post_save import NOW, ORIGIN, PostSaveTestCase
from upcoming import Candidate, status

DAY = timedelta(days=1)
TODAY = NOW.astimezone(IST).date()  # Wed 21 Oct 2026
D = date(2026, 12, 12)  # the auto-debit due date in the pure tests
BILL_DUE = date(2026, 12, 5)
BILL_RECEIVED = datetime(2026, 11, 20, 6, 30, tzinfo=timezone.utc)  # 20 Nov, 12:00 IST
NETFLIX_NOTICE = "AutoPay for NETFLIX, debit of Rs 649.00 is scheduled on 12-12-26 from HDFC Bank A/c XX4821"
NETFLIX_DEBIT = "Rs 649.00 debited via UPI AutoPay from HDFC Bank A/c XX4821 to NETFLIX"
CARD_NOTICE = "AutoPay for NETFLIX, Rs 649.00 will be charged on 12-12-26 to your HDFC Bank Credit Card XX9012"
CARD_SPEND = "Rs 119.00 spent on HDFC Bank Credit Card XX9012 at SPOTIFY on 19-10-26. Avl Limit: Rs 50000"
BILL_RAW = "HDFC Bank Credit Card XX9012 statement: Total due Rs 4500.00, minimum due Rs 500.00 by 05-12-26"
RECEIVED = "Payment of Rs 500.00 received on your HDFC Bank Credit Card XX9012. Thank you"
JOBS_SECRET = "test-jobs-secret"


def ist(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=IST)


def cand(type="DEBIT", amount="649", merchant: Optional[str] = "Netflix", raw=NETFLIX_DEBIT,
         category="Entertainment", account_ref="HDFC XX4821", day=D) -> Candidate:
    return Candidate(uuid.uuid4(), type, Decimal(amount), merchant, raw, category, account_ref, day)


def auto_debit(payee="Netflix", amount="649", due=D) -> UpcomingPayment:
    return UpcomingPayment(kind="auto_debit", payee=payee, amount=Decimal(amount), due_date=due,
                           raw_message=NETFLIX_NOTICE, notice_key="x", created_at=ist(due - 3 * DAY))


def card_bill(amount="4500", min_amount: Optional[str] = "500", account_ref: Optional[str] = "HDFC XX9012",
              due=BILL_DUE) -> UpcomingPayment:
    return UpcomingPayment(kind="card_bill", payee="HDFC card XX9012", amount=Decimal(amount),
                           min_amount=None if min_amount is None else Decimal(min_amount), due_date=due,
                           account_ref=account_ref, raw_message=BILL_RAW, notice_key="y", created_at=BILL_RECEIVED)


def credit(amount="500", account_ref="hdfc xx9012", day=date(2026, 11, 25), category="Credit Card Payment",
           type="CREDIT") -> Candidate:
    return cand(type=type, amount=amount, merchant=None, raw=RECEIVED, category=category, account_ref=account_ref,
                day=day)


class StatusTests(unittest.TestCase):
    def check(self, payments, today=D, notice=None) -> upcoming.Settlement:
        return status(notice or auto_debit(), payments, today)

    def test_auto_debit_date_window(self):
        for day in (D - 2 * DAY, D, D + 5 * DAY):
            self.assertEqual(self.check([cand(day=day)]), ("paid", Decimal(0)), day)
        for day in (D - 3 * DAY, D + 6 * DAY):
            self.assertEqual(self.check([cand(day=day)]).status, "upcoming", day)

    def test_auto_debit_amount_within_one_percent(self):
        for amount in ("655.49", "642.51", "649"):
            self.assertEqual(self.check([cand(amount=amount)]).status, "paid", amount)
        for amount in ("656.20", "641.80"):
            self.assertEqual(self.check([cand(amount=amount)]).status, "upcoming", amount)

    def test_name_from_the_sms_when_no_merchant_was_read(self):
        self.assertEqual(self.check([cand(merchant=None)]).status, "paid")

    def test_only_common_words_shared(self):
        payment = cand(merchant="HDFC Bank", raw="Rs 649 debited from HDFC Bank A/c XX4821")
        self.assertEqual(self.check([payment], notice=auto_debit(payee="HDFC Bank Netflix")).status, "upcoming")

    def test_a_credit_does_not_pay_an_auto_debit(self):
        self.assertEqual(self.check([cand(type="CREDIT")]).status, "upcoming")

    def test_auto_debit_not_seen_after_five_days(self):
        self.assertEqual(self.check([], today=D + 5 * DAY), ("upcoming", Decimal(0)))
        self.assertEqual(self.check([], today=D + 6 * DAY), ("not_seen", Decimal(0)))
        self.assertEqual(self.check([cand(day=D - 10 * DAY)], today=D + 6 * DAY), ("not_seen", Decimal(0)))

    def bill(self, payments, notice=None, today=date(2026, 11, 30)) -> upcoming.Settlement:
        return status(notice or card_bill(), payments, today)

    def test_card_bill_paid_at_the_minimum(self):
        self.assertEqual(self.bill([credit("500")]), ("paid", Decimal(500)))
        self.assertEqual(self.bill([credit("499")]), ("upcoming", Decimal(499)))
        self.assertEqual(self.bill([credit("300"), credit("200", day=date(2026, 11, 28))]), ("paid", Decimal(500)))

    def test_card_bill_without_a_minimum_needs_the_total(self):
        notice = card_bill(min_amount=None)
        self.assertEqual(self.bill([credit("4500")], notice), ("paid", Decimal(4500)))
        self.assertEqual(self.bill([credit("4499")], notice), ("upcoming", Decimal(4499)))

    def test_card_bill_payments_that_do_not_count(self):
        self.assertEqual(self.bill([credit(account_ref="HDFC XX1111")]), ("upcoming", Decimal(0)))
        self.assertEqual(self.bill([credit(day=date(2026, 11, 19))]), ("upcoming", Decimal(0)))  # before the notice
        self.assertEqual(self.bill([credit(type="DEBIT", account_ref="HDFC XX4821")]), ("upcoming", Decimal(0)))
        self.assertEqual(self.bill([credit(type="DEBIT")]), ("upcoming", Decimal(0)))
        self.assertEqual(self.bill([credit(category="Refunds & Cashback")]), ("upcoming", Decimal(0)))
        self.assertEqual(self.bill([credit(day=date(2026, 11, 20))]).status, "paid")  # the day it arrived counts
        self.assertEqual(self.bill([credit(account_ref="  HDFC   XX9012 ")]).status, "paid")

    def test_card_bill_without_an_account_is_never_paid(self):
        notice = card_bill(account_ref=None)
        self.assertEqual(self.bill([credit("4500"), credit("4500", account_ref=None)], notice),
                         ("upcoming", Decimal(0)))

    def test_card_bill_not_seen_the_day_after(self):
        self.assertEqual(self.bill([], today=BILL_DUE).status, "upcoming")
        self.assertEqual(self.bill([credit("100")], today=BILL_DUE + DAY), ("not_seen", Decimal(100)))


class UpcomingTestCase(PostSaveTestCase):
    async def notice(self, kind="auto_debit", payee="Netflix", amount="649", due=TODAY + DAY, *,
                     min_amount: Optional[str] = None, account_ref: Optional[str] = None, created=NOW,
                     dismissed: Optional[datetime] = None, raw=NETFLIX_NOTICE) -> uuid.UUID:
        async with AsyncSessionLocal() as db:
            row = UpcomingPayment(kind=kind, payee=payee, amount=Decimal(amount), due_date=due,
                                  min_amount=None if min_amount is None else Decimal(min_amount),
                                  account_ref=account_ref, notice_key=uuid.uuid4().hex, raw_message=raw,
                                  created_at=created, dismissed_at=dismissed)
            db.add(row)
            await db.commit()
            return row.id

    async def bill_notice(self, due=TODAY + DAY, *, min_amount: Optional[str] = "500", **changes) -> uuid.UUID:
        return await self.notice("card_bill", "HDFC card XX9012", "4500", due, min_amount=min_amount,
                                 account_ref="HDFC XX9012", raw=BILL_RAW, **changes)

    async def txn(self, type="DEBIT", amount="649", merchant: Optional[str] = "Netflix", when=NOW, *,
                  category="Entertainment", account_ref: Optional[str] = "HDFC XX4821", raw=NETFLIX_DEBIT,
                  is_parsed=True, currency="INR", user_id=None) -> uuid.UUID:
        self.counter += 1
        async with AsyncSessionLocal() as db:
            row = Transaction(type=type, amount=Decimal(amount), currency=currency, merchant=merchant,
                              category=category, account_ref=account_ref, user_id=user_id, is_parsed=is_parsed,
                              parsed_by="regex", raw_message=raw, message_hash=f"upcoming-{self.counter}",
                              transaction_time=when, created_at=NOW, updated_at=NOW)
            db.add(row)
            await db.commit()
            return row.id

    async def series(self, name="Spotify", amount="119", days=(date(2026, 8, 20), date(2026, 9, 19),
                                                               date(2026, 10, 19)), **changes) -> list[uuid.UUID]:
        """A monthly series: next expected 2026-11-18 with the default days."""
        changes.setdefault("raw", f"Rs {amount}.00 debited from HDFC Bank A/c XX4821 to {name.upper()}")
        return [await self.txn(amount=amount, merchant=name, when=ist(day), **changes) for day in days]

    async def items(self, until=TODAY + 29 * DAY, today=TODAY) -> list[upcoming.UpcomingItem]:
        async with AsyncSessionLocal() as db:
            return await upcoming.list_upcoming(db, self.owner, today, until)

    async def statements(self, until=TODAY + 29 * DAY) -> int:
        async with AsyncSessionLocal() as db:
            return await self.count_statements(lambda: upcoming.list_upcoming(db, self.owner, TODAY, until))


class ListTests(UpcomingTestCase):
    async def statuses(self, **kwargs) -> list[tuple[str, str]]:
        return [(item.payee, item.status) for item in await self.items(**kwargs)]

    async def test_empty(self):
        self.assertEqual(await self.items(), [])
        self.assertEqual(await self.statements(), 3)  # notices + recurring's two

    async def test_a_hand_added_debit_pays_and_deleting_it_unpays(self):
        await self.notice()
        added = await self.manual(649, "Netflix", NOW)
        self.assertEqual(await self.statuses(), [("Netflix", "paid")])
        response = await self.client.delete(f"/api/v1/transactions/{added['id']}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(await self.statuses(), [("Netflix", "upcoming")])

    async def test_payments_that_do_not_count(self):
        await self.notice()
        original = await self.txn(amount="1", merchant="Other", raw="Rs 1.00 debited from HDFC Bank A/c XX4821")
        await self.flag(await self.txn(), original)
        await self.txn(user_id=self.other.id)
        await self.txn(currency="USD")
        await self.txn(is_parsed=False)
        self.assertEqual(await self.statuses(), [("Netflix", "upcoming")])
        await self.txn()
        self.assertEqual(await self.statuses(), [("Netflix", "paid")])

    async def test_dismissed_notices_are_not_returned(self):
        await self.notice(dismissed=NOW)
        await self.notice(payee="Hotstar", amount="299")
        self.assertEqual(await self.statuses(), [("Hotstar", "upcoming")])

    async def test_lookback_and_until_are_inclusive(self):
        until = TODAY + 10 * DAY
        for payee, due in (("Early", TODAY - 8 * DAY), ("First", TODAY - 7 * DAY), ("Last", until),
                           ("Late", until + DAY)):
            await self.notice(payee=payee, due=due)
        self.assertEqual(await self.statuses(until=until), [("First", "not_seen"), ("Last", "upcoming")])

    async def test_expected_from_a_series(self):
        await self.series()
        self.assertEqual(await self.items(until=date(2026, 11, 17)), [])
        [item] = await self.items(until=date(2026, 11, 19))
        self.assertEqual(item, upcoming.UpcomingItem(
            id=None, kind="expected", payee="Spotify", account_ref=None, amount=Decimal("119"), min_amount=None,
            date=date(2026, 11, 18), status="upcoming", paid_amount=Decimal(0), leaves_bank=True))

    async def test_a_late_series(self):
        await self.series(days=(date(2026, 7, 1), date(2026, 7, 31), date(2026, 8, 30)))
        [item] = await self.items()
        self.assertEqual((item.kind, item.date, item.status), ("expected", date(2026, 9, 29), "late"))

    async def test_cancelled_and_dismissed_series_are_not_listed(self):
        await self.series()
        await self.series("Hotstar", "299")
        for key, decision in (("spotify", "cancelled"), ("hotstar", "dismissed")):
            response = await self.client.put("/api/v1/recurring/decisions", headers=ORIGIN,
                                             json={"merchant_key": key, "cadence": "monthly", "status": decision})
            self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(await self.items(), [])

    async def test_a_notice_replaces_the_expected_payment(self):
        """CUJ 4: the price rise is announced by a notice; one Netflix item, at the new price."""
        await self.series("Netflix", "499")
        notice_id = await self.notice(amount="649", due=date(2026, 11, 16))
        [item] = await self.items()
        self.assertEqual((item.id, item.kind, item.amount, item.date),
                         (notice_id, "auto_debit", Decimal("649"), date(2026, 11, 16)))

    async def test_a_dismissed_notice_still_replaces_it(self):
        await self.series("Netflix", "499")
        await self.notice(amount="649", due=date(2026, 11, 16), dismissed=NOW)
        self.assertEqual(await self.items(), [])

    async def test_a_far_or_different_notice_does_not_replace_it(self):
        await self.series("Netflix", "499")
        await self.notice(amount="649", due=date(2026, 11, 26))  # 8 days after 18 Nov
        self.assertEqual([(i.kind, i.payee) for i in await self.items(until=date(2026, 11, 30))],
                         [("expected", "Netflix"), ("auto_debit", "Netflix")])

    async def test_another_payee_does_not_replace_it(self):
        await self.series("Netflix", "499")
        await self.notice(payee="Hotstar", amount="299", due=date(2026, 11, 18))
        self.assertEqual([(i.kind, i.payee) for i in await self.items()],
                         [("auto_debit", "Hotstar"), ("expected", "Netflix")])

    async def test_leaves_bank(self):
        await self.notice(payee="Netflix")
        await self.notice(payee="Prime Video", amount="299", raw=CARD_NOTICE)
        await self.bill_notice()
        await self.series("Spotify")
        await self.series("Gym", "1500", account_ref="Cash", raw="Added by hand in Folio")
        await self.series("Hotstar", "299", account_ref="HDFC XX9012", raw=CARD_SPEND)
        leaves = {item.payee: item.leaves_bank for item in await self.items()}
        self.assertEqual(leaves, {"Netflix": True, "Prime Video": False, "HDFC card XX9012": True, "Spotify": True,
                                  "Gym": False, "Hotstar": False})

    async def test_order(self):
        await self.series("Spotify")  # 18 Nov
        await self.notice(payee="Hotstar", amount="299", due=date(2026, 11, 18))
        await self.notice(payee="apple", amount="99", due=date(2026, 11, 18))
        await self.bill_notice(date(2026, 11, 18))
        await self.notice(payee="Zomato", amount="149", due=date(2026, 11, 10))
        self.assertEqual([(i.date.day, i.kind, i.payee) for i in await self.items()], [
            (10, "auto_debit", "Zomato"), (18, "card_bill", "HDFC card XX9012"), (18, "auto_debit", "apple"),
            (18, "auto_debit", "Hotstar"), (18, "expected", "Spotify"),
        ])

    async def test_full_read_budget(self):
        await self.series()
        await self.notice()
        await self.bill_notice()
        self.assertEqual(await self.statements(), 5)
        await self.notice(payee="Old", dismissed=NOW)
        self.assertEqual(await self.statements(), 5)


class ApiTests(UpcomingTestCase):
    async def get(self, **params):
        return await self.client.get("/api/v1/upcoming", params=params)

    async def test_shape(self):
        bill_id = await self.bill_notice(TODAY + 5 * DAY)
        debit_id = await self.notice(due=TODAY + 3 * DAY)
        await self.txn("CREDIT", "500", None, category="Credit Card Payment", account_ref="HDFC XX9012", raw=RECEIVED)
        await self.series()
        response = await self.get()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"currency": "INR", "items": [
            {"id": str(debit_id), "kind": "auto_debit", "payee": "Netflix", "account_ref": None, "amount": "649.00",
             "min_amount": None, "date": "2026-10-24", "status": "upcoming", "paid_amount": "0.00",
             "leaves_bank": True},
            {"id": str(bill_id), "kind": "card_bill", "payee": "HDFC card XX9012", "account_ref": "HDFC XX9012",
             "amount": "4500.00", "min_amount": "500.00", "date": "2026-10-26", "status": "paid",
             "paid_amount": "500.00", "leaves_bank": True},
            {"id": None, "kind": "expected", "payee": "Spotify", "account_ref": None, "amount": "119.00",
             "min_amount": None, "date": "2026-11-18", "status": "upcoming", "paid_amount": "0.00",
             "leaves_bank": True},
        ]})

    async def test_days(self):
        await self.notice(payee="Inside", due=TODAY + 29 * DAY)
        await self.notice(payee="Outside", due=TODAY + 30 * DAY)
        await self.notice(payee="Far", due=TODAY + 61 * DAY)
        self.assertEqual([i["payee"] for i in (await self.get()).json()["items"]], ["Inside"])
        self.assertEqual([i["payee"] for i in (await self.get(days=62)).json()["items"]], ["Inside", "Outside", "Far"])
        self.assertEqual([i["payee"] for i in (await self.get(days=1)).json()["items"]], [])
        for days in (0, 63, "x"):
            self.assertEqual((await self.get(days=days)).status_code, 422, days)

    async def test_needs_a_session(self):
        app.dependency_overrides.clear()
        self.assertEqual((await self.get()).status_code, 401)

    async def dismissed_at(self, notice_id) -> Optional[datetime]:
        async with AsyncSessionLocal() as db:
            value = (await db.get(UpcomingPayment, notice_id)).dismissed_at
            return None if value is None else _utc(value)

    async def dismiss(self, item_id, headers=ORIGIN):
        return await self.client.post(f"/api/v1/upcoming/{item_id}/dismiss", headers=headers)

    async def test_dismiss(self):
        notice_id = await self.notice()
        response = await self.dismiss(notice_id)
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertEqual(await self.dismissed_at(notice_id), NOW)
        self.assertEqual((await self.get()).json()["items"], [])
        with patch("limits.utc_now", return_value=NOW + DAY):
            again = await self.dismiss(notice_id)
        self.assertEqual((again.status_code, again.content), (204, b""))
        self.assertEqual(await self.dismissed_at(notice_id), NOW)

    async def test_dismiss_errors(self):
        notice_id = await self.notice()
        missing = await self.dismiss(uuid.uuid4())
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Upcoming payment not found."}))
        self.assertEqual((await self.dismiss("not-a-uuid")).status_code, 422)
        forbidden = await self.dismiss(notice_id, headers={})
        self.assertEqual((forbidden.status_code, forbidden.json()), (403, {"detail": "Invalid request origin."}))
        app.dependency_overrides.clear()
        self.assertEqual((await self.dismiss(notice_id)).status_code, 401)
        self.assertIsNone(await self.dismissed_at(notice_id))

    async def test_openapi(self):
        paths = app.openapi()["paths"]
        self.assertEqual(list(paths["/api/v1/upcoming"]), ["get"])
        self.assertEqual(list(paths["/api/v1/upcoming/{item_id}/dismiss"]), ["post"])
        self.assertEqual(paths["/api/v1/upcoming"]["get"]["tags"], ["Upcoming payments"])


EVE = BILL_DUE - DAY  # 4 Dec


class ReminderTests(UpcomingTestCase):
    async def run_daily(self, today=EVE) -> None:
        async with AsyncSessionLocal() as db:
            await upcoming.daily(db, self.owner, today)

    async def reminders(self) -> list[tuple[str, str, str, str]]:
        return [(n.title, n.body, n.link, n.dedupe_key) for n in await self.notifications("card_bill_due")]

    async def test_bill_due_tomorrow(self):
        bill_id = await self.bill_notice(BILL_DUE)
        await self.run_daily()
        expected = [("HDFC card XX9012 bill due tomorrow", "₹4,500 due on 5 Dec (minimum ₹500). No payment seen yet.",
                     "/upcoming", f"card_bill_due:{bill_id}")]
        self.assertEqual(await self.reminders(), expected)
        await self.run_daily()
        await self.run_daily(BILL_DUE)
        self.assertEqual(await self.reminders(), expected)
        [notification] = await self.notifications("card_bill_due")
        self.assertEqual(notification.user_id, self.owner.id)

    async def test_bill_due_today_without_a_minimum(self):
        bill_id = await self.bill_notice(BILL_DUE, min_amount=None)
        await self.run_daily(BILL_DUE)
        self.assertEqual(await self.reminders(), [("HDFC card XX9012 bill due today",
                                                   "₹4,500 due on 5 Dec. No payment seen yet.", "/upcoming",
                                                   f"card_bill_due:{bill_id}")])

    async def test_no_reminder_once_paid(self):
        await self.bill_notice(BILL_DUE, created=BILL_RECEIVED)
        await self.txn("CREDIT", "500", None, ist(date(2026, 11, 25)), category="Credit Card Payment",
                       account_ref="HDFC XX9012", raw=RECEIVED)
        await self.run_daily()
        self.assertEqual(await self.reminders(), [])

    async def test_no_reminder(self):
        await self.bill_notice(BILL_DUE + DAY)  # due in 2 days
        await self.bill_notice(BILL_DUE, dismissed=NOW)
        await self.notice(due=BILL_DUE)  # an auto-debit
        await self.bill_notice(EVE - DAY)  # due yesterday: not seen
        await self.run_daily()
        self.assertEqual(await self.reminders(), [])

    async def test_a_failure_is_logged_not_raised(self):
        await self.bill_notice(BILL_DUE)
        with patch.object(upcoming, "list_upcoming", AsyncMock(side_effect=RuntimeError("bug"))), \
                self.assertLogs("upcoming", "ERROR") as logs:
            await self.run_daily()
        self.assertIn("Upcoming payments reminder failed", logs.output[0])
        self.assertEqual(await self.reminders(), [])

    async def test_through_the_daily_trigger(self):
        await self.bill_notice(BILL_DUE)
        headers = {"X-Jobs-Secret": JOBS_SECRET}
        evening = ist(EVE, 20)
        with patch.dict(os.environ, {"JOBS_SECRET": JOBS_SECRET}), patch.object(jobs, "now", return_value=evening):
            with patch.object(jobs, "OWNER_EMAIL", ""):
                response = await self.client.post("/api/v1/jobs/daily", params={"force": "true"}, headers=headers)
                self.assertEqual((response.status_code, response.json()), (200, {"ran": []}))
            self.assertEqual(await self.reminders(), [])
            response = await self.client.post("/api/v1/jobs/daily", params={"force": "true"}, headers=headers)
        self.assertEqual((response.status_code, response.json()),
                         (200, {"ran": ["watchdog", "upcoming_reminders", "refund_overdue"]}))
        self.assertEqual([title for title, *_ in await self.reminders()], ["HDFC card XX9012 bill due tomorrow"])


if __name__ == "__main__":
    unittest.main()
