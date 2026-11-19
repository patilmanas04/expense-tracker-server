"""Self-transfer suggestions (transfers.py, count_real_spending.md Phase 3): which SMS pair, the suggestion and its
notification, Confirm / Not a self transfer, the notification buttons, and cleanup when a leg is deleted. Every
pairing case posts raw SMS through the webhook, so the real parser fills amount, type and account
(gated_steps_decision.md, step 20). Isolated in-memory SQLite; email stays off (no Gmail keys); Gemini stays off."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""  # never send real mail from tests

import itertools
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, func, select, update

import notifications
import post_save
import transfers
from auth import current_user
from balance import affects_bank_balance
from database import AsyncSessionLocal
from main import app
from models import (DuplicateFlag, MerchantCategory, Notification, NotificationDismissal, Transaction,
                    TransferPair)
from notifications import create_once
from parser import BANK_ACCOUNT_RE, card_text
from test_post_save import HOOK, NOW, ORIGIN, PostSaveTestCase

MINUTE = timedelta(minutes=1)
T0 = datetime(2026, 10, 21, 4, 32, tzinfo=timezone.utc)  # 10:02 IST

# CUJ 2 (count_real_spending.md), "10000.00" on both legs. Different UPI refs on purpose: matching on a shared
# reference (Alternative F) stays out.
SYNTHETIC_PAIR_DEBIT = ("ICICI Bank Acct XX310 debited for Rs 10000.00 on 21-Oct-26; MANAS P credited. "
                        "UPI:629412340001. Call 18002662 for dispute. SMS BLOCK 310 to 9215676766.")
SYNTHETIC_PAIR_CREDIT = ("Rs.10000.00 credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA manas.p@okicici "
                         "(UPI 629412340002)")
# A debit-card alert: affects_bank_balance is True, but it names no bank account (stricter rule 1).
SYNTHETIC_CARD_ALERT = "Spent Rs.10000.00 From HDFC Bank Card x5566 At AMAZON On 2026-10-21:10:02:11 Bal Rs.5000.00"
# An ATM withdrawal: Cash Withdrawal (stricter rule 2).
SYNTHETIC_ATM = ("ICICI Bank Acct XX310 debited with INR 10,000.00 on 21-Oct-26 at ATM WDL MG ROAD. "
                 "Avl Bal INR 3,000.00.")
SYNTHETIC_CARD_BILL = ("Rs.10000.00 debited from HDFC Bank A/c XX4821 on 21-10-26 towards HDFC Bank Credit Card "
                       "XX9012. Ref 629400000555")
SYNTHETIC_CARD_PAYMENT_RECEIVED = ("Payment of Rs 10,000.00 has been received on your ICICI Bank Credit Card "
                                   "Account 4xxx7001 on 21-Oct-26. Thank you.")
SYNTHETIC_SIP = ("Rs.10000.00 debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA zerodha@hdfcbank "
                 "(UPI Ref No 629400000777). Not you? Call 18002586161")
SYNTHETIC_NO_ACCOUNT = ("Rs.10000.00 credited to HDFC Bank A/c on 21-10-26 from VPA manas.p@okicici "
                        "(UPI 629412340003)")
SYNTHETIC_UNREAD = ("Your HDFC Bank A/c XX4821 has been credited on 21-10-26 by VPA manas.p@okicici "
                    "(UPI 629412340004)")
SYNTHETIC_USD_DEBIT = ("ICICI Bank Acct XX310 debited for USD 10000.00 on 21-Oct-26; MANAS P credited. "
                       "UPI:629412340005. Call 18002662 for dispute. SMS BLOCK 310 to 9215676766.")
DMART = ("Rs.1200.00 debited from HDFC Bank A/c XX4821 on 21-10-26 to VPA dmart@ybl (UPI Ref No 629400100777). "
         "Not you? Call 18002586161")

# Masked real SMS: (debit, credit) of one transfer between your own HDFC and ICICI accounts. Empty until the owner
# adds one from Activity (gated_steps_decision.md, "How the owner closes the gate"): copy both messages, mask the
# account numbers, names and references, and add the pair here. If it fails, fix the parser, not the pairing rules.
REAL_PAIRS: list[tuple[str, str]] = []

_refs = itertools.count(1)


def _ref() -> str:
    return f"6294{next(_refs):08d}"


def pair_debit(amount="10000.00", ref=None, acct="XX310", name="MANAS P") -> str:
    """The CUJ 2 ICICI debit wording."""
    return (f"ICICI Bank Acct {acct} debited for Rs {amount} on 21-Oct-26; {name} credited. "
            f"UPI:{ref or _ref()}. Call 18002662 for dispute. SMS BLOCK 310 to 9215676766.")


def pair_credit(amount="10000.00", ref=None, acct="XX4821", vpa="manas.p@okicici") -> str:
    """The CUJ 2 HDFC credit wording."""
    return f"Rs.{amount} credited to HDFC Bank A/c {acct} on 21-10-26 from VPA {vpa} (UPI {ref or _ref()})"


def hdfc_debit(amount="10000.00", ref=None, acct="XX4821", vpa="manas.p@okhdfc") -> str:
    """The HDFC debit wording (tests/test_refunds.py debit_sms)."""
    return (f"Rs.{amount} debited from HDFC Bank A/c {acct} on 21-10-26 to VPA {vpa} (UPI Ref No {ref or _ref()}). "
            "Not you? Call 18002586161")


def icici_credit(amount="10000.00", ref=None, acct="XX310", vpa="manas.p@okhdfc") -> str:
    return f"Rs.{amount} credited to ICICI Bank A/c {acct} on 21-10-26 from VPA {vpa} (UPI {ref or _ref()})"


def card_alert(amount="10000.00") -> str:
    return f"Spent Rs.{amount} From HDFC Bank Card x5566 At AMAZON On 2026-10-21:10:02:11 Bal Rs.5000.00"


class TransferTestCase(PostSaveTestCase):
    async def post(self, message: str, when: datetime) -> dict:
        """A raw SMS through the webhook (the after-save checks run); it must not be held out as a duplicate."""
        txn = await self.sms(message, when)
        self.assertIsNone(txn.get("duplicate_of"), txn)
        return txn

    async def post_id(self, message: str, when: datetime) -> uuid.UUID:
        return uuid.UUID((await self.post(message, when))["id"])

    async def quiet(self, message: str, when: datetime) -> uuid.UUID:
        """Saved without the after-save checks."""
        with patch("post_save.run", new=AsyncMock()):
            return await self.post_id(message, when)

    async def row(self, txn_id) -> Transaction:
        async with AsyncSessionLocal() as db:
            return await db.get(Transaction, txn_id)

    async def pairs(self) -> list[TransferPair]:
        async with AsyncSessionLocal() as db:
            return list(await db.scalars(select(TransferPair).order_by(TransferPair.created_at)))

    async def pair_of(self, debit_id, credit_id) -> Optional[TransferPair]:
        async with AsyncSessionLocal() as db:
            return await db.scalar(select(TransferPair).where(TransferPair.debit_id == debit_id,
                                                              TransferPair.credit_id == credit_id))

    async def get_pair(self, pair_id) -> Optional[TransferPair]:
        async with AsyncSessionLocal() as db:
            return await db.get(TransferPair, pair_id)

    async def suggestions(self) -> list[Notification]:
        return await self.notifications(transfers.KIND)

    async def assert_none(self):
        self.assertEqual(await self.pairs(), [])
        self.assertEqual(await self.suggestions(), [])

    async def cuj2(self, amount="10000.00", debit_at=T0, credit_at=T0 + 2 * MINUTE, name="MANAS P",
                   vpa="manas.p@okicici", credit_acct="XX4821") -> tuple[uuid.UUID, uuid.UUID, TransferPair]:
        """The ICICI XX310 debit, then the HDFC credit: one pending pair."""
        debit_id = await self.post_id(pair_debit(amount, name=name), debit_at)
        credit_id = await self.post_id(pair_credit(amount, acct=credit_acct, vpa=vpa), credit_at)
        pair = await self.pair_of(debit_id, credit_id)
        self.assertIsNotNone(pair)
        self.assertEqual(pair.status, transfers.PENDING)
        return debit_id, credit_id, pair

    async def confirm(self, pair_id, headers=ORIGIN):
        return await self.client.post(f"/api/v1/transfers/pairs/{pair_id}/confirm", headers=headers)

    async def reject(self, pair_id, headers=ORIGIN):
        return await self.client.post(f"/api/v1/transfers/pairs/{pair_id}/reject", headers=headers)

    async def patch_category(self, txn_id, category):
        response = await self.client.patch(f"/api/v1/transactions/{txn_id}", json={"category": category},
                                           headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["category"], category)

    async def delete_txn(self, txn_id):
        response = await self.client.delete(f"/api/v1/transactions/{txn_id}", headers=ORIGIN)
        self.assertEqual(response.status_code, 204, response.text)

    async def inbox(self, kind=transfers.KIND) -> list[dict]:
        response = await self.client.get("/api/v1/notifications", params={"limit": 100})
        self.assertEqual(response.status_code, 200, response.text)
        return [item for item in response.json()["items"] if item["kind"] == kind]

    async def item_for(self, pair_id) -> dict:
        [item] = [item for item in await self.inbox() if item["id"] == str(await self._notification_id(pair_id))]
        return item

    async def _notification_id(self, pair_id) -> uuid.UUID:
        async with AsyncSessionLocal() as db:
            return await db.scalar(select(Notification.id).where(Notification.dedupe_key == f"pair:{pair_id}"))

    async def leg(self, txn_id) -> transfers.Leg:
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, txn_id, "sms")
            self.assertIsNotNone(ctx)
            return transfers._leg(ctx.txn)

    async def reset(self):
        async with AsyncSessionLocal() as db:
            for model in (TransferPair, NotificationDismissal, Notification, DuplicateFlag, Transaction):
                await db.execute(delete(model))
            await db.commit()


class PairingTests(TransferTestCase):
    async def test_cuj2_suggests_one_pair(self):
        debit_id = await self.post_id(SYNTHETIC_PAIR_DEBIT, T0)
        credit_id = await self.post_id(SYNTHETIC_PAIR_CREDIT, T0 + 2 * MINUTE)
        [pair] = await self.pairs()
        self.assertEqual((pair.debit_id, pair.credit_id, pair.user_id, pair.status, pair.decided_at),
                         (debit_id, credit_id, self.owner.id, "pending", None))
        self.assertIsNotNone(pair.created_at)
        debit, credit = await self.row(debit_id), await self.row(credit_id)
        self.assertEqual((debit.type, debit.amount, debit.account_ref, debit.user_id),
                         ("DEBIT", 10000, "ICICI XX310", None))  # unassigned SMS rows pair
        self.assertEqual((credit.type, credit.amount, credit.account_ref, credit.user_id),
                         ("CREDIT", 10000, "HDFC XX4821", None))
        [notification] = await self.suggestions()
        self.assertEqual((notification.user_id, notification.title, notification.body, notification.link,
                          notification.dedupe_key), (
            self.owner.id, "Looks like a transfer between your accounts",
            "₹10,000.00 from ICICI XX310 at 10:02 to HDFC XX4821 at 10:04", None, f"pair:{pair.id}"))
        self.assertEqual((debit.category, credit.category), ("Uncategorized", "Uncategorized"))  # unchanged

    async def test_credit_first_then_debit(self):
        credit_id = await self.post_id(SYNTHETIC_PAIR_CREDIT, T0)
        debit_id = await self.post_id(SYNTHETIC_PAIR_DEBIT, T0 + 2 * MINUTE)
        [pair] = await self.pairs()
        self.assertEqual((pair.debit_id, pair.credit_id), (debit_id, credit_id))
        [notification] = await self.suggestions()
        self.assertEqual(notification.body, "₹10,000.00 from ICICI XX310 at 10:04 to HDFC XX4821 at 10:02")

    async def test_window_is_30_minutes_inclusive(self):
        debit_id = await self.post_id(pair_debit("10000.00"), T0)
        credit_id = await self.post_id(pair_credit("10000.00"), T0 + 30 * MINUTE)
        self.assertIsNotNone(await self.pair_of(debit_id, credit_id))
        late_debit = await self.post_id(pair_debit("10001.00"), T0)
        late_credit = await self.post_id(pair_credit("10001.00"), T0 + 31 * MINUTE)
        self.assertIsNone(await self.pair_of(late_debit, late_credit))
        early_credit = await self.post_id(pair_credit("10002.00"), T0)
        early_debit = await self.post_id(pair_debit("10002.00"), T0 + 30 * MINUTE)  # credit 30 min before
        self.assertIsNotNone(await self.pair_of(early_debit, early_credit))
        self.assertEqual(len(await self.pairs()), 2)
        self.assertEqual(len(await self.suggestions()), 2)

    async def test_same_account_never_pairs(self):
        debit = await self.post(SYNTHETIC_PAIR_DEBIT, T0)
        credit = await self.post(icici_credit(), T0 + 2 * MINUTE)
        self.assertEqual((debit["account_ref"], credit["account_ref"]), ("ICICI XX310", "ICICI XX310"))
        await self.assert_none()

    async def test_two_accounts_at_one_bank_pair(self):
        debit = await self.post(hdfc_debit(acct="XX4821"), T0)
        credit = await self.post(pair_credit(acct="XX4822"), T0 + 2 * MINUTE)
        self.assertEqual((debit["account_ref"], credit["account_ref"]), ("HDFC XX4821", "HDFC XX4822"))
        [pair] = await self.pairs()
        self.assertEqual((str(pair.debit_id), str(pair.credit_id)), (debit["id"], credit["id"]))
        [notification] = await self.suggestions()
        self.assertEqual(notification.body, "₹10,000.00 from HDFC XX4821 at 10:02 to HDFC XX4822 at 10:04")

    async def test_different_amounts(self):
        debit = await self.post(pair_debit("10000.00"), T0)
        credit = await self.post(pair_credit("10000.50"), T0 + 2 * MINUTE)
        self.assertNotEqual(debit["amount"], credit["amount"])
        await self.assert_none()

    async def test_other_currency(self):
        debit = await self.post(SYNTHETIC_USD_DEBIT, T0)
        self.assertEqual(debit["currency"], "USD")
        await self.post(SYNTHETIC_PAIR_CREDIT, T0 + 2 * MINUTE)
        await self.assert_none()

    async def test_unread_sms(self):
        unread = await self.post(SYNTHETIC_UNREAD, T0)
        self.assertFalse(unread["is_parsed"])
        await self.post(SYNTHETIC_PAIR_DEBIT, T0 + 2 * MINUTE)
        await self.assert_none()

    async def test_hand_added_legs(self):
        for paid_with, amount, debit_amount in (("bank", "10000", "10000.00"), ("cash", "10001", "10001.00")):
            response = await self.client.post("/api/v1/transactions", headers=ORIGIN, json={
                "type": "CREDIT", "amount": amount, "merchant": "Manas P", "paid_with": paid_with,
                "transaction_time": (T0 + MINUTE).isoformat(), "category": "Transfer"})
            self.assertEqual(response.status_code, 201, response.text)
            hand = await self.row(uuid.UUID(response.json()["id"]))
            self.assertEqual((hand.parsed_by, hand.account_ref), ("manual", "Cash" if paid_with == "cash" else None))
            await self.post(pair_debit(debit_amount), T0)
        await self.assert_none()

    async def test_missing_account_ref(self):
        credit = await self.post(SYNTHETIC_NO_ACCOUNT, T0)
        self.assertIsNone(credit["account_ref"])
        await self.post(SYNTHETIC_PAIR_DEBIT, T0 + 2 * MINUTE)
        await self.assert_none()

    async def test_debit_card_alert_never_pairs(self):
        # Stricter rule 1 (gated_steps_decision.md, step 20): the SMS must name a bank account.
        self.assertTrue(affects_bank_balance(SYNTHETIC_CARD_ALERT))
        self.assertIsNone(BANK_ACCOUNT_RE.search(card_text(SYNTHETIC_CARD_ALERT)))
        alert = await self.post(SYNTHETIC_CARD_ALERT, T0)
        self.assertEqual((alert["type"], alert["amount"], alert["account_ref"]), ("DEBIT", 10000, "HDFC XX5566"))
        await self.post(icici_credit(), T0 + 2 * MINUTE)
        await self.assert_none()
        # The other order.
        await self.post(icici_credit("10001.00"), T0 + 40 * MINUTE)
        await self.post(card_alert("10001.00"), T0 + 42 * MINUTE)
        await self.assert_none()

    async def test_cash_withdrawal_never_pairs(self):
        # Stricter rule 2: an ATM withdrawal is never a transfer to another of your accounts.
        atm = await self.post(SYNTHETIC_ATM, T0)
        self.assertEqual((atm["category"], atm["account_ref"]), ("Cash Withdrawal", "ICICI XX310"))
        self.assertTrue(transfers._names_bank_account(SYNTHETIC_ATM))  # only the category stops it
        await self.post(SYNTHETIC_PAIR_CREDIT, T0 + MINUTE)
        await self.assert_none()

    async def test_card_bill_and_card_payment_never_pair(self):
        bill = await self.post(SYNTHETIC_CARD_BILL, T0)
        self.assertEqual((bill["type"], bill["category"]), ("DEBIT", "Credit Card Payment"))
        await self.post(icici_credit(), T0 + 2 * MINUTE)
        await self.assert_none()
        debit = await self.post(pair_debit(), T0 + 3 * MINUTE)
        self.assertEqual(debit["account_ref"], "ICICI XX310")
        received = await self.post(SYNTHETIC_CARD_PAYMENT_RECEIVED, T0 + 4 * MINUTE)
        self.assertEqual((received["type"], received["category"], received["account_ref"]),
                         ("CREDIT", "Credit Card Payment", "ICICI XX7001"))
        await self.assert_none()

    async def test_investments_never_pair(self):
        sip = await self.post(SYNTHETIC_SIP, T0)
        self.assertEqual(sip["category"], "Investments & Finance")
        await self.post(icici_credit(), T0 + 2 * MINUTE)
        await self.assert_none()

    async def test_one_leg_already_self_transfer_pairs_but_not_both(self):
        debit_id = await self.post_id(pair_debit("10000.00"), T0)
        await self.patch_category(debit_id, "Self Transfer")
        credit = await self.post(pair_credit("10000.00", vpa="savings.manas@okhdfc"), T0 + 2 * MINUTE)
        self.assertNotEqual(credit["category"], "Self Transfer")
        self.assertIsNotNone(await self.pair_of(debit_id, uuid.UUID(credit["id"])))
        # Both already Self Transfer: a rule makes the credit arrive as one, and the debit is edited first.
        response = await self.client.post("/api/v1/rules", headers=ORIGIN, json={
            "sms_words": "XX4823", "direction": "CREDIT", "category": "Self Transfer"})
        self.assertEqual(response.status_code, 201, response.text)
        second_debit = await self.post_id(pair_debit("20000.00", name="ASHA M"), T0 + 60 * MINUTE)
        await self.patch_category(second_debit, "Self Transfer")
        second_credit = await self.post(pair_credit("20000.00", acct="XX4823", vpa="asha.m@okicici"),
                                        T0 + 62 * MINUTE)
        self.assertEqual(((await self.row(second_debit)).category, second_credit["category"]),
                         ("Self Transfer", "Self Transfer"))
        self.assertIsNone(await self.pair_of(second_debit, uuid.UUID(second_credit["id"])))
        self.assertEqual(len(await self.pairs()), 1)

    async def test_closest_wins(self):
        a = await self.post_id(pair_credit(acct="XX4821", vpa="manas.p@okicici"), T0)
        b = await self.post_id(pair_credit(acct="XX4822", vpa="savings.manas@okhdfc"), T0 + 20 * MINUTE)
        debit_id = await self.post_id(pair_debit(), T0 + 8 * MINUTE)
        [pair] = await self.pairs()
        self.assertEqual((pair.debit_id, pair.credit_id), (debit_id, a))
        self.assertNotEqual(pair.credit_id, b)

    async def test_tie_gives_none(self):
        await self.post(pair_credit(acct="XX4821", vpa="manas.p@okicici"), T0)
        await self.post(pair_credit(acct="XX4822", vpa="savings.manas@okhdfc"), T0 + 20 * MINUTE)
        await self.post(pair_debit(), T0 + 10 * MINUTE)
        await self.assert_none()

    async def test_second_run_creates_nothing_more(self):
        debit_id, credit_id, _ = await self.cuj2()
        await post_save.run(debit_id, "sms")
        await post_save.run(credit_id, "sms")
        self.assertEqual(len(await self.pairs()), 1)
        self.assertEqual(len(await self.suggestions()), 1)

    async def test_simultaneous_tasks_one_pair(self):
        debit_id = await self.quiet(SYNTHETIC_PAIR_DEBIT, T0)
        credit_id = await self.quiet(SYNTHETIC_PAIR_CREDIT, T0 + 2 * MINUTE)
        await self.assert_none()
        # Both legs' tasks find each other before either writes.
        partners = []
        for txn_id in (debit_id, credit_id):
            async with AsyncSessionLocal() as db:
                ctx = await post_save.load(db, txn_id, "sms")
                partners.append((transfers._leg(ctx.txn), await transfers._partner(db, ctx.owner, transfers._leg(ctx.txn))))
        (debit, debit_partner), (credit, credit_partner) = partners
        self.assertEqual((debit_partner.id, credit_partner.id), (credit_id, debit_id))
        results = []
        for _ in range(2):
            async with AsyncSessionLocal() as db:
                results.append(await transfers._suggest(db, self.owner.id, debit, credit))
        self.assertEqual(results, [True, False])
        self.assertEqual(len(await self.pairs()), 1)
        self.assertEqual(len(await self.suggestions()), 1)

    async def test_no_owner_configured(self):
        with patch("post_save.OWNER_EMAIL", ""):
            await self.post(SYNTHETIC_PAIR_DEBIT, T0)
            await self.post(SYNTHETIC_PAIR_CREDIT, T0 + 2 * MINUTE)
        await self.assert_none()

    async def test_other_users_rows_never_pair(self):
        credit_id = await self.post_id(SYNTHETIC_PAIR_CREDIT, T0)
        async with AsyncSessionLocal() as db:
            await db.execute(update(Transaction).where(Transaction.id == credit_id).values(user_id=self.other.id))
            await db.commit()
        self.assertEqual((await self.row(credit_id)).user_id, self.other.id)
        await self.post(SYNTHETIC_PAIR_DEBIT, T0 + 2 * MINUTE)
        await self.assert_none()

    async def test_held_out_leg_never_pairs(self):
        earlier = await self.post_id(DMART, T0 - 60 * MINUTE)
        credit_id = await self.post_id(SYNTHETIC_PAIR_CREDIT, T0)
        await self.flag(credit_id, earlier)
        await self.post(SYNTHETIC_PAIR_DEBIT, T0 + 2 * MINUTE)
        await self.assert_none()

    async def test_leg_in_confirmed_pair_is_not_offered_again(self):
        debit_id, credit_id, pair = await self.cuj2()
        self.assertEqual((await self.confirm(pair.id)).status_code, 200)
        another = await self.post_id(pair_credit(acct="XX4822", vpa="savings.manas@okhdfc"), T0 + 5 * MINUTE)
        self.assertIsNone(await self.pair_of(debit_id, another))
        self.assertEqual(len(await self.pairs()), 1)
        self.assertEqual(len(await self.suggestions()), 1)

    async def statements(self, txn_id) -> int:
        async with AsyncSessionLocal() as db:
            ctx = await post_save.load(db, txn_id, "sms")
            self.assertIsNotNone(ctx)
            return await self.count_statements(lambda: transfers.check(ctx))

    async def test_statement_budget(self):
        alert_id = await self.post_id(SYNTHETIC_CARD_ALERT, T0)
        self.assertEqual(await self.statements(alert_id), 0)
        debit_id = await self.post_id(SYNTHETIC_PAIR_DEBIT, T0 + 2 * MINUTE)
        self.assertEqual(await self.statements(debit_id), 1)
        await self.assert_none()

    async def test_registered_for_sms_only(self):
        [(check, causes)] = [(c, causes) for name, c, causes in post_save._CHECKS if name == "transfers"]
        self.assertIs(check, transfers.check)
        self.assertEqual(causes, {"sms"})
        self.assertEqual(post_save._CHECKS[-1][0], "transfers")
        debit_id = await self.quiet(SYNTHETIC_PAIR_DEBIT, T0)
        credit_id = await self.quiet(SYNTHETIC_PAIR_CREDIT, T0 + 2 * MINUTE)
        for cause in ("manual", "edit", "ai_retry", "restored"):
            await post_save.run(debit_id, cause)
            await post_save.run(credit_id, cause)
        await self.assert_none()
        await post_save.run(credit_id, "sms")  # the partner was there all along
        self.assertIsNotNone(await self.pair_of(debit_id, credit_id))


class RouteTests(TransferTestCase):
    async def spent(self) -> float:
        response = await self.client.get("/api/v1/limits")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["monthly"]["spent"]

    async def test_confirm(self):
        response = await self.client.put("/api/v1/limits", json={"monthly_limit": 100000}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        debit_id, credit_id, pair = await self.cuj2()
        self.assertEqual(await self.spent(), 10000)
        old = {txn_id: (await self.row(txn_id)).updated_at for txn_id in (debit_id, credit_id)}
        before = datetime.now(timezone.utc)
        response = await self.confirm(pair.id)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual({k: v for k, v in body.items() if k != "decided_at"}, {
            "id": str(pair.id), "status": "confirmed", "debit_id": str(debit_id), "credit_id": str(credit_id)})
        decided = datetime.fromisoformat(body["decided_at"].replace("Z", "+00:00"))
        self.assertEqual(decided.utcoffset(), timedelta(0))
        self.assertGreaterEqual(decided, before)
        for txn_id in (debit_id, credit_id):
            row = await self.row(txn_id)
            self.assertEqual(row.category, "Self Transfer")
            self.assertGreater(row.updated_at, old[txn_id])
        polled = (await self.client.get("/api/v1/transactions", params={"updated_after": before.isoformat()})).json()
        self.assertEqual({row["id"] for row in polled}, {str(debit_id), str(credit_id)})
        self.assertEqual(await self.spent(), 0)
        async with AsyncSessionLocal() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(MerchantCategory)), 0)
        stored = await self.get_pair(pair.id)
        self.assertEqual(stored.status, "confirmed")
        self.assertIsNotNone(stored.decided_at)
        item = await self.item_for(pair.id)
        self.assertEqual((item["actions"], item["resolved"]), ([], "Marked as self transfer"))

    async def test_reject_and_rerun(self):
        d = await self.post_id(pair_debit(), T0)
        c1 = await self.post_id(pair_credit(acct="XX4821", vpa="manas.p@okicici"), T0 + 5 * MINUTE)
        [p1] = await self.pairs()
        self.assertEqual((p1.debit_id, p1.credit_id), (d, c1))
        c2 = await self.post_id(pair_credit(acct="XX4822", vpa="savings.manas@okhdfc"), T0 + 20 * MINUTE)
        self.assertEqual(len(await self.pairs()), 1)  # D is already in a pending pair
        response = await self.reject(p1.id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["debit_id"], response.json()["credit_id"]),
                         ("rejected", str(d), str(c1)))
        self.assertIsNotNone(response.json()["decided_at"])
        for txn_id in (d, c1, c2):
            self.assertEqual((await self.row(txn_id)).category, "Uncategorized")
        p2 = await self.pair_of(d, c2)  # the re-check found D's other partner
        self.assertIsNotNone(p2)
        self.assertEqual(p2.status, "pending")
        self.assertEqual(len(await self.suggestions()), 2)
        await post_save.run(c1, "sms")
        self.assertEqual(len(await self.pairs()), 2)  # (D, C1) is never suggested again
        self.assertEqual(len(await self.suggestions()), 2)
        self.assertEqual((await self.get_pair(p1.id)).status, "rejected")
        item = await self.item_for(p1.id)
        self.assertEqual((item["actions"], item["resolved"]), ([], "Not a self transfer"))

    async def test_confirm_after_leg_became_card_payment(self):
        for i, category in enumerate(("Credit Card Payment", "Cash Withdrawal", "Investments & Finance")):
            with self.subTest(category=category):
                debit_id, credit_id, pair = await self.cuj2(f"1000{i}.00", name=f"PAYEE {chr(65 + i)}",
                                                           vpa=f"payee{i}@okicici")
                await self.patch_category(debit_id, category)
                response = await self.confirm(pair.id)
                self.assertEqual((response.status_code, response.json()), (409, {"detail": transfers.NO_LONGER}))
                stored = await self.get_pair(pair.id)
                self.assertEqual(stored.status, "expired")
                self.assertIsNotNone(stored.decided_at)
                self.assertEqual((await self.row(debit_id)).category, category)
                self.assertEqual((await self.row(credit_id)).category, "Uncategorized")
                item = await self.item_for(pair.id)
                self.assertEqual((item["actions"], item["resolved"]), ([], "No longer applies"))

    async def test_confirm_when_a_leg_is_gone(self):
        debit_id, credit_id, pair = await self.cuj2()
        await self.delete_txn(debit_id)
        self.assertIsNone(await self.get_pair(pair.id))  # release, same commit as the delete
        response = await self.confirm(pair.id)
        self.assertEqual((response.status_code, response.json()), (404, {"detail": transfers.NOT_FOUND}))
        item = await self.item_for(pair.id)
        self.assertEqual((item["actions"], item["resolved"]), ([], "No longer applies"))
        # A leg removed without release (SQLite doesn't enforce the foreign keys): the re-check fails.
        debit_id, credit_id, pair = await self.cuj2("20000.00", name="ASHA M", vpa="asha.m@okicici")
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Transaction).where(Transaction.id == debit_id))
            await db.commit()
        self.assertIsNotNone(await self.get_pair(pair.id))
        response = await self.confirm(pair.id)
        self.assertEqual((response.status_code, response.json()), (409, {"detail": transfers.NO_LONGER}))
        self.assertEqual((await self.get_pair(pair.id)).status, "expired")
        self.assertEqual((await self.row(credit_id)).category, "Uncategorized")

    async def test_confirm_when_a_leg_is_in_another_confirmed_pair(self):
        debit_id = await self.quiet(pair_debit(), T0)
        first_credit = await self.quiet(pair_credit(acct="XX4821", vpa="manas.p@okicici"), T0 + 2 * MINUTE)
        second_credit = await self.quiet(pair_credit(acct="XX4822", vpa="savings.manas@okhdfc"), T0 + 3 * MINUTE)
        debit = await self.leg(debit_id)
        for credit_id in (first_credit, second_credit):
            async with AsyncSessionLocal() as db:
                self.assertTrue(await transfers._suggest(db, self.owner.id, debit, await self.leg(credit_id)))
        first, second = await self.pair_of(debit_id, first_credit), await self.pair_of(debit_id, second_credit)
        self.assertEqual((first.status, second.status), ("pending", "pending"))
        self.assertEqual((await self.confirm(first.id)).status_code, 200)
        response = await self.confirm(second.id)
        self.assertEqual((response.status_code, response.json()), (409, {"detail": transfers.NO_LONGER}))
        self.assertEqual((await self.get_pair(second.id)).status, "expired")
        self.assertEqual((await self.row(second_credit)).category, "Uncategorized")

    async def test_double_confirm_and_answered(self):
        _, _, pair = await self.cuj2()
        self.assertEqual((await self.confirm(pair.id)).status_code, 200)
        for response in (await self.confirm(pair.id), await self.reject(pair.id)):
            self.assertEqual((response.status_code, response.json()), (409, {"detail": transfers.ANSWERED}))
        self.assertEqual((await self.get_pair(pair.id)).status, "confirmed")
        _, _, other = await self.cuj2("20000.00", name="ASHA M", vpa="asha.m@okicici")
        self.assertEqual((await self.reject(other.id)).status_code, 200)
        for response in (await self.reject(other.id), await self.confirm(other.id)):
            self.assertEqual((response.status_code, response.json()), (409, {"detail": transfers.ANSWERED}))
        self.assertEqual((await self.get_pair(other.id)).status, "rejected")

    async def test_ownership_and_auth(self):
        debit_id, credit_id, pair = await self.cuj2()
        app.dependency_overrides[current_user] = lambda: self.other
        for response in (await self.confirm(pair.id), await self.reject(pair.id)):
            self.assertEqual((response.status_code, response.json()), (404, {"detail": transfers.NOT_FOUND}))
        app.dependency_overrides[current_user] = lambda: self.owner
        for response in (await self.confirm(uuid.uuid4()), await self.reject(uuid.uuid4())):
            self.assertEqual((response.status_code, response.json()), (404, {"detail": transfers.NOT_FOUND}))
        for response in (await self.confirm("nope"), await self.reject("nope")):
            self.assertEqual(response.status_code, 422)
        for headers in ({"Origin": "http://evil.example"}, {}):
            for response in (await self.confirm(pair.id, headers), await self.reject(pair.id, headers)):
                self.assertEqual((response.status_code, response.json()), (403, {"detail": "Invalid request origin."}))
        app.dependency_overrides.pop(current_user)
        for response in (await self.confirm(pair.id), await self.reject(pair.id)):
            self.assertEqual(response.status_code, 401)
        stored = await self.get_pair(pair.id)
        self.assertEqual((stored.status, stored.decided_at), ("pending", None))
        for txn_id in (debit_id, credit_id):
            self.assertEqual((await self.row(txn_id)).category, "Uncategorized")
        self.assertEqual(len(await self.pairs()), 1)


class InboxTests(TransferTestCase):
    async def test_actions_and_resolved_texts(self):
        _, _, pending = await self.cuj2("11000.00", name="PAYEE A", vpa="payee1@okicici")
        _, _, confirmed = await self.cuj2("12000.00", name="PAYEE B", vpa="payee2@okicici")
        _, _, rejected = await self.cuj2("13000.00", name="PAYEE C", vpa="payee3@okicici")
        expired_debit, _, expired = await self.cuj2("14000.00", name="PAYEE D", vpa="payee4@okicici")
        _, gone_credit, gone = await self.cuj2("15000.00", name="PAYEE E", vpa="payee5@okicici")
        self.assertEqual((await self.confirm(confirmed.id)).status_code, 200)
        self.assertEqual((await self.reject(rejected.id)).status_code, 200)
        await self.patch_category(expired_debit, "Cash Withdrawal")
        self.assertEqual((await self.confirm(expired.id)).status_code, 409)
        await self.delete_txn(gone_credit)
        async with AsyncSessionLocal() as db:
            await create_once(db, self.owner.id, kind="spend_limit", title="Limit", body="b", link="/limits",
                              dedupe_key="test-spend-limit")
        item = await self.item_for(pending.id)
        self.assertEqual((item["title"], item["link"], item["resolved"]),
                         ("Looks like a transfer between your accounts", None, None))
        self.assertEqual(item["actions"], [
            {"label": "Confirm", "path": f"/api/v1/transfers/pairs/{pending.id}/confirm"},
            {"label": "Not a self transfer", "path": f"/api/v1/transfers/pairs/{pending.id}/reject"},
        ])
        for pair, text in ((confirmed, "Marked as self transfer"), (rejected, "Not a self transfer"),
                           (expired, "No longer applies"), (gone, "No longer applies")):
            item = await self.item_for(pair.id)
            self.assertEqual((item["actions"], item["resolved"]), ([], text), text)
        [limit] = [item for item in await self.inbox("spend_limit") if item["title"] == "Limit"]
        self.assertEqual((limit["actions"], limit["resolved"]), ([], None))

    async def test_resolver_registered_and_budget(self):
        self.assertIs(notifications._RESOLVERS["self_transfer"], transfers.pair_actions)
        for i in range(3):
            await self.cuj2(f"1100{i}.00", name=f"PAYEE {chr(65 + i)}", vpa=f"payee{i}@okicici")
        rows = await self.suggestions()
        self.assertEqual(len(rows), 3)
        async with AsyncSessionLocal() as db:
            self.assertEqual(await self.count_statements(lambda: transfers.pair_actions(db, rows)), 1)
            self.assertEqual(await self.count_statements(lambda: transfers.pair_actions(db, [])), 0)
            junk = [Notification(id=uuid.uuid4(), user_id=self.owner.id, kind=transfers.KIND, title="t", body="b",
                                 dedupe_key=key, created_at=NOW)
                    for key in ("pair:nope", f"refund:{uuid.uuid4()}:{uuid.uuid4()}", "pair:", None)]
            self.assertEqual(await self.count_statements(lambda: transfers.pair_actions(db, junk)), 0)
            self.assertEqual(await transfers.pair_actions(db, junk), {})


class DeleteTests(TransferTestCase):
    async def test_deleting_a_leg_removes_its_pair(self):
        n = 0
        for state in ("pending", "confirmed", "rejected"):
            for leg in ("debit", "credit"):
                with self.subTest(state=state, leg=leg):
                    n += 1
                    debit_id, credit_id, pair = await self.cuj2(f"{20000 + n}.00", name=f"PAYEE {chr(64 + n)}",
                                                               vpa=f"payee{n}@okicici")
                    if state == "confirmed":
                        self.assertEqual((await self.confirm(pair.id)).status_code, 200)
                    elif state == "rejected":
                        self.assertEqual((await self.reject(pair.id)).status_code, 200)
                    gone, kept = (debit_id, credit_id) if leg == "debit" else (credit_id, debit_id)
                    await self.delete_txn(gone)
                    self.assertIsNone(await self.get_pair(pair.id))
                    self.assertEqual((await self.row(kept)).category,
                                     "Self Transfer" if state == "confirmed" else "Uncategorized")
        self.assertEqual(await self.pairs(), [])
        _, _, pair = await self.cuj2()
        unrelated = await self.post_id(DMART, T0 + 60 * MINUTE)
        await self.delete_txn(unrelated)
        self.assertEqual((await self.get_pair(pair.id)).status, "pending")


class GateTests(TransferTestCase):
    @unittest.skipUnless(REAL_PAIRS, "pending one real HDFC/ICICI self-transfer SMS pair")
    async def test_real_pairs_pair(self):
        for debit_sms, credit_sms in REAL_PAIRS:
            with self.subTest(debit=debit_sms[:40]):
                await self.reset()
                debit_id = await self.post_id(debit_sms, T0)
                credit_id = await self.post_id(credit_sms, T0 + 2 * MINUTE)
                [pair] = await self.pairs()
                self.assertEqual((pair.debit_id, pair.credit_id, pair.status), (debit_id, credit_id, "pending"))
                self.assertEqual(len(await self.suggestions()), 1)


if __name__ == "__main__":
    unittest.main()
