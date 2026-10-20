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
- Bank formats vary. The parser uses the first currency-labelled amount; it does not extract embedded SMS dates. `received_at` (or receipt time) drives the dashboard date. Verify your bank's formats.
- On regex failure, the existing Gemini integration sends the SMS to Google. Remove `GEMINI_API_KEY` to preserve unmatched messages locally for review instead. Set `GEMINI_MODEL` to one available to your account; the original model default is retained. Editing review records is outside this read-only dashboard.

## Checks

```powershell
uv run python -m unittest discover -s tests -v
uv run python check_database.py
```

Tests use an isolated in-memory SQLite database. `check_database.py` makes a read-only count query against your configured database. Provider responses are mocked in OAuth tests; real Google login requires your credentials.
