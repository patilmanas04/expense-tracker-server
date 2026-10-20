"""Authorise Folio to send alert emails from your Gmail and print GMAIL_REFRESH_TOKEN.

1. Put GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (a "Desktop app" OAuth client) in .env
2. From expense-tracker-server run:   uv run python scripts/gmail_authorize.py
3. Approve in the browser, then copy the printed GMAIL_REFRESH_TOKEN line into .env

Only the gmail.send scope is requested: Folio can send mail as you but can't read it.
Nothing is written to disk.
"""
import base64
import hashlib
import html
import http.server
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
from mailer import SCOPE, TOKEN_URL  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def pkce_pair() -> tuple[str, str]:
    """PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str,
                      login_hint: Optional[str] = None) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # always return one, even if you authorised before
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str,
                  transport: Optional[httpx.BaseTransport] = None) -> dict:
    with httpx.Client(transport=transport, timeout=20) as client:
        response = client.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    if response.status_code != 200:
        raise SystemExit(f"Google refused the authorisation code: {response.text}")
    return response.json()


class CallbackServer:
    """Listens on 127.0.0.1 (random port) for Google's redirect back after you approve."""

    def __init__(self, state: str):
        self.state = state
        self.code: Optional[str] = None
        self.error: Optional[str] = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
                if not query:  # e.g. the browser asking for /favicon.ico
                    self.send_response(404)
                    self.end_headers()
                    return
                if query.get("state") != outer.state:
                    outer.error = "state mismatch"
                elif "error" in query:
                    outer.error = query["error"]
                else:
                    outer.code = query.get("code")
                message = (
                    "<h1>Folio can now send alert emails.</h1><p>Close this tab and return to the terminal.</p>"
                    if outer.code else f"<h1>Authorisation failed</h1><p>{html.escape(outer.error or 'No code received')}</p>"
                )
                body = f"<!doctype html><meta charset=utf-8><body style='font-family:sans-serif;padding:40px'>{message}"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):  # keep the terminal clean
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.timeout = 1
        self.redirect_uri = f"http://127.0.0.1:{self.httpd.server_port}/"

    def wait(self, timeout: float = 300) -> None:
        deadline = time.monotonic() + timeout
        try:
            while self.code is None and self.error is None and time.monotonic() < deadline:
                self.httpd.handle_request()
        finally:
            self.httpd.server_close()


def main() -> int:
    load_dotenv(SERVER_DIR / ".env")
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Add GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET to expense-tracker-server/.env first (README → Email alerts).")
        return 1

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    server = CallbackServer(state)
    hint = os.getenv("ALERT_EMAIL_TO") or os.getenv("DASHBOARD_OWNER_EMAIL")
    url = authorization_url(client_id, server.redirect_uri, state, challenge, login_hint=hint)
    print("Opening Google in your browser. If it doesn't open, visit this URL:\n")
    print(url + "\n")
    print('If Google says "Google hasn\'t verified this app", choose Advanced → Go to (your app) — it is your own app.\n')
    webbrowser.open(url)
    server.wait()

    if not server.code:
        print(f"Authorisation failed: {server.error or 'timed out after 5 minutes'}")
        return 1
    tokens = exchange_code(client_id, client_secret, server.code, server.redirect_uri, verifier)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("Google didn't return a refresh token. Remove the app at https://myaccount.google.com/permissions and run this again.")
        return 1
    if SCOPE not in tokens.get("scope", "").split():
        print("Warning: the gmail.send permission wasn't granted. Run this again and tick it on the consent screen.")
        return 1
    print("Success! Add this line to expense-tracker-server/.env, then restart the server:\n")
    print(f"GMAIL_REFRESH_TOKEN={refresh_token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
