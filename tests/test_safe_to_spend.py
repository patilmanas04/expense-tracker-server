"""Safe to spend until payday (safe_to_spend.py): the payday rule and its states, the per-item rules, the whole
figure against the database (CUJs 1, 2, 4 and 5, boundaries, card or cash, paid and not seen), the statement
budget and the API. Isolated in-memory SQLite; email stays off (no Gmail keys); Gemini stays off."""
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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select

import safe_to_spend
import upcoming
from database import AsyncSessionLocal
from main import app
from models import BalanceSetting
from safe_to_spend import Payday, payday, subtract
from test_post_save import NOW, ORIGIN
from test_upcoming import BILL_RAW, CARD_NOTICE, CARD_SPEND, RECEIVED, UpcomingTestCase, ist

DAY = timedelta(days=1)
CUJ_TODAY = date(2026, 11, 18)
MONTHLY = (date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1))  # → payday 1 Dec
SALARY_RAW = "Rs 90000 credited to HDFC Bank A/c XX4821 NEFT ACME SALARY"
SIP_RAW = "AutoPay for GROWW SIP, debit of Rs 5000.00 is scheduled on 28-11-26 from HDFC Bank A/c XX4821"


class PaydayTests(unittest.TestCase):
    def test_cuj1_upcoming(self):
        self.assertEqual(payday(MONTHLY, CUJ_TODAY), Payday(date(2026, 12, 1), "upcoming", date(2026, 11, 30), 13))

    def test_payday_today(self):
        self.assertEqual(payday(MONTHLY, date(2026, 12, 1)),
                         Payday(date(2026, 12, 1), "today", date(2026, 12, 1), None))

    def test_late(self):
        self.assertEqual(payday(MONTHLY, date(2026, 12, 3)),
                         Payday(date(2026, 12, 1), "late", date(2026, 12, 3), None))

    def test_salary_on_the_last_day(self):
        days = (date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 31))
        self.assertEqual(payday(days, CUJ_TODAY), Payday(date(2026, 11, 30), "upcoming", date(2026, 11, 29), 12))

    def test_month_end_with_fewer_than_two_salaries(self):
        expected = Payday(date(2026, 12, 1), "month_end", date(2026, 11, 30), 13)
        self.assertEqual(payday([], CUJ_TODAY), expected)
        self.assertEqual(payday([date(2026, 11, 1)], CUJ_TODAY), expected)

    def test_month_end_with_one_salary_in_the_window(self):
        self.assertEqual(payday([date(2026, 6, 1), date(2026, 11, 1)], CUJ_TODAY).state, "month_end")
        # 120 days back is inclusive: 21 Jul is on the edge for 18 Nov.
        self.assertEqual(payday([date(2026, 7, 21), date(2026, 11, 1)], CUJ_TODAY).state, "upcoming")
        self.assertEqual(payday([date(2026, 7, 20), date(2026, 11, 1)], CUJ_TODAY).state, "month_end")

    def test_month_end_across_the_year(self):
        self.assertEqual(payday([], date(2026, 12, 31)),
                         Payday(date(2027, 1, 1), "month_end", date(2026, 12, 31), 1))


def item(kind="auto_debit", amount="4500", status="upcoming", paid="0", leaves_bank=True,
         min_amount: Optional[str] = None) -> upcoming.UpcomingItem:
    return upcoming.UpcomingItem(id=None, kind=kind, payee="X", account_ref=None, amount=Decimal(amount),
                                 min_amount=None if min_amount is None else Decimal(min_amount),
                                 date=CUJ_TODAY, status=status, paid_amount=Decimal(paid), leaves_bank=leaves_bank)


class SubtractTests(unittest.TestCase):
    def test_card_or_cash(self):
        for kind in ("auto_debit", "expected"):
            for status in ("upcoming", "late", "paid", "not_seen"):
                self.assertEqual(subtract(item(kind, status=status, leaves_bank=False)), (0, "card_or_cash"))

    def test_card_bill_subtracts_what_is_left(self):
        def bill(paid, status="upcoming"):
            return subtract(item("card_bill", status=status, paid=paid, min_amount="500"))

        self.assertEqual(bill("1000"), (Decimal(3500), None))
        self.assertEqual(bill("500", "paid"), (Decimal(4000), None))  # the minimum reached, the rest still due
        self.assertEqual(bill("4500", "paid"), (0, "paid"))
        self.assertEqual(bill("5000", "paid"), (0, "paid"))
        self.assertEqual(bill("0", "not_seen"), (Decimal(4500), None))

    def test_by_status(self):
        self.assertEqual(subtract(item("auto_debit")), (Decimal(4500), None))
        self.assertEqual(subtract(item("expected", status="late")), (Decimal(4500), None))
        self.assertEqual(subtract(item("auto_debit", status="paid")), (0, "paid"))
        self.assertEqual(subtract(item("auto_debit", status="not_seen")), (0, "not_seen"))
        self.assertEqual(subtract(item("expected", status="not_seen")), (0, "not_seen"))


class SafeTestCase(UpcomingTestCase):
    async def set_balance(self, amount, as_of=ist(date(2026, 11, 17))) -> None:
        async with AsyncSessionLocal() as db:
            db.add(BalanceSetting(user_id=self.owner.id, opening_balance=Decimal(str(amount)), currency="INR",
                                  as_of=as_of, created_at=NOW, updated_at=NOW))
            await db.commit()

    async def salaries(self, *days: date) -> None:
        for day in days:
            await self.txn("CREDIT", "90000", "Acme", when=ist(day), category="Salary", raw=SALARY_RAW)

    async def due(self, payee, amount, day, raw=SIP_RAW, **changes):
        return await self.notice(payee=payee, amount=amount, due=day, raw=raw, **changes)

    async def setting(self, db) -> BalanceSetting:
        return await db.scalar(select(BalanceSetting).where(BalanceSetting.user_id == self.owner.id))

    async def compute(self, today=CUJ_TODAY) -> safe_to_spend.SafeToSpend:
        async with AsyncSessionLocal() as db:
            return await safe_to_spend.compute(db, self.owner, await self.setting(db), today)


class ComputeTests(SafeTestCase):
    async def test_cuj1(self):
        await self.set_balance(40000)
        await self.salaries(*MONTHLY)
        await self.series("Rent", "18000", days=(date(2026, 8, 25), date(2026, 9, 25), date(2026, 10, 25)))
        await self.due("Groww SIP", "5000", date(2026, 11, 28))
        result = await self.compute()
        self.assertEqual(result, safe_to_spend.SafeToSpend(
            currency="INR", balance="40000.00", next_payday=date(2026, 12, 1), payday_state="upcoming", days=13,
            due=[
                # Gaps of 31 and 30 days: a 30-day period, so next expected is 24 Nov.
                safe_to_spend.SafeItem(date=date(2026, 11, 24), payee="Rent", kind="expected", status="upcoming",
                                       amount="18000.00", min_amount=None, paid_amount="0.00",
                                       subtracted="18000.00", note=None),
                safe_to_spend.SafeItem(date=date(2026, 11, 28), payee="Groww SIP", kind="auto_debit",
                                       status="upcoming", amount="5000.00", min_amount=None, paid_amount="0.00",
                                       subtracted="5000.00", note=None),
            ],
            safe="17000.00", per_day="1307.00"))

    async def payees(self, today) -> list[str]:
        return [i.payee for i in (await self.compute(today)).due]

    async def test_payday_boundary(self):
        await self.set_balance(40000)
        await self.salaries(*MONTHLY)
        await self.due("Before", "1000", date(2026, 11, 30))
        await self.due("Payday", "1000", date(2026, 12, 1))
        result = await self.compute()
        self.assertEqual([(i.payee, i.subtracted) for i in result.due], [("Before", "1000.00")])
        self.assertEqual(result.safe, "39000.00")

    async def test_month_end_without_salaries(self):
        await self.set_balance(40000)
        await self.due("Last day", "1000", date(2026, 11, 30))
        await self.due("Next month", "1000", date(2026, 12, 1))
        result = await self.compute()
        self.assertEqual((result.payday_state, result.next_payday, result.days),
                         ("month_end", date(2026, 12, 1), 13))
        self.assertEqual([i.payee for i in result.due], ["Last day"])
        self.assertEqual((result.safe, result.per_day), ("39000.00", "3000.00"))  # 39000 / 13

    async def test_late_cuj5(self):
        await self.set_balance(40000)
        await self.salaries(*MONTHLY)
        for payee, day in (("Yesterday", 2), ("Today", 3), ("Tomorrow", 4)):
            await self.due(payee, "1000", date(2026, 12, day))
        result = await self.compute(date(2026, 12, 3))
        self.assertEqual((result.payday_state, result.next_payday, result.days, result.per_day),
                         ("late", date(2026, 12, 1), None, None))
        self.assertEqual([(i.payee, i.subtracted, i.note) for i in result.due],
                         [("Yesterday", "1000.00", None), ("Today", "1000.00", None)])
        self.assertEqual(result.safe, "38000.00")

    async def test_payday_today(self):
        await self.set_balance(40000)
        await self.salaries(*MONTHLY)
        await self.due("Today", "1000", date(2026, 12, 1))
        await self.due("Tomorrow", "1000", date(2026, 12, 2))
        result = await self.compute(date(2026, 12, 1))
        self.assertEqual((result.payday_state, result.days, result.per_day), ("today", None, None))
        self.assertEqual([i.payee for i in result.due], ["Today"])
        self.assertEqual(result.safe, "39000.00")

    async def test_cuj4_card_bill_part_paid(self):
        await self.set_balance(40000)
        await self.salaries(date(2026, 9, 10), date(2026, 10, 10), date(2026, 11, 10))
        await self.bill_notice(date(2026, 12, 5), created=ist(date(2026, 11, 20)))
        await self.txn("CREDIT", "1000", None, when=ist(date(2026, 11, 22)), category="Credit Card Payment",
                       account_ref="HDFC XX9012", raw=RECEIVED)
        result = await self.compute(date(2026, 11, 25))
        self.assertEqual((result.next_payday, result.days), (date(2026, 12, 10), 15))
        [bill] = result.due
        self.assertEqual((bill.kind, bill.status, bill.amount, bill.min_amount, bill.paid_amount, bill.subtracted,
                          bill.note), ("card_bill", "paid", "4500.00", "500.00", "1000.00", "3500.00", None))
        self.assertEqual(Decimal(result.safe), Decimal(result.balance) - 3500)
        await self.txn("CREDIT", "3500", None, when=ist(date(2026, 11, 24)), category="Credit Card Payment",
                       account_ref="HDFC XX9012", raw=RECEIVED)
        result = await self.compute(date(2026, 11, 25))
        [bill] = result.due
        self.assertEqual((bill.paid_amount, bill.subtracted, bill.note), ("4500.00", "0.00", "paid"))
        self.assertEqual(result.safe, result.balance)

    async def test_card_or_cash(self):
        await self.set_balance(40000)
        await self.salaries(*MONTHLY)
        await self.series("Hotstar", "299", account_ref="HDFC XX9012", raw=CARD_SPEND)
        await self.series("Gym", "1500", account_ref="Cash", raw="Added by hand in Folio")
        await self.due("Prime Video", "299", date(2026, 11, 20), raw=CARD_NOTICE)
        result = await self.compute()
        self.assertEqual({i.payee: (i.subtracted, i.note) for i in result.due}, {
            "Hotstar": ("0.00", "card_or_cash"), "Gym": ("0.00", "card_or_cash"),
            "Prime Video": ("0.00", "card_or_cash")})
        self.assertEqual((result.safe, result.balance), ("40000.00", "40000.00"))

    async def test_paid_and_not_seen(self):
        await self.set_balance(40000)
        await self.salaries(*MONTHLY)
        await self.due("Netflix", "649", date(2026, 11, 16))
        await self.txn(when=ist(date(2026, 11, 16)))  # a matching Netflix debit (before the balance's as_of)
        await self.due("Hotstar", "299", CUJ_TODAY - 6 * DAY)
        result = await self.compute()
        self.assertEqual([(i.payee, i.status, i.subtracted, i.note) for i in result.due],
                         [("Hotstar", "not_seen", "0.00", "not_seen"), ("Netflix", "paid", "0.00", "paid")])
        self.assertEqual(result.safe, "40000.00")

    async def test_late_expected_item_is_subtracted(self):
        await self.set_balance(40000, as_of=ist(date(2026, 10, 20)))
        await self.salaries(date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1))
        await self.series(days=(date(2026, 7, 1), date(2026, 7, 31), date(2026, 8, 30)))
        result = await self.compute(date(2026, 10, 21))
        self.assertEqual((result.next_payday, result.payday_state, result.days),
                         (date(2026, 11, 1), "upcoming", 11))
        [late] = result.due
        self.assertEqual((late.date, late.status, late.subtracted, late.note),
                         (date(2026, 9, 29), "late", "119.00", None))
        self.assertEqual(result.safe, "39881.00")

    async def test_short_cuj2(self):
        await self.set_balance(12000)
        await self.salaries(*MONTHLY)
        await self.due("Rent", "10000", date(2026, 11, 25))
        await self.due("Groww SIP", "5200", date(2026, 11, 28))
        result = await self.compute()
        self.assertEqual((result.safe, result.per_day, result.days), ("-3200.00", None, 13))

    async def test_exactly_zero(self):
        await self.set_balance(5000)
        await self.salaries(*MONTHLY)
        await self.due("Groww SIP", "5000", date(2026, 11, 28))
        result = await self.compute()
        self.assertEqual((result.safe, result.per_day, result.days), ("0.00", None, 13))

    async def statements(self, today=CUJ_TODAY) -> int:
        async with AsyncSessionLocal() as db:
            setting = await self.setting(db)
            return await self.count_statements(lambda: safe_to_spend.compute(db, self.owner, setting, today))

    async def test_statement_budget(self):
        await self.set_balance(40000)
        await self.salaries(*MONTHLY)
        self.assertEqual(await self.statements(), 5)  # summarize, salary_days, notices, recurring's two
        await self.due("Groww SIP", "5000", date(2026, 11, 28))
        await self.series()  # next expected 18 Nov
        self.assertEqual(len((await self.compute()).due), 2)
        self.assertEqual(await self.statements(), 7)  # + payments + newest


class ApiTests(SafeTestCase):
    async def get(self):
        return await self.client.get("/api/v1/balance/safe-to-spend")

    async def test_no_balance(self):
        response = await self.get()
        self.assertEqual((response.status_code, response.json()), (404, {"detail": "No balance set."}))

    async def test_needs_a_session(self):
        await self.set_balance(40000)
        app.dependency_overrides.clear()
        self.assertEqual((await self.get()).status_code, 401)

    async def test_shape(self):
        """The clock is NOW (Wed 21 Oct 2026, 12:00 IST)."""
        await self.set_balance(40000, as_of=ist(date(2026, 10, 20)))
        await self.salaries(date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1))
        await self.due("Groww SIP", "5000", date(2026, 10, 25))
        response = await self.get()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "currency": "INR", "balance": "40000.00", "next_payday": "2026-11-01", "payday_state": "upcoming",
            "days": 11,
            "due": [{"date": "2026-10-25", "payee": "Groww SIP", "kind": "auto_debit", "status": "upcoming",
                     "amount": "5000.00", "min_amount": None, "paid_amount": "0.00", "subtracted": "5000.00",
                     "note": None}],
            "safe": "35000.00", "per_day": "3181.00",  # floor(35000 / 11)
        })

    async def test_openapi_and_other_balance_routes(self):
        paths = app.openapi()["paths"]
        self.assertEqual(list(paths["/api/v1/balance/safe-to-spend"]), ["get"])
        self.assertEqual(paths["/api/v1/balance/safe-to-spend"]["get"]["tags"], ["Balance"])
        await self.set_balance(40000)
        summary = await self.client.get("/api/v1/balance")
        self.assertEqual((summary.status_code, summary.json()["current"]), (200, 40000.0))
        self.assertEqual((await self.client.get("/api/v1/balance/breakdown")).status_code, 200)
        cleared = await self.client.delete("/api/v1/balance", headers=ORIGIN)
        self.assertIn(cleared.status_code, (200, 204), cleared.text)
        self.assertEqual((await self.get()).status_code, 404)


if __name__ == "__main__":
    unittest.main()
