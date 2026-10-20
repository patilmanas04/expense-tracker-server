"""Spend limits and Gmail alert tests. A fake Google server stands in for the network;
the real database and Gmail are never touched."""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-at-least-thirty-two-characters"
os.environ["DASHBOARD_OWNER_EMAIL"] = "owner@example.com"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["GEMINI_API_KEY"] = ""
for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "ALERT_EMAIL_TO"):
    os.environ[key] = ""

import base64
import email
import email.policy
import hashlib
import json
import threading
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import limits
import mailer
from auth import current_user
from database import AsyncSessionLocal, Base, engine
from main import app
from models import SpendAlert, Transaction, User
from scripts import gmail_authorize

GMAIL_ENV = {
    "GMAIL_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "GMAIL_CLIENT_SECRET": "test-client-secret",
    "GMAIL_REFRESH_TOKEN": "1//test-refresh-token",
}
ORIGIN = {"Origin": "http://localhost:3000"}
HOOK = {"X-Webhook-Secret": "test-webhook-secret"}
# Wednesday 21 Oct 2026, 12:00 IST. Week = Mon 19 – Sun 25 Oct; month = October.
NOW = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))


class FakeGoogle:
    """Answers the OAuth token and Gmail send endpoints like Google does."""

    def __init__(self):
        self.token_requests = []
        self.sent = []
        self.send_failures = 0  # fail this many sends before succeeding
        self.token_error = None
        self.send_error = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == mailer.TOKEN_URL:
            self.token_requests.append(dict(urllib.parse.parse_qsl(request.content.decode())))
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error})
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3599, "token_type": "Bearer"})
        if str(request.url) == mailer.SEND_URL:
            assert request.headers["Authorization"] == "Bearer ya29.test"
            if self.send_error:
                return httpx.Response(403, json={"error": {"code": 403, "message": self.send_error}})
            if self.send_failures:
                self.send_failures -= 1
                return httpx.Response(500, json={"error": {"code": 500, "message": "Backend Error"}})
            raw = json.loads(request.content)["raw"]
            self.sent.append(email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default))
            return httpx.Response(200, json={"id": f"msg-{len(self.sent)}", "labelIds": ["SENT"]})
        return httpx.Response(404)


def body_text(message) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


class GoogleTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.google = FakeGoogle()
        mailer._TRANSPORT = httpx.MockTransport(self.google.handler)
        mailer._token = None
        env = patch.dict(os.environ, GMAIL_ENV)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        mailer._TRANSPORT = None
        mailer._token = None


class MailerTests(GoogleTestCase):
    async def test_sends_multipart_message_through_gmail_api(self):
        message_id = await mailer.send_email("me@example.com", "Hello ₹", "Plain body", "<p>HTML body</p>")
        self.assertEqual(message_id, "msg-1")
        sent = self.google.sent[0]
        self.assertEqual((sent["To"], sent["Subject"]), ("me@example.com", "Hello ₹"))
        self.assertIn("Plain body", body_text(sent))
        self.assertIn("<p>HTML body</p>", sent.get_body(preferencelist=("html",)).get_content())
        token_request = self.google.token_requests[0]
        self.assertEqual(token_request["grant_type"], "refresh_token")
        self.assertEqual(token_request["refresh_token"], GMAIL_ENV["GMAIL_REFRESH_TOKEN"])
        self.assertEqual(token_request["client_id"], GMAIL_ENV["GMAIL_CLIENT_ID"])

    async def test_reuses_access_token_until_it_expires(self):
        await mailer.send_email("me@example.com", "One", "1")
        await mailer.send_email("me@example.com", "Two", "2")
        self.assertEqual((len(self.google.token_requests), len(self.google.sent)), (1, 2))

    async def test_revoked_refresh_token_explains_the_fix(self):
        self.google.token_error = "invalid_grant"
        with self.assertRaises(mailer.MailError) as caught:
            await mailer.send_email("me@example.com", "Hi", "x")
        self.assertIn("gmail_authorize.py", str(caught.exception))

    async def test_gmail_error_message_is_surfaced(self):
        self.google.send_error = "Gmail API has not been used in project 123 before or it is disabled."
        with self.assertRaises(mailer.MailError) as caught:
            await mailer.send_email("me@example.com", "Hi", "x")
        self.assertIn("Gmail API has not been used", str(caught.exception))

    async def test_not_configured(self):
        with patch.dict(os.environ, {"GMAIL_REFRESH_TOKEN": ""}):
            self.assertFalse(mailer.configured())
            with self.assertRaises(mailer.MailNotConfigured):
                await mailer.send_email("me@example.com", "Hi", "x")

    def test_recipient_defaults_to_owner(self):
        self.assertEqual(mailer.recipient(), "owner@example.com")
        with patch.dict(os.environ, {"ALERT_EMAIL_TO": "alerts@example.com"}):
            self.assertEqual(mailer.recipient(), "alerts@example.com")


class PeriodAndFormatTests(unittest.TestCase):
    def test_week_runs_monday_to_sunday_in_ist(self):
        # Thursday 01:30 IST is still Wednesday in UTC.
        start, start_utc, end_utc = limits.period_window("weekly", datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(start, date(2026, 10, 19))
        self.assertEqual(start_utc, datetime(2026, 10, 18, 18, 30, tzinfo=timezone.utc))
        self.assertEqual(end_utc, datetime(2026, 10, 25, 18, 30, tzinfo=timezone.utc))

    def test_month_follows_ist_calendar(self):
        start, start_utc, end_utc = limits.period_window("monthly", datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(start, date(2026, 11, 1))
        self.assertEqual(end_utc, datetime(2026, 11, 30, 18, 30, tzinfo=timezone.utc))

    def test_indian_number_format(self):
        self.assertEqual(limits.format_money(Decimal("123456.5"), "INR"), "₹1,23,456.50")
        self.assertEqual(limits.format_money(Decimal("12345678.9"), "INR"), "₹1,23,45,678.90")
        self.assertEqual(limits.format_money(Decimal("999"), "INR"), "₹999.00")


class LimitsApiTests(GoogleTestCase):
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
        clock = patch("limits.utc_now", return_value=NOW)
        clock.start()
        self.addAsyncCleanup(self.client.aclose)
        self.addCleanup(clock.stop)
        self.addCleanup(app.dependency_overrides.clear)
        self.counter = 0

    def sign_in(self):
        app.dependency_overrides[current_user] = lambda: self.owner

    async def set_limits(self, weekly=None, monthly=None):
        response = await self.client.put("/api/v1/limits", json={"weekly_limit": weekly, "monthly_limit": monthly}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def debit(self, amount, merchant="SWIGGY", when=NOW, card=False):
        self.counter += 1
        if card:
            message = f"Spent Rs.{amount} On HDFC Bank Card 9012 At {merchant} On 2026-10-21.Not You? SMS BLOCK CC 9012 to 7308080808 #{self.counter}"
        else:
            message = f"Sent Rs.{amount}\nFrom HDFC Bank A/C *4821\nTo {merchant}\nOn 21/10/26\nRef 6294000{self.counter:05d}"
        response = await self.client.post(
            "/api/v1/webhook/sms", json={"message": message, "received_at": when.isoformat()}, headers=HOOK
        )
        self.assertEqual(response.json()["status"], "success", response.text)

    async def seed(self, rows):
        async with AsyncSessionLocal() as db:
            for i, (kind, amount, when, parsed, currency, owner) in enumerate(rows):
                # Stored in UTC like the webhook does (SQLite would drop an IST offset without converting).
                db.add(Transaction(type=kind, amount=amount, currency=currency, is_parsed=parsed, user_id=owner,
                                   raw_message="fixture", message_hash=f"seed-{i}",
                                   transaction_time=when.astimezone(timezone.utc)))
            await db.commit()

    async def alerts(self):
        async with AsyncSessionLocal() as db:
            return (await db.scalars(select(SpendAlert).order_by(SpendAlert.created_at))).all()

    async def test_requires_login_and_same_origin(self):
        self.assertEqual((await self.client.get("/api/v1/limits")).status_code, 401)
        self.sign_in()
        foreign = {"Origin": "https://attacker.example"}
        put = await self.client.put("/api/v1/limits", json={"weekly_limit": 100}, headers=foreign)
        test_mail = await self.client.post("/api/v1/limits/test-email", headers=foreign)
        self.assertEqual((put.status_code, test_mail.status_code), (403, 403))
        self.assertEqual(self.google.sent, [])

    async def test_validation(self):
        self.sign_in()
        for body in [{"weekly_limit": 0}, {"monthly_limit": -5}, {"weekly_limit": "10.555"}, {"monthly_limit": "1e16"}]:
            with self.subTest(body=body):
                response = await self.client.put("/api/v1/limits", json=body, headers=ORIGIN)
                self.assertEqual(response.status_code, 422, response.text)

    async def test_summary_counts_week_and_month_in_ist(self):
        self.sign_in()
        await self.seed([
            ("DEBIT", 100, datetime(2026, 10, 20, 9, 0, tzinfo=IST), True, "INR", self.owner.id),  # this week
            ("DEBIT", 60, datetime(2026, 10, 19, 0, 10, tzinfo=IST), True, "INR", None),  # Monday 00:10 IST
            ("DEBIT", 300, datetime(2026, 10, 18, 23, 30, tzinfo=IST), True, "INR", None),  # Sunday, last week
            ("DEBIT", 200, datetime(2026, 10, 2, 12, 0, tzinfo=IST), True, "INR", None),  # earlier this month
            ("DEBIT", 400, datetime(2026, 9, 30, 12, 0, tzinfo=IST), True, "INR", None),  # last month
            ("CREDIT", 1000, datetime(2026, 10, 20, 9, 0, tzinfo=IST), True, "INR", None),
            ("DEBIT", 999, datetime(2026, 10, 20, 9, 0, tzinfo=IST), False, "INR", None),  # needs review
            ("DEBIT", 50, datetime(2026, 10, 20, 9, 0, tzinfo=IST), True, "USD", None),
            ("DEBIT", 777, datetime(2026, 10, 20, 9, 0, tzinfo=IST), True, "INR", self.other.id),
        ])
        await self.set_limits(weekly="5000", monthly="20000")
        data = (await self.client.get("/api/v1/limits")).json()
        self.assertEqual((data["weekly"]["spent"], data["weekly"]["limit"]), (160, 5000))
        self.assertEqual((data["monthly"]["spent"], data["monthly"]["limit"]), (660, 20000))
        self.assertEqual((data["weekly"]["start"], data["weekly"]["end"]), ("2026-10-19", "2026-10-25"))
        self.assertEqual((data["monthly"]["start"], data["monthly"]["end"]), ("2026-10-01", "2026-10-31"))
        self.assertEqual(data["email"], {"configured": True, "to": "owner@example.com", "last_error": None})

    async def test_crossing_the_weekly_limit_sends_exactly_one_email(self):
        self.sign_in()
        await self.set_limits(weekly="500")
        await self.debit("300.00")
        self.assertEqual(self.google.sent, [])
        await self.debit("250.00", merchant="ZOMATO")
        self.assertEqual(len(self.google.sent), 1)
        mail = self.google.sent[0]
        self.assertEqual(mail["To"], "owner@example.com")
        self.assertIn("Weekly spend limit crossed", mail["Subject"])
        text = body_text(mail)
        for expected in ["₹550.00", "₹500.00", "110%", "Zomato", "19 Oct", "25 Oct", "http://localhost:3000"]:
            self.assertIn(expected, text)
        await self.debit("100.00")
        self.assertEqual(len(self.google.sent), 1)
        self.assertTrue((await self.client.get("/api/v1/limits")).json()["weekly"]["alerted"])

    async def test_touching_the_limit_exactly_counts(self):
        self.sign_in()
        await self.set_limits(monthly="500")
        await self.debit("500.00")
        self.assertEqual(len(self.google.sent), 1)
        self.assertIn("Monthly spend limit reached", self.google.sent[0]["Subject"])

    async def test_weekly_and_monthly_limits_alert_independently(self):
        self.sign_in()
        await self.seed([("DEBIT", 900, datetime(2026, 10, 5, 12, 0, tzinfo=IST), True, "INR", None)])
        await self.set_limits(weekly="1000", monthly="1200")
        await self.debit("400.00")  # week 400 (< 1000), month 1300 (>= 1200)
        self.assertEqual([m["Subject"].split(":")[0] for m in self.google.sent], ["Monthly spend limit crossed"])
        await self.debit("700.00")  # week 1100 crosses now; month already alerted
        self.assertEqual(len(self.google.sent), 2)
        self.assertIn("Weekly", self.google.sent[1]["Subject"])

    async def test_card_spends_count_and_credits_never_alert(self):
        self.sign_in()
        await self.set_limits(weekly="1000")
        response = await self.client.post("/api/v1/webhook/sms", headers=HOOK, json={
            "message": "Credit Alert!\nRs.5,000.00 credited to HDFC Bank A/c XX4821 on 21-10-26 from VPA a@okaxis (UPI 629400099999)",
            "received_at": NOW.isoformat()})
        self.assertEqual(response.json()["status"], "success")
        self.assertEqual(self.google.sent, [])
        await self.debit("1,200", merchant="AMAZON", card=True)
        self.assertEqual(len(self.google.sent), 1)

    async def test_raising_the_limit_rearms_the_alert(self):
        self.sign_in()
        await self.set_limits(weekly="500")
        await self.debit("600.00")
        await self.set_limits(weekly="1000")
        await self.debit("300.00")  # 900 < 1000
        self.assertEqual(len(self.google.sent), 1)
        await self.debit("150.00")  # 1050 >= 1000
        self.assertEqual(len(self.google.sent), 2)
        self.assertIn("₹1,000.00", body_text(self.google.sent[1]))

    async def test_failed_send_is_retried_on_the_next_payment(self):
        self.sign_in()
        await self.set_limits(weekly="500")
        self.google.send_failures = 1
        await self.debit("600.00")
        self.assertEqual(self.google.sent, [])
        [alert] = await self.alerts()
        self.assertEqual((alert.status, alert.attempts), ("failed", 1))
        summary = (await self.client.get("/api/v1/limits")).json()
        self.assertIn("Backend Error", summary["email"]["last_error"])
        self.assertTrue(summary["weekly"]["alerted"])  # the in-app notification still went out
        await self.debit("10.00")
        self.assertEqual(len(self.google.sent), 1)
        [alert] = await self.alerts()
        self.assertEqual((alert.status, alert.attempts), ("sent", 2))
        self.assertIn("₹610.00", body_text(self.google.sent[0]))

    async def test_no_email_or_alert_record_when_gmail_is_not_configured(self):
        self.sign_in()
        await self.set_limits(weekly="100")
        with patch.dict(os.environ, {"GMAIL_REFRESH_TOKEN": ""}):
            await self.debit("500.00")
            summary = (await self.client.get("/api/v1/limits")).json()
        self.assertEqual((self.google.sent, await self.alerts()), ([], []))
        self.assertFalse(summary["email"]["configured"])
        # Once configured, the next payment sends the pending alert.
        await self.debit("1.00")
        self.assertEqual(len(self.google.sent), 1)

    async def test_late_sms_from_a_past_week_does_not_alert(self):
        self.sign_in()
        await self.set_limits(weekly="100")
        await self.debit("500.00", when=NOW - timedelta(days=7))
        self.assertEqual(self.google.sent, [])

    async def test_clearing_limits_stops_alerts(self):
        self.sign_in()
        await self.set_limits(weekly="100")
        cleared = await self.set_limits()
        self.assertEqual((cleared["weekly"]["limit"], cleared["monthly"]["limit"]), (None, None))
        await self.debit("500.00")
        self.assertEqual(self.google.sent, [])

    async def test_test_email_endpoint(self):
        self.sign_in()
        ok = await self.client.post("/api/v1/limits/test-email", headers=ORIGIN)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json(), {"status": "sent", "to": "owner@example.com"})
        self.assertIn("test", self.google.sent[0]["Subject"].lower())
        self.google.send_error = "Request had insufficient authentication scopes."
        failed = await self.client.post("/api/v1/limits/test-email", headers=ORIGIN)
        self.assertEqual(failed.status_code, 502)
        self.assertIn("insufficient authentication scopes", failed.json()["detail"])
        with patch.dict(os.environ, {"GMAIL_CLIENT_ID": ""}):
            missing = await self.client.post("/api/v1/limits/test-email", headers=ORIGIN)
        self.assertEqual(missing.status_code, 503)
        self.assertIn("GMAIL_REFRESH_TOKEN", missing.json()["detail"])


class GmailAuthorizeScriptTests(unittest.TestCase):
    def test_pkce_challenge_is_s256_of_verifier(self):
        verifier, challenge = gmail_authorize.pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)
        self.assertGreaterEqual(len(verifier), 43)

    def test_authorization_url_requests_offline_send_only_access(self):
        url = gmail_authorize.authorization_url("cid", "http://127.0.0.1:5555/", "state123", "challenge")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(query["scope"], "https://www.googleapis.com/auth/gmail.send")
        self.assertEqual((query["access_type"], query["prompt"]), ("offline", "consent"))
        self.assertEqual((query["code_challenge"], query["code_challenge_method"]), ("challenge", "S256"))
        self.assertEqual((query["state"], query["redirect_uri"], query["client_id"]), ("state123", "http://127.0.0.1:5555/", "cid"))

    def test_exchange_code_returns_refresh_token(self):
        seen = {}

        def handler(request):
            seen.update(urllib.parse.parse_qsl(request.content.decode()))
            return httpx.Response(200, json={"access_token": "a", "refresh_token": "1//new", "scope": gmail_authorize.SCOPE})

        tokens = gmail_authorize.exchange_code("cid", "sec", "code1", "http://127.0.0.1:5555/", "verifier", transport=httpx.MockTransport(handler))
        self.assertEqual(tokens["refresh_token"], "1//new")
        self.assertEqual((seen["grant_type"], seen["code_verifier"], seen["code"]), ("authorization_code", "verifier", "code1"))

    def test_callback_server_captures_code_and_checks_state(self):
        for state, query, expected in [
            ("good", "code=abc&state=good", ("abc", None)),
            ("good", "code=abc&state=forged", (None, "state mismatch")),
            ("good", "error=access_denied&state=good", (None, "access_denied")),
        ]:
            with self.subTest(query=query):
                server = gmail_authorize.CallbackServer(state)
                thread = threading.Thread(target=server.wait, kwargs={"timeout": 5})
                thread.start()
                response = httpx.get(f"{server.redirect_uri}?{query}", timeout=5)
                thread.join(5)
                self.assertEqual(response.status_code, 200)
                self.assertEqual((server.code, server.error), expected)


if __name__ == "__main__":
    unittest.main()
