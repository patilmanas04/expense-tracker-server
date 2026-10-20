# Folio · Expense Tracker API

FastAPI backend for an iPhone bank-SMS webhook and a private Next.js dashboard. This is a **single-owner application**: only `DASHBOARD_OWNER_EMAIL` may sign in. The owner can see their own and existing unassigned transactions; other users' assigned rows are excluded. No real data is seeded or modified by tests.

## Run

Keep your existing `.env` values. Add the new keys from `.env.example` without overwriting your database or webhook credentials.

```powershell
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Start the Next.js client separately with `npm install` and `npm run dev`. Visit `http://localhost:3000`. Next.js proxies `/api/v1/*` to FastAPI, so authentication and data share the frontend origin.

## Google sign-in setup

1. Select/create a project in [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
2. Configure Google Auth Platform branding, audience and consent. Add your Google account as a test user while the app is in Testing.
3. Create an OAuth client of type **Web application**.
4. Add this exact **Authorized redirect URI**: `http://localhost:3000/api/v1/auth/google/callback`.
5. Add these values to the **backend** `.env` and restart the backend:

```dotenv
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-google-client-secret
SESSION_SECRET=your-random-secret-at-least-32-characters
DASHBOARD_OWNER_EMAIL=your-google-account@gmail.com
FRONTEND_URL=http://localhost:3000
GOOGLE_REDIRECT_URI=http://localhost:3000/api/v1/auth/google/callback
```

Generate the session secret with `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep it stable across restarts and different from your webhook secret.

No Google secret belongs in Next.js or a `NEXT_PUBLIC_*` variable. This server-side authorization-code flow needs no Authorized JavaScript Origin; if you configure one, use the frontend origin. Only `openid email profile` scopes are requested. Authlib validates state, PKCE and OpenID Connect tokens. Signed, HttpOnly, SameSite=Lax cookies contain a user ID, not Google tokens, and expire after seven days. HTTPS cookies are automatic for an HTTPS `FRONTEND_URL`.

See [Google's server-side OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server) and [Authlib's Starlette integration](https://docs.authlib.org/en/latest/client/starlette.html).

## iPhone and production URLs

Use a stable HTTPS frontend domain for Google sign-in from your iPhone. Add `https://your-dashboard.example/api/v1/auth/google/callback` to Google Cloud's redirect URIs, and update `FRONTEND_URL` and `GOOGLE_REDIRECT_URI` accordingly. A plain LAN IP can preview the UI but is not suitable for this Google OAuth web redirect. Both services must remain running. Set the client `BACKEND_URL` to the backend's reachable internal/HTTPS address, then rebuild/restart Next.js.

## iPhone Shortcuts webhook

The existing endpoint and secret still work:

```http
POST /api/v1/webhook/sms
Content-Type: application/json
X-Webhook-Secret: YOUR_EXISTING_WEBHOOK_SECRET
```

```json
{
  "message": "INR 1234.56 debited from a/c XX1234 at ZOMATO",
  "sender": "VK-HDFCBK",
  "received_at": "2026-10-20T14:30:00+05:30"
}
```

Create a Message automation for your bank sender, then **Get Contents of URL**, POST, JSON body. Put the message variable in `message`, not the literal text "Shortcut Input". Set the secret header. `sender` and `received_at` are optional; any supplied date must be ISO 8601 with a timezone. Manual Play testing needs a Text action containing sample SMS text. `Authorization: Bearer ...` remains supported.

`success`, `duplicate`, and `ignored` responses use HTTP 200. Unknown formats are preserved with `is_parsed=false`, marked for review and excluded from totals. Analytics separate currencies, calculate amounts using Decimal, and use Asia/Kolkata dates. Net cash flow is income minus expenses for the selected period, not an account balance. Table filters do not alter period charts/totals. CSV export covers the current page.

## Live updates

`GET /api/v1/transactions?created_after=<ISO time with timezone>` returns only rows recorded at or after that time. The dashboard polls it every 5 seconds. The webhook stamps `created_at` right before inserting, after any Gemini call, so slow AI-parsed SMS aren't skipped by the polling cursor.

## Bank balance

Set your current bank balance in the dashboard's Settings. From that moment, every parsed debit SMS is subtracted and every credit added. Saving a new amount restarts the count from then.

- Credit-card alerts (`BLOCK CC`, `Avl Limit`, `Cardmember`, "credit card" without a bank account) are **not** deducted. A card spend leaves your account when you pay the card bill, and that payment arrives as its own bank debit SMS. Card bills paid from an account (`… debited from A/c … towards … Credit Card`) do count.
- Unparsed SMS (`is_parsed=false`) and other currencies are not counted.
- The value lives in the `balance_settings` table, which `create_all` adds automatically on the next server start.

```http
GET    /api/v1/balance            -> summary, or null if not set
PUT    /api/v1/balance            {"amount": "125000.50"}   (optional "as_of": ISO time with timezone)
DELETE /api/v1/balance
```

All three require the dashboard session. `PUT`/`DELETE` also require the dashboard `Origin`. When a balance is set, `success` and `duplicate` webhook responses include `balance` (`current`, `spent`, `received`, `excluded_card`, …). In Shortcuts you can add **Get Dictionary Value** `balance` → `current` and **Show Notification** "Balance left: …" after each SMS.

## Spend limits, notifications and (optional) email alerts

Set a weekly (Mon–Sun, IST) and/or monthly limit in **Settings → Spending limits**. When a debit SMS takes your spending to or past a limit, Folio creates one **in-app notification** for that week/month: a red dot on the bell on Home, opening a drawer, with each notification linking to Settings → Spending limits. That needs no setup. **Email is optional and off** until you add the Gmail keys below; with them, the same event also sends one email. "Spent" matches the dashboard: parsed debits, card spends included. The email goes out in the background after the webhook responds. A failed send is retried on the next payments (up to 3 tries), and the error shows in Settings. Raising a limit re-arms its alert.

Mail is sent with the **Gmail API** using OAuth and only the `gmail.send` scope, so Folio can send as you but can't read your mail.

### Optional: get the Gmail OAuth credentials (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/), open your project (the sign-in one is fine). Go to **APIs & Services → Library**, search **Gmail API** and click **Enable**.
2. **Google Auth Platform → Data Access → Add or remove scopes**: add `https://www.googleapis.com/auth/gmail.send` ("Send email on your behalf") and **Save**.
3. **Google Auth Platform → Audience**: click **Publish app** so the status is **In production**. While the app is in *Testing*, Google expires refresh tokens after 7 days and alerts would stop. Publishing a personal app doesn't need Google's review. You'll just see a "Google hasn't verified this app" screen when you approve it.
4. **Google Auth Platform → Clients → Create client**. Choose application type **Desktop app**, name it e.g. `Folio mail`, then **Create**. Copy the **Client ID** and **Client secret**. A Desktop client lets the helper script receive Google's redirect on `127.0.0.1` without registering a redirect URI.
5. Add them to `expense-tracker-server/.env`:
   ```dotenv
   GMAIL_CLIENT_ID=xxxx.apps.googleusercontent.com
   GMAIL_CLIENT_SECRET=GOCSPX-xxxx
   ```
6. From `expense-tracker-server`, run the helper and approve in the browser with the Gmail account that should send the alerts:
   ```bash
   uv run python scripts/gmail_authorize.py
   ```
   On the unverified-app screen choose **Advanced → Go to Folio mail (unsafe)**. It's your own app. Then allow **Send email on your behalf**. The terminal prints a line to copy into `.env`:
   ```dotenv
   GMAIL_REFRESH_TOKEN=1//0g-xxxx
   ```
7. Optional: `ALERT_EMAIL_TO=you@example.com`. It defaults to `DASHBOARD_OWNER_EMAIL`.
8. Restart the server (this also creates the `spend_limits` and `spend_alerts` tables), then use **Settings → Send test email**.

| Message in Settings | Fix |
| --- | --- |
| Refresh token expired or revoked | Run `scripts/gmail_authorize.py` again. This happens after a Google password change, when access is removed, or every 7 days if the app is still in Testing. |
| Gmail API has not been used in project … | Step 1: enable the Gmail API. |
| Insufficient authentication scopes | Run the script again and allow the send permission. |
| Rejected GMAIL_CLIENT_ID / SECRET | The refresh token must come from the same client as these two values. |

```http
GET  /api/v1/notifications       -> {"unread": 1, "items": [{id, kind, title, body, link, created_at, read}]}
POST /api/v1/notifications/read  {"ids": ["…"]}  (omit ids to mark all read)
GET  /api/v1/limits              -> this week/month spent, limits, notified flag, email status
PUT  /api/v1/limits              {"weekly_limit": "5000", "monthly_limit": null}   (null = no limit)
POST /api/v1/limits/test-email
```

## Webhook review

Implemented:

- Authentication on transaction/dashboard reads; explicit frontend CORS origin.
- Fixed truncation of four-plus-digit amounts and added Indian comma grouping for amounts/balances.
- Atomic duplicate-insert recovery for concurrent retries using the existing unique hash constraint.
- Removed raw SMS logging; bounded input lengths and timezone validation.
- Ignored obvious OTP, failed/declined payment, scheduled debit and payment-request messages.
- Retained successful regex parsing when merchant is missing, avoiding unnecessary AI requests.
- Asynchronous Gemini calls with a 20-second timeout, validated fields and safe fallback after parser errors.
- Excluded unparsed transactions from analytics and omitted raw SMS from the dashboard response.

Remaining considerations:

- Configure request-body/rate limits at the public reverse proxy, use stable HTTPS, and introduce migrations when models change (`create_all` does not migrate existing columns).
- The personal dashboard reads a maximum 367-day range into memory. Move aggregation to SQL for larger datasets, and queue ingestion if volume increases.
- Hash deduplication treats identical normalized SMS text as the same event. Distinct identical messages require a bank/event identifier to distinguish them.
- Parsing order: regex (tuned for HDFC/ICICI account, card and UPI alerts; the earliest debit/credit keyword decides direction, and balance/limit/due amounts are never used as the transaction amount) → Gemini full parse if regex cannot find a type and amount → Gemini category-only call if regex parsed it but no keyword rule matched (`parsed_by=regex+ai`) → `Uncategorized`. Categories are listed in `parser.CATEGORIES`.
- Bank formats vary. The parser uses the first currency-labelled amount that is not a balance/limit; it does not extract embedded SMS dates. `received_at` (or receipt time) drives the dashboard date. Verify your bank's formats.
- On regex failure, the existing Gemini integration sends the SMS to Google. Remove `GEMINI_API_KEY` to preserve unmatched messages locally for review instead. Set `GEMINI_MODEL` to one available to your account; the original model default is retained. Editing review records is outside this read-only dashboard.

## Checks

```powershell
uv run python -m unittest discover -s tests -v
uv run python check_database.py
```

Tests use an isolated in-memory SQLite database. `check_database.py` makes a read-only count query against your configured database. Provider responses are mocked in OAuth tests; real Google login requires your credentials.
