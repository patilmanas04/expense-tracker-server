"""Refund watch (refunds.py): marking a payment, the suggestion when a credit arrives, Yes / No, the notification
buttons, the overdue reminder job, and cleanup when either transaction is deleted. Isolated in-memory SQLite; email
stays off (no Gmail keys); Gemini stays off."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import re
import unittest
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import jobs
import notifications
import post_save
import refunds
from database import AsyncSessionLocal
from limits import IST
from main import app
from models import Notification, NotificationDismissal, RefundWatch, Transaction
from notifications import create_once
from test_post_save import NOW, ORIGIN, PostSaveTestCase

DAY = timedelta(days=1)
MINUTE = timedelta(minutes=1)
TODAY = date(2026, 10, 21)
assert NOW.astimezone(IST).date() == TODAY
DUE = TODAY + 10 * DAY  # 31 Oct
JOBS_SECRET = "test-jobs-secret"

MYNTRA_DEBIT = ("Rs.2499.00 debited from HDFC Bank A/c XX4821 on 16-10-26 to VPA myntra@icici (UPI Ref No 629000100001). "
                "Not you? Call 18002586161")
MYNTRA_REFUND = ("Myntra Designs BANGALORE refund of Rs 2,499.00 credited to your HDFC Bank A/c XX4821 on 21-10-26. "
                 "Avl Bal Rs 40,000.00")
FRIEND_CREDIT = "Rs.2499.00 credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA rahul@okaxis (UPI Ref No 629400100002)"


def debit_sms(amount="2499.00", vpa="myntra@icici", ref="629000100001") -> str:
    return (f"Rs.{amount} debited from HDFC Bank A/c XX4821 on 16-10-26 to VPA {vpa} (UPI Ref No {ref}). "
            "Not you? Call 18002586161")


def refund_sms(amount="2,499.00", balance="40,000.00") -> str:
    return (f"Myntra Designs BANGALORE refund of Rs {amount} credited to your HDFC Bank A/c XX4821 on 21-10-26. "
            f"Avl Bal Rs {balance}")


def friend_sms(amount="2499.00", ref="629400100002") -> str:
    return f"Rs.{amount} credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA rahul@okaxis (UPI Ref No {ref})"


class RefundTestCase(PostSaveTestCase):
    async def txn(self, type="DEBIT", amount="2499", merchant: Optional[str] = "Myntra", when=NOW - 5 * DAY, *,
                  category="Shopping", raw="fixture", is_parsed=True, currency="INR", user_id=None) -> uuid.UUID:
        self.counter += 1
        async with AsyncSessionLocal() as db:
            row = Transaction(type=type, amount=Decimal(amount), currency=currency, merchant=merchant,
                              category=category, user_id=user_id, is_parsed=is_parsed, parsed_by="regex",
                              raw_message=raw, message_hash=f"refund-{self.counter}", account_ref="HDFC XX4821",
                              transaction_time=when, created_at=NOW, updated_at=NOW)
            db.add(row)
            await db.commit()
            return row.id

    async def credit(self, amount="2499", merchant: Optional[str] = "Myntra Designs", when=NOW, *, cause="sms",
                     **changes) -> uuid.UUID:
        """A credit saved straight to the database, then the after-save checks for `cause`."""
        credit_id = await self.txn("CREDIT", amount, merchant, when, **changes)
        await post_save.run(credit_id, cause)
        return credit_id

    async def sms_id(self, message: str, when=NOW) -> uuid.UUID:
        return uuid.UUID((await self.sms(message, when))["id"])

    async def mark(self, txn_id, amount="2499", due=DUE, headers=ORIGIN):
        return await self.client.post("/api/v1/refunds", headers=headers, json={
            "transaction_id": str(txn_id), "amount": amount, "due_date": due.isoformat()})

    async def watched(self, message=MYNTRA_DEBIT, when=NOW - 5 * DAY, amount="2499", due=DUE) -> uuid.UUID:
        debit_id = await self.sms_id(message, when)
        response = await self.mark(debit_id, amount, due)
        self.assertEqual(response.status_code, 201, response.text)
        return debit_id

    async def watched_seed(self, amount="2499", expected=None, **changes) -> uuid.UUID:
        debit_id = await self.txn("DEBIT", amount, **changes)
        response = await self.mark(debit_id, expected or amount)
        self.assertEqual(response.status_code, 201, response.text)
        return debit_id

    async def get(self, txn_id):
        return await self.client.get(f"/api/v1/refunds/{txn_id}")

    async def cancel(self, txn_id, headers=ORIGIN):
        return await self.client.delete(f"/api/v1/refunds/{txn_id}", headers=headers)

    async def accept(self, debit_id, credit_id, headers=ORIGIN):
        return await self.client.post(f"/api/v1/refunds/{debit_id}/accept/{credit_id}", headers=headers)

    async def reject(self, debit_id, credit_id, headers=ORIGIN):
        return await self.client.post(f"/api/v1/refunds/{debit_id}/reject/{credit_id}", headers=headers)

    async def delete_txn(self, txn_id):
        response = await self.client.delete(f"/api/v1/transactions/{txn_id}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204, response.text)

    async def listed(self) -> dict[str, dict]:
        response = await self.client.get("/api/v1/transactions", params={"limit": 200})
        self.assertEqual(response.status_code, 200, response.text)
        return {row["id"]: row for row in response.json()}

    async def watch(self, txn_id) -> Optional[RefundWatch]:
        async with AsyncSessionLocal() as db:
            return await db.scalar(select(RefundWatch).where(RefundWatch.transaction_id == txn_id))

    async def watch_count(self) -> int:
        async with AsyncSessionLocal() as db:
            return len(list(await db.scalars(select(RefundWatch.id))))

    async def updated_at(self, txn_id) -> datetime:
        async with AsyncSessionLocal() as db:
            return await db.scalar(select(Transaction.updated_at).where(Transaction.id == txn_id))

    async def matches(self) -> list[tuple]:
        return [(n.title, n.body, n.link, n.dedupe_key) for n in await self.notifications(refunds.MATCH_KIND)]

    async def keys(self) -> list[str]:
        return [n.dedupe_key for n in await self.notifications(refunds.MATCH_KIND)]

    async def inbox(self, kind=refunds.MATCH_KIND) -> list[dict]:
        response = await self.client.get("/api/v1/notifications")
        self.assertEqual(response.status_code, 200, response.text)
        return [item for item in response.json()["items"] if item["kind"] == kind]


class MarkTests(RefundTestCase):
    async def test_mark_shape_and_listing(self):
        debit_id = await self.sms_id(MYNTRA_DEBIT, NOW - 5 * DAY)
        other_id = await self.sms_id(FRIEND_CREDIT, NOW - 4 * DAY)
        response = await self.mark(debit_id)
        self.assertEqual((response.status_code, response.json()), (201, {
            "transaction_id": str(debit_id), "expected_amount": "2499.00", "due_date": "2026-10-31",
            "credit_id": None, "state": "expected"}))
        rows = await self.listed()
        self.assertEqual((rows[str(debit_id)]["refund"], rows[str(debit_id)]["refund_for"]), ("expected", None))
        self.assertEqual((rows[str(other_id)]["refund"], rows[str(other_id)]["refund_for"]), (None, None))
        watch = await self.watch(debit_id)
        self.assertEqual((watch.user_id, watch.expected_amount, watch.due_date, watch.credit_id, watch.closed_at),
                         (self.owner.id, Decimal("2499.00"), DUE, None, None))

    async def test_a_partial_refund(self):
        debit_id = await self.txn()
        response = await self.mark(debit_id, "1000.50")
        self.assertEqual((response.status_code, response.json()["expected_amount"]), (201, "1000.50"))

    async def test_validation_texts(self):
        debit_id = await self.txn()
        credit_id = await self.txn("CREDIT", "2499")
        unread_id = await self.txn("DEBIT", "0", None, is_parsed=False)
        cases = [
            (credit_id, "2499", DUE, "Only payments can expect a refund."),
            (unread_id, "2499", DUE, "Only payments can expect a refund."),
            (debit_id, "0", DUE, "Enter a refund amount more than zero."),
            (debit_id, "-5", DUE, "Enter a refund amount more than zero."),
            (debit_id, "2499.01", DUE, "The refund can't be more than the payment (₹2,499)."),
            (debit_id, "2499", TODAY, "Pick a date 1 to 60 days from today."),
            (debit_id, "2499", TODAY + 61 * DAY, "Pick a date 1 to 60 days from today."),
            (debit_id, "2499", TODAY - DAY, "Pick a date 1 to 60 days from today."),
        ]
        for txn_id, amount, due, detail in cases:
            response = await self.mark(txn_id, amount, due)
            self.assertEqual((response.status_code, response.json()), (422, {"detail": detail}), (amount, due))
        response = await self.mark(debit_id, "10.001")
        self.assertEqual(response.status_code, 422)
        self.assertIsInstance(response.json()["detail"], list)
        for body in ({"transaction_id": "nope", "amount": "1", "due_date": DUE.isoformat()},
                     {"transaction_id": str(debit_id), "amount": "1", "due_date": "31-10-2026"},
                     {"transaction_id": str(debit_id), "amount": "abc", "due_date": DUE.isoformat()}):
            response = await self.client.post("/api/v1/refunds", headers=ORIGIN, json=body)
            self.assertEqual(response.status_code, 422, body)
            self.assertIsInstance(response.json()["detail"], list)
        self.assertEqual(await self.watch_count(), 0)

    async def test_a_partial_payment_text_keeps_paise(self):
        debit_id = await self.txn(amount="2499.50")
        response = await self.mark(debit_id, "2499.51")
        self.assertEqual(response.json(), {"detail": "The refund can't be more than the payment (₹2,499.50)."})

    async def test_date_bounds(self):
        first, second = await self.txn(), await self.txn(when=NOW - 4 * DAY)
        self.assertEqual((await self.mark(first, due=TODAY + DAY)).status_code, 201)
        response = await self.mark(second, due=TODAY + 60 * DAY)
        self.assertEqual((response.status_code, response.json()["due_date"]), (201, "2026-12-20"))

    async def test_one_watch_per_payment(self):
        debit_id = await self.txn()
        self.assertEqual((await self.mark(debit_id)).status_code, 201)
        response = await self.mark(debit_id, "100")
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": "A refund is already expected for this payment."}))
        credit_id = await self.txn("CREDIT", when=NOW)
        self.assertEqual((await self.accept(debit_id, credit_id)).status_code, 200)
        response = await self.mark(debit_id, "100")
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": "A refund is already expected for this payment."}))
        self.assertEqual((await self.cancel(debit_id)).status_code, 204)
        response = await self.mark(debit_id, "100")
        self.assertEqual((response.status_code, response.json()["expected_amount"]), (201, "100.00"))

    async def test_a_race_is_a_conflict(self):
        debit_id = await self.txn()
        async with AsyncSessionLocal() as db:
            db.add(RefundWatch(user_id=self.owner.id, transaction_id=debit_id, expected_amount=Decimal("1"),
                               due_date=DUE))
            await db.commit()
        real = refunds.AsyncSession.scalar

        async def pre_check_misses(db, statement, *args, **kwargs):
            if re.match(r"SELECT refund_watches\.id\s+FROM", str(statement)):
                return None  # as if the other tap hadn't committed yet: the unique constraint decides
            return await real(db, statement, *args, **kwargs)

        with patch.object(refunds.AsyncSession, "scalar", pre_check_misses):
            response = await self.mark(debit_id, "100")
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": "A refund is already expected for this payment."}))
        self.assertEqual(await self.watch_count(), 1)

    async def test_ownership(self):
        response = await self.mark(uuid.uuid4())
        self.assertEqual((response.status_code, response.json()), (404, {"detail": "Transaction not found."}))
        theirs = await self.txn(user_id=self.other.id)
        response = await self.mark(theirs)
        self.assertEqual((response.status_code, response.json()), (404, {"detail": "Transaction not found."}))
        unassigned = await self.sms_id(MYNTRA_DEBIT, NOW - 5 * DAY)  # an SMS row: user_id None, the owner's
        self.assertEqual((await self.mark(unassigned)).status_code, 201)
        mine = await self.txn(user_id=self.owner.id, when=NOW - 3 * DAY)
        self.assertEqual((await self.mark(mine)).status_code, 201)

    async def test_mark_and_cancel_move_updated_at(self):
        debit_id = await self.txn()
        before = await self.updated_at(debit_id)
        await self.mark(debit_id)
        marked = await self.updated_at(debit_id)
        self.assertGreater(marked, before)
        self.assertEqual((await self.cancel(debit_id)).status_code, 204)
        self.assertGreater(await self.updated_at(debit_id), marked)
        self.assertEqual((await self.listed())[str(debit_id)]["refund"], None)

    async def test_get_and_delete(self):
        debit_id = await self.txn()
        response = await self.get(debit_id)
        self.assertEqual((response.status_code, response.json()),
                         (404, {"detail": "No refund is expected for this payment."}))
        await self.mark(debit_id, "1200")
        response = await self.get(debit_id)
        self.assertEqual((response.status_code, response.json()), (200, {
            "transaction_id": str(debit_id), "expected_amount": "1200.00", "due_date": "2026-10-31",
            "credit_id": None, "state": "expected"}))
        response = await self.cancel(debit_id)
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertIsNone(await self.watch(debit_id))
        response = await self.cancel(debit_id)
        self.assertEqual((response.status_code, response.json()),
                         (404, {"detail": "No refund is expected for this payment."}))
        self.assertEqual((await self.get(debit_id)).status_code, 404)

    async def test_another_users_watch_is_not_found(self):
        debit_id = await self.txn(user_id=self.other.id)
        async with AsyncSessionLocal() as db:
            db.add(RefundWatch(user_id=self.other.id, transaction_id=debit_id, expected_amount=Decimal("1"),
                               due_date=DUE))
            await db.commit()
        self.assertEqual((await self.get(debit_id)).status_code, 404)
        self.assertEqual((await self.cancel(debit_id)).status_code, 404)
        self.assertIsNotNone(await self.watch(debit_id))

    async def test_writes_need_the_origin(self):
        debit_id = await self.txn()
        credit_id = await self.txn("CREDIT", when=NOW)
        await self.mark(debit_id)
        for response in (await self.mark(await self.txn(when=NOW - DAY), headers={}),
                         await self.cancel(debit_id, headers={}),
                         await self.accept(debit_id, credit_id, headers={}),
                         await self.reject(debit_id, credit_id, headers={})):
            self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request origin."}))
        self.assertEqual((await self.watch(debit_id)).credit_id, None)

    async def test_every_route_needs_a_session(self):
        debit_id, credit_id = uuid.uuid4(), uuid.uuid4()
        app.dependency_overrides.clear()
        for response in (await self.mark(debit_id), await self.get(debit_id), await self.cancel(debit_id),
                         await self.accept(debit_id, credit_id), await self.reject(debit_id, credit_id)):
            self.assertEqual(response.status_code, 401, response.text)

    def test_openapi(self):
        paths = app.openapi()["paths"]
        base = "/api/v1/refunds"
        expected = {base: ["post"], f"{base}/{{transaction_id}}": ["get", "delete"],
                    f"{base}/{{transaction_id}}/accept/{{credit_id}}": ["post"],
                    f"{base}/{{transaction_id}}/reject/{{credit_id}}": ["post"]}
        for path, methods in expected.items():
            self.assertEqual(sorted(paths[path]), sorted(methods), path)
            for method in methods:
                self.assertEqual(paths[path][method]["tags"], ["Refunds"])


class SuggestTests(RefundTestCase):
    async def test_cuj_1_the_refund_sms(self):
        debit_id = await self.watched()
        credit_id = await self.sms_id(MYNTRA_REFUND)
        self.assertEqual(await self.matches(), [(
            "Is this your Myntra refund?", "₹2,499 from Myntra Designs BANGALORE on 21 Oct", None,
            f"refund:{debit_id}:{credit_id}")])
        [notification] = await self.notifications(refunds.MATCH_KIND)
        self.assertEqual(notification.user_id, self.owner.id)

    async def test_exact_amount_from_anyone(self):
        debit_id = await self.watched()
        credit_id = await self.sms_id(FRIEND_CREDIT)
        self.assertEqual(await self.matches(), [(
            "Is this your Myntra refund?", "₹2,499 from Rahul on 21 Oct", None, f"refund:{debit_id}:{credit_id}")])

    async def test_a_smaller_amount_needs_a_shared_word(self):
        debit_id = await self.watched()
        await self.sms_id(friend_sms("1000.00", "629400100003"))
        self.assertEqual(await self.matches(), [])
        credit_id = await self.sms_id(refund_sms("1,000.00"), NOW + MINUTE)
        self.assertEqual(await self.keys(), [f"refund:{debit_id}:{credit_id}"])

    async def test_a_larger_amount_never(self):
        await self.watched(amount="2000")
        await self.sms_id(MYNTRA_REFUND)
        self.assertEqual(await self.matches(), [])

    async def test_common_words_are_not_a_match(self):
        debit_id = await self.watched_seed(merchant="HDFC Bank", expected="2000")
        await self.credit("1000", "HDFC Bank Ltd", raw="Rs 1000 credited to HDFC Bank A/c XX4821")
        self.assertEqual(await self.matches(), [])
        credit_id = await self.credit("2000", "HDFC Bank Ltd", NOW + MINUTE)
        self.assertEqual(await self.keys(), [f"refund:{debit_id}:{credit_id}"])
        [title] = [n.title for n in await self.notifications(refunds.MATCH_KIND)]
        self.assertEqual(title, "Is this your HDFC Bank refund?")

    async def test_the_90_day_window(self):
        inside = await self.watched_seed(when=NOW - 90 * DAY)
        credit_id = await self.credit()
        self.assertEqual(await self.keys(), [f"refund:{inside}:{credit_id}"])

    async def test_just_outside_the_window(self):
        await self.watched_seed(when=NOW - 90 * DAY - MINUTE)
        await self.credit()
        self.assertEqual(await self.matches(), [])

    async def test_a_credit_before_the_payment(self):
        await self.watched_seed(when=NOW)
        await self.credit(when=NOW - MINUTE)
        self.assertEqual(await self.matches(), [])

    async def test_a_debit_never_suggests(self):
        await self.watched()
        await self.sms_id(debit_sms(ref="629000100002"))
        self.assertEqual(await self.matches(), [])

    async def test_cancelled_and_refunded_watches_get_nothing(self):
        cancelled = await self.watched_seed()
        self.assertEqual((await self.cancel(cancelled)).status_code, 204)
        await self.credit()
        self.assertEqual(await self.matches(), [])
        refunded = await self.watched_seed(when=NOW - 2 * DAY)
        first = await self.txn("CREDIT", when=NOW)
        self.assertEqual((await self.accept(refunded, first)).status_code, 200)
        await self.credit(when=NOW + MINUTE)
        self.assertEqual(await self.matches(), [])

    async def test_another_currency(self):
        await self.watched_seed(currency="USD")
        await self.credit()
        self.assertEqual(await self.matches(), [])

    async def test_card_payments_and_self_transfers_are_never_refunds(self):
        await self.watched_seed()
        await self.credit(category="Credit Card Payment")
        await self.credit(category="Self Transfer", when=NOW + MINUTE)
        self.assertEqual(await self.matches(), [])
        await self.credit(category="Refunds & Cashback", when=NOW + 2 * MINUTE)
        self.assertEqual(len(await self.matches()), 1)

    async def test_an_unread_or_zero_credit(self):
        await self.watched_seed()
        await self.credit(is_parsed=False)
        await self.credit("0", when=NOW + MINUTE)
        self.assertEqual(await self.matches(), [])

    async def test_a_hand_added_credit(self):
        debit_id = await self.watched()
        added = await self.manual(2499, "Myntra", type="CREDIT")
        self.assertEqual(await self.matches(), [(
            "Is this your Myntra refund?", "₹2,499 from Myntra on 21 Oct", None, f"refund:{debit_id}:{added['id']}")])

    async def test_a_credit_without_a_merchant_matches_on_its_text(self):
        debit_id = await self.watched()
        credit_id = await self.credit("1000", None, raw="Refund from MYNTRA of Rs 1000.00 credited to A/c XX4821")
        self.assertEqual(await self.matches(), [(
            "Is this your Myntra refund?", "₹1,000 on 21 Oct", None, f"refund:{debit_id}:{credit_id}")])

    async def test_a_payment_without_a_merchant(self):
        debit_id = await self.watched_seed(merchant=None)
        credit_id = await self.credit(merchant="Unknown")
        self.assertEqual(await self.matches(), [(
            "Is this a refund?", "₹2,499 on 21 Oct", None, f"refund:{debit_id}:{credit_id}")])

    async def test_the_nearest_exact_payment_wins(self):
        await self.watched()
        ajio = await self.watched(debit_sms(vpa="ajio@icici", ref="629000100009"), NOW - 2 * DAY)
        credit_id = await self.sms_id(MYNTRA_REFUND)
        self.assertEqual(await self.keys(), [f"refund:{ajio}:{credit_id}"])

    async def test_an_exact_amount_beats_a_shared_word(self):
        exact = await self.watched_seed(when=NOW - 30 * DAY)
        await self.watched_seed("3000", when=NOW - DAY)
        credit_id = await self.sms_id(MYNTRA_REFUND)
        self.assertEqual(await self.keys(), [f"refund:{exact}:{credit_id}"])

    async def test_equal_payments_go_to_the_older_watch(self):
        first = await self.watched_seed()
        await self.watched_seed()  # same time, marked later
        credit_id = await self.credit()
        self.assertEqual(await self.keys(), [f"refund:{first}:{credit_id}"])

    async def test_once_per_pair(self):
        await self.watched()
        credit_id = await self.sms_id(MYNTRA_REFUND)
        await post_save.run(credit_id, "restored")
        await post_save.run(credit_id, "manual")
        self.assertEqual(len(await self.matches()), 1)

    async def test_a_held_out_credit_waits_for_your_decision(self):
        debit_id = await self.watched_seed()
        original = await self.txn("CREDIT", "2499", "Myntra Designs", NOW - 100 * DAY)
        held = await self.txn("CREDIT", "2499", "Myntra Designs", NOW)
        await self.flag(held, original)
        await post_save.run(held, "sms")
        self.assertEqual(await self.matches(), [])
        response = await self.client.post(f"/api/v1/transactions/{held}/not-duplicate", headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.keys(), [f"refund:{debit_id}:{held}"])

    async def test_a_credit_already_confirmed_elsewhere(self):
        older = await self.watched_seed(when=NOW - 10 * DAY)
        nearer = await self.watched_seed(when=NOW - 2 * DAY)
        credit_id = await self.credit()
        self.assertEqual(await self.keys(), [f"refund:{nearer}:{credit_id}"])
        self.assertEqual((await self.accept(nearer, credit_id)).status_code, 200)
        await post_save.run(credit_id, "restored")
        self.assertEqual(await self.keys(), [f"refund:{nearer}:{credit_id}"])
        self.assertEqual((await self.watch(older)).credit_id, None)

    async def test_registered_for_sms_manual_restored(self):
        [(check, causes)] = [(check, causes) for name, check, causes in post_save._CHECKS if name == "refunds"]
        self.assertIs(check, refunds.suggest)
        self.assertEqual(causes, {"sms", "manual", "restored"})
        await self.watched_seed()
        await self.credit(cause="edit")
        await self.credit(cause="ai_retry", when=NOW + MINUTE)
        self.assertEqual(await self.matches(), [])

    async def statements(self, txn_id) -> int:
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, txn_id, "sms")
            self.assertIsNotNone(ctx)
            return await self.count_statements(lambda: refunds.suggest(ctx))

    async def test_query_budget(self):
        self.assertEqual(await self.statements(await self.txn()), 0)
        self.assertEqual(await self.statements(await self.txn("CREDIT", when=NOW)), 1)
        await self.watched_seed(when=NOW - 200 * DAY)  # out of the window
        self.assertEqual(await self.statements(await self.txn("CREDIT", when=NOW + MINUTE)), 1)


class AcceptRejectTests(RefundTestCase):
    async def test_accept(self):
        debit_id = await self.watched_seed()
        credit_id = await self.txn("CREDIT", when=NOW)
        debit_before, credit_before = await self.updated_at(debit_id), await self.updated_at(credit_id)
        response = await self.accept(debit_id, credit_id)
        self.assertEqual((response.status_code, response.json()), (200, {
            "transaction_id": str(debit_id), "expected_amount": "2499.00", "due_date": "2026-10-31",
            "credit_id": str(credit_id), "state": "refunded"}))
        watch = await self.watch(debit_id)
        self.assertEqual(watch.credit_id, credit_id)
        self.assertIsNotNone(watch.closed_at)
        self.assertGreater(await self.updated_at(debit_id), debit_before)
        self.assertGreater(await self.updated_at(credit_id), credit_before)
        rows = await self.listed()
        self.assertEqual((rows[str(debit_id)]["refund"], rows[str(debit_id)]["refund_for"]), ("refunded", None))
        self.assertEqual((rows[str(credit_id)]["refund"], rows[str(credit_id)]["refund_for"]), (None, str(debit_id)))
        response = await self.get(debit_id)
        self.assertEqual(response.json()["state"], "refunded")

    async def test_conflicts(self):
        debit_id = await self.watched_seed()
        other_debit = await self.watched_seed(when=NOW - 2 * DAY)
        credit_id = await self.txn("CREDIT", when=NOW)
        await self.accept(debit_id, credit_id)
        response = await self.accept(debit_id, credit_id)
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": "This refund is already confirmed."}))
        response = await self.accept(other_debit, credit_id)
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": "That credit is already matched to another refund."}))
        self.assertEqual((await self.watch(other_debit)).credit_id, None)

    async def test_the_unique_credit_decides_a_race(self):
        debit_id = await self.watched_seed()
        other_debit = await self.watched_seed(when=NOW - 2 * DAY)
        credit_id = await self.txn("CREDIT", when=NOW)
        await self.accept(debit_id, credit_id)
        real = refunds.AsyncSession.scalar

        async def pre_check_misses(db, statement, *args, **kwargs):
            if re.match(r"SELECT refund_watches\.id\s+FROM", str(statement)):
                return None  # as if the other Yes hadn't committed yet
            return await real(db, statement, *args, **kwargs)

        with patch.object(refunds.AsyncSession, "scalar", pre_check_misses):
            response = await self.accept(other_debit, credit_id)
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": "That credit is already matched to another refund."}))
        self.assertEqual((await self.watch(other_debit)).credit_id, None)

    async def interleaved_accept(self, debit_id, credit_id, meanwhile):
        """accept(debit_id, credit_id), with `meanwhile()` run to completion after its checks, before its write."""
        real = refunds.AsyncSession.scalar
        fired = False

        async def other_tap_lands(db, statement, *args, **kwargs):
            nonlocal fired
            if not fired and re.match(r"SELECT refund_watches\.id\s+FROM refund_watches\s+WHERE refund_watches\."
                                      r"credit_id =", str(statement)):
                fired = True
                await meanwhile()
            return await real(db, statement, *args, **kwargs)

        with patch.object(refunds.AsyncSession, "scalar", other_tap_lands):
            response = await self.accept(debit_id, credit_id)
        self.assertTrue(fired)
        return response

    async def test_two_simultaneous_yes_taps(self):
        debit_id = await self.watched_seed()
        first, second = await self.txn("CREDIT", when=NOW), await self.txn("CREDIT", when=NOW - DAY)
        second_before = await self.updated_at(second)
        responses = []

        async def first_yes():
            responses.append(await self.accept(debit_id, first))

        response = await self.interleaved_accept(debit_id, second, first_yes)
        self.assertEqual((responses[0].status_code, responses[0].json()["credit_id"]), (200, str(first)))
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": "This refund is already confirmed."}))
        self.assertEqual((await self.watch(debit_id)).credit_id, first)
        self.assertEqual(await self.updated_at(second), second_before)
        rows = await self.listed()
        self.assertEqual((rows[str(first)]["refund_for"], rows[str(second)]["refund_for"]), (str(debit_id), None))

    async def test_a_cancel_during_yes(self):
        debit_id = await self.watched_seed()
        credit_id = await self.txn("CREDIT", when=NOW)
        credit_before = await self.updated_at(credit_id)
        responses = []

        async def cancel():
            responses.append(await self.cancel(debit_id))

        response = await self.interleaved_accept(debit_id, credit_id, cancel)
        self.assertEqual(responses[0].status_code, 204)
        self.assertEqual((response.status_code, response.json()),
                         (404, {"detail": "No refund is expected for this payment."}))
        self.assertIsNone(await self.watch(debit_id))
        self.assertEqual(await self.updated_at(credit_id), credit_before)

    async def test_credit_not_found(self):
        debit_id = await self.watched_seed()
        theirs = await self.txn("CREDIT", when=NOW, user_id=self.other.id)
        unread = await self.txn("CREDIT", when=NOW, is_parsed=False)
        another_debit = await self.txn(when=NOW)
        for credit_id in (uuid.uuid4(), another_debit, theirs, unread):
            response = await self.accept(debit_id, credit_id)
            self.assertEqual((response.status_code, response.json()), (404, {"detail": "Credit not found."}))
        response = await self.accept(another_debit, await self.txn("CREDIT", when=NOW))
        self.assertEqual((response.status_code, response.json()),
                         (404, {"detail": "No refund is expected for this payment."}))

    async def test_cuj_2_not_this_one(self):
        debit_id = await self.watched()
        friend = await self.sms_id(FRIEND_CREDIT)
        self.assertEqual(await self.keys(), [f"refund:{debit_id}:{friend}"])
        response = await self.reject(debit_id, friend)
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertEqual(await self.matches(), [])
        self.assertEqual((await self.watch(debit_id)).credit_id, None)
        self.assertEqual((await self.listed())[str(debit_id)]["refund"], "expected")
        credit_id = await self.sms_id(MYNTRA_REFUND, NOW + 20 * MINUTE)
        self.assertEqual(await self.keys(), [f"refund:{debit_id}:{credit_id}"])
        self.assertEqual((await self.accept(debit_id, credit_id)).status_code, 200)
        self.assertEqual((await self.reject(debit_id, friend)).status_code, 204)  # again: still 204
        self.assertEqual((await self.reject(uuid.uuid4(), uuid.uuid4())).status_code, 204)

    async def test_reject_removes_only_its_own_suggestion(self):
        first, second = await self.watched_seed(), await self.watched_seed(when=NOW - 2 * DAY)
        credit_a, credit_b = uuid.uuid4(), uuid.uuid4()
        async with AsyncSessionLocal() as db:
            for debit_id, credit_id in ((first, credit_a), (second, credit_b), (first, credit_b)):
                await create_once(db, self.owner.id, kind=refunds.MATCH_KIND, title="t", body="b", link=None,
                                  dedupe_key=f"refund:{debit_id}:{credit_id}")
            await create_once(db, self.other.id, kind=refunds.MATCH_KIND, title="t", body="b", link=None,
                              dedupe_key=f"refund:{first}:{credit_a}")
            mine = await db.scalar(select(Notification.id).where(
                Notification.user_id == self.owner.id, Notification.dedupe_key == f"refund:{first}:{credit_a}"))
            db.add(NotificationDismissal(notification_id=mine))
            await db.commit()
        self.assertEqual((await self.reject(first, credit_a)).status_code, 204)
        self.assertEqual(sorted((n.user_id == self.owner.id, n.dedupe_key)
                                for n in await self.notifications(refunds.MATCH_KIND)), sorted([
            (True, f"refund:{second}:{credit_b}"), (True, f"refund:{first}:{credit_b}"),
            (False, f"refund:{first}:{credit_a}")]))
        async with AsyncSessionLocal() as db:
            self.assertEqual(list(await db.scalars(select(NotificationDismissal.notification_id))), [])

    async def test_cancelling_a_refunded_watch(self):
        debit_id = await self.watched_seed()
        credit_id = await self.txn("CREDIT", when=NOW)
        await self.accept(debit_id, credit_id)
        debit_before, credit_before = await self.updated_at(debit_id), await self.updated_at(credit_id)
        self.assertEqual((await self.cancel(debit_id)).status_code, 204)
        self.assertGreater(await self.updated_at(debit_id), debit_before)
        self.assertGreater(await self.updated_at(credit_id), credit_before)
        rows = await self.listed()
        self.assertEqual((rows[str(debit_id)]["refund"], rows[str(credit_id)]["refund_for"]), (None, None))
        self.assertEqual((await self.mark(debit_id)).status_code, 201)


class ResolverTests(RefundTestCase):
    def test_registered(self):
        self.assertIs(notifications._RESOLVERS["refund_match"], refunds.refund_actions)
        self.assertNotIn("refund_overdue", notifications._RESOLVERS)

    def buttons(self, debit_id, credit_id) -> list[dict]:
        return [{"label": "Yes", "path": f"/api/v1/refunds/{debit_id}/accept/{credit_id}"},
                {"label": "No", "path": f"/api/v1/refunds/{debit_id}/reject/{credit_id}"}]

    async def test_yes_and_no_while_open(self):
        debit_id = await self.watched()
        credit_id = await self.sms_id(MYNTRA_REFUND)
        [item] = await self.inbox()
        self.assertEqual((item["actions"], item["resolved"], item["link"]), (self.buttons(debit_id, credit_id), None,
                                                                             None))
        self.assertTrue(all(action["path"].startswith("/api/v1/") for action in item["actions"]))
        response = await self.client.post(item["actions"][0]["path"], headers=ORIGIN)
        self.assertEqual((response.status_code, response.json()["state"]), (200, "refunded"))

    async def test_decided(self):
        debit_id = await self.watched()
        friend = await self.sms_id(FRIEND_CREDIT)
        credit_id = await self.sms_id(MYNTRA_REFUND, NOW + 20 * MINUTE)
        self.assertEqual(await self.keys(), [f"refund:{debit_id}:{friend}", f"refund:{debit_id}:{credit_id}"])
        await self.accept(debit_id, credit_id)
        resolved = {item["body"]: (item["actions"], item["resolved"]) for item in await self.inbox()}
        self.assertEqual(resolved, {
            "₹2,499 from Rahul on 21 Oct": ([], "No longer applies"),
            "₹2,499 from Myntra Designs BANGALORE on 21 Oct": ([], "Marked as refunded"),
        })

    async def test_cancelled_watch(self):
        debit_id = await self.watched()
        await self.sms_id(MYNTRA_REFUND)
        await self.cancel(debit_id)
        [item] = await self.inbox()
        self.assertEqual((item["actions"], item["resolved"]), ([], "No longer applies"))

    async def test_deleted_credit(self):
        await self.watched()
        credit_id = await self.sms_id(MYNTRA_REFUND)
        await self.delete_txn(credit_id)
        [item] = await self.inbox()
        self.assertEqual((item["actions"], item["resolved"]), ([], "No longer applies"))

    async def test_a_malformed_key_gets_neither(self):
        debit_id = await self.watched()
        async with AsyncSessionLocal() as db:
            for key in ("refund:x", f"refund:{debit_id}:nope", f"refunds:{debit_id}:{debit_id}"):
                await create_once(db, self.owner.id, kind=refunds.MATCH_KIND, title=key, body="b", link=None,
                                  dedupe_key=key)
        items = await self.inbox()
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual((item["actions"], item["resolved"]), ([], None), item["title"])

    async def test_query_budget(self):
        first = await self.watched()
        await self.sms_id(MYNTRA_REFUND)
        second = await self.watched_seed(when=NOW - 2 * DAY)
        credit_id = await self.txn("CREDIT", when=NOW + MINUTE)
        await self.accept(second, credit_id)
        async with AsyncSessionLocal() as db:
            await create_once(db, self.owner.id, kind=refunds.MATCH_KIND, title="t", body="b", link=None,
                              dedupe_key=f"refund:{second}:{credit_id}")
        async with AsyncSessionLocal() as db:
            rows = list(await db.scalars(select(Notification)))
            self.assertEqual(len(rows), 2)
            found = {}

            async def resolve():
                found.update(await refunds.refund_actions(db, rows))

            self.assertEqual(await self.count_statements(resolve), 2)
            self.assertEqual(sorted(r.resolved or "buttons" for r in found.values()),
                             ["Marked as refunded", "buttons"])
            await self.cancel(first)
            self.assertEqual(await self.count_statements(resolve), 1)  # nothing open: no credit lookup
            self.assertEqual(await self.count_statements(lambda: refunds.refund_actions(db, [])), 0)


class OverdueTests(RefundTestCase):
    async def run_daily(self, today=DUE + DAY, owner=None) -> None:
        async with AsyncSessionLocal() as db:
            await refunds.daily(db, owner or self.owner, today)

    async def overdue(self) -> list[tuple]:
        return [(n.title, n.body, n.link, n.dedupe_key) for n in await self.notifications(refunds.OVERDUE_KIND)]

    async def test_a_day_late(self):
        debit_id = await self.watched()
        watch = await self.watch(debit_id)
        await self.run_daily(DUE)
        self.assertEqual(await self.overdue(), [])
        await self.run_daily()
        expected = [("Refund from Myntra still pending", "₹2,499 expected by 31 Oct.", f"/activity?txn={debit_id}",
                     f"refund-overdue:{watch.id}")]
        self.assertEqual(await self.overdue(), expected)
        await self.run_daily()
        await self.run_daily(DUE + 5 * DAY)
        self.assertEqual(await self.overdue(), expected)
        [notification] = await self.notifications(refunds.OVERDUE_KIND)
        self.assertEqual(notification.user_id, self.owner.id)
        [item] = await self.inbox(refunds.OVERDUE_KIND)
        self.assertEqual((item["actions"], item["resolved"]), ([], None))

    async def test_refunded_or_cancelled_watches_are_not_overdue(self):
        refunded = await self.watched_seed()
        await self.accept(refunded, await self.txn("CREDIT", when=NOW))
        cancelled = await self.watched_seed(when=NOW - 2 * DAY)
        await self.cancel(cancelled)
        await self.run_daily()
        self.assertEqual(await self.overdue(), [])

    async def test_another_users_watch(self):
        debit_id = await self.txn(user_id=self.other.id)
        async with AsyncSessionLocal() as db:
            db.add(RefundWatch(user_id=self.other.id, transaction_id=debit_id, expected_amount=Decimal("10"),
                               due_date=DUE))
            await db.commit()
        await self.run_daily()
        self.assertEqual(await self.overdue(), [])

    async def test_partial_amount_and_no_merchant(self):
        partial = await self.watched_seed(expected="1000.50")
        nameless = await self.watched_seed("700", merchant=None, when=NOW - 2 * DAY)
        await self.run_daily()
        self.assertEqual(sorted(await self.overdue()), sorted([
            ("Refund from Myntra still pending", "₹1,000.50 expected by 31 Oct.", f"/activity?txn={partial}",
             f"refund-overdue:{(await self.watch(partial)).id}"),
            ("Refund still pending", "₹700 expected by 31 Oct.", f"/activity?txn={nameless}",
             f"refund-overdue:{(await self.watch(nameless)).id}"),
        ]))

    async def test_through_the_daily_trigger(self):
        debit_id = await self.watched()
        headers = {"X-Jobs-Secret": JOBS_SECRET}
        evening = datetime.combine(DUE + DAY, time(21, 30), tzinfo=IST)
        with patch.dict(os.environ, {"JOBS_SECRET": JOBS_SECRET}), patch.object(jobs, "now", return_value=evening):
            response = await self.client.post("/api/v1/jobs/daily", params={"force": "true"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("refund_overdue", response.json()["ran"])
        self.assertEqual([link for _, _, link, _ in await self.overdue()], [f"/activity?txn={debit_id}"])

    async def test_a_failure_is_logged_not_raised(self):
        await self.watched()
        with patch.object(refunds, "create_once", AsyncMock(side_effect=RuntimeError("bug"))), \
                self.assertLogs("refunds", "ERROR") as logs:
            await self.run_daily()
        self.assertIn("Refund reminder failed", logs.output[0])
        self.assertEqual(await self.overdue(), [])


class DeleteTests(RefundTestCase):
    async def test_deleting_a_watched_payment(self):
        debit_id = await self.watched()
        await self.delete_txn(debit_id)
        self.assertIsNone(await self.watch(debit_id))
        self.assertEqual(await self.watch_count(), 0)

    async def test_deleting_a_refunded_payment(self):
        debit_id = await self.watched_seed()
        credit_id = await self.txn("CREDIT", when=NOW)
        await self.accept(debit_id, credit_id)
        before = await self.updated_at(credit_id)
        await self.delete_txn(debit_id)
        self.assertEqual(await self.watch_count(), 0)
        self.assertGreater(await self.updated_at(credit_id), before)
        self.assertIsNone((await self.listed())[str(credit_id)]["refund_for"])

    async def test_deleting_a_confirmed_refund_reopens_the_watch(self):
        debit_id = await self.watched()
        credit_id = await self.sms_id(MYNTRA_REFUND)
        await self.accept(debit_id, credit_id)
        before = await self.updated_at(debit_id)
        await self.delete_txn(credit_id)
        watch = await self.watch(debit_id)
        self.assertEqual((watch.credit_id, watch.closed_at), (None, None))
        self.assertGreater(await self.updated_at(debit_id), before)
        self.assertEqual((await self.listed())[str(debit_id)]["refund"], "expected")
        friend = await self.sms_id(FRIEND_CREDIT, NOW + 20 * MINUTE)
        self.assertIn(f"refund:{debit_id}:{friend}", await self.keys())

    async def test_an_unrelated_delete(self):
        debit_id = await self.watched_seed()
        credit_id = await self.txn("CREDIT", when=NOW)
        await self.accept(debit_id, credit_id)
        open_id = await self.watched_seed(when=NOW - 2 * DAY)
        before = await self.updated_at(open_id)
        await self.delete_txn(await self.txn(when=NOW - DAY))
        await self.delete_txn(await self.txn("CREDIT", when=NOW + MINUTE))
        self.assertEqual((await self.watch(debit_id)).credit_id, credit_id)
        self.assertIsNone((await self.watch(open_id)).credit_id)
        self.assertEqual(await self.updated_at(open_id), before)


if __name__ == "__main__":
    unittest.main()
