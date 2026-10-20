"""Sends mail through the Gmail API with an OAuth refresh token.

Only the `gmail.send` scope is used: Folio can send mail as you but can't read your inbox.
Run `scripts/gmail_authorize.py` once to mint GMAIL_REFRESH_TOKEN.
"""
import base64
import os
import time
from email.message import EmailMessage
from typing import Optional

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPE = "https://www.googleapis.com/auth/gmail.send"
REAUTHORIZE_HINT = (
    "Run `uv run python scripts/gmail_authorize.py` again and update GMAIL_REFRESH_TOKEN in .env. "
    "Tokens expire after 7 days while the Google consent screen is in Testing, and when you change your Google password."
)

# Tests swap in httpx.MockTransport; None means the real network.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_token: Optional[tuple[str, float]] = None  # (access token, monotonic expiry)


class MailError(Exception):
    """Google refused the request; the message says what to fix."""


class MailNotConfigured(MailError):
    pass


def _credentials() -> tuple[str, str, str]:
    return (
        os.getenv("GMAIL_CLIENT_ID", "").strip(),
        os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        os.getenv("GMAIL_REFRESH_TOKEN", "").strip(),
    )


def configured() -> bool:
    return all(_credentials())


def recipient() -> str:
    """ALERT_EMAIL_TO, or the dashboard owner's address."""
    return (os.getenv("ALERT_EMAIL_TO", "").strip() or os.getenv("DASHBOARD_OWNER_EMAIL", "").strip()).lower()


def _google_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("message") or f"HTTP {response.status_code}"
    return body.get("error_description") or error or f"HTTP {response.status_code}"


async def _access_token(client: httpx.AsyncClient) -> str:
    global _token
    if _token and _token[1] > time.monotonic() + 60:
        return _token[0]
    client_id, client_secret, refresh_token = _credentials()
    response = await client.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    if response.status_code != 200:
        error = _google_error(response)
        if error == "invalid_grant":
            raise MailError(f"Google rejected the Gmail refresh token (expired or revoked). {REAUTHORIZE_HINT}")
        if error in ("invalid_client", "unauthorized_client"):
            raise MailError("Google rejected GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET. Check they match the OAuth client used to create the refresh token.")
        raise MailError(f"Couldn't get a Gmail access token: {error}")
    data = response.json()
    _token = (data["access_token"], time.monotonic() + int(data.get("expires_in", 3600)))
    return _token[0]


def build_message(to: str, subject: str, text: str, html: Optional[str] = None) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject  # From is filled in by Gmail with the authorised account
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return message


async def send_email(to: str, subject: str, text: str, html: Optional[str] = None) -> str:
    """Sends one email and returns Gmail's message id."""
    global _token
    if not configured():
        raise MailNotConfigured(
            "Email alerts aren't set up. Add GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN "
            "to the server .env and restart it."
        )
    raw = base64.urlsafe_b64encode(build_message(to, subject, text, html).as_bytes()).decode()
    async with httpx.AsyncClient(transport=_TRANSPORT, timeout=20) as client:
        for attempt in range(2):
            token = await _access_token(client)
            response = await client.post(SEND_URL, json={"raw": raw}, headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 401 and attempt == 0:
                _token = None  # access token revoked early; refresh once and retry
                continue
            break
    if response.status_code >= 400:
        raise MailError(f"Gmail didn't send the email: {_google_error(response)}")
    return response.json().get("id", "")
