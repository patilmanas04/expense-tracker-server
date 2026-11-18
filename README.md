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

`success`, `duplicate`, and `ignored` responses use HTTP 200. When the SMS looks like a second record of an earlier transaction (see Possible duplicates), the status is still `success` and the message reads "Transaction logged successfully via regex (possible duplicate: held out of totals)", so the Shortcut can show why Balance left didn't move. Unknown formats are preserved with `is_parsed=false`, marked for review and excluded from totals. Analytics separate currencies, calculate amounts using Decimal, and use Asia/Kolkata dates. Net cash flow is income minus expenses for the selected period, not an account balance. Table filters do not alter period charts/totals. CSV export covers the current page.

## Live updates

`GET /api/v1/transactions?updated_after=<ISO time with timezone>` returns only rows added **or changed** at or after that time: new SMS, your edits, and categories filled in later by the background retry. The dashboard polls it every 5 seconds. `created_after` still works for new rows only. The server stamps `created_at`/`updated_at` itself, right before writing (after any Gemini call), so the polling cursor never skips a slow row.

## Categories

Each SMS gets its name and category from, in order:

0. **Your merged names and category rules** (see Names and rules). The merchant becomes its merged name first; when the bank's name had no keyword category, the keyword rules run again on the merged name ("Amazonpayindiap" merged into "Amazon" is Shopping). Then the newest category rule that matches sets the category (`parsed_by = "regex+rule"`) and the steps below, Gemini included, are skipped. Both come from one query.
1. **Your category for that merchant.** Editing a transaction's category (or picking one when adding by hand) saves it in `merchant_categories`, per merchant and direction (paying vs. money in). It beats the keyword rules. Choosing Uncategorized forgets it.
2. **The keyword rules** in `parser.py`.
3. **Your earlier payments** to the same merchant: the category of the latest categorised one, never "Self Transfer" (only a category you picked in step 1 carries that to the next payment), and never one a category rule set on an SMS the regex parser read (`parsed_by = "regex+rule"`, on arrival or by Apply to past; its conditions decide: a rule for rent of ₹15,000 or more doesn't spread to a ₹300 payment to the same person). A rule's category on a payment added by hand with Auto (`manual`) or read by Gemini (`ai_agent`) has no marker, so those payments do count here, like any category they carry.
4. **Gemini**, waited on for at most 5 s so the Shortcut stays quick. If it's slower or failing, the SMS is saved Uncategorized and a background task retries with a 30 s limit (right away, then after 1 and 5 minutes) and fills the category in. An edit you make meanwhile always wins. Retries live in the server process, so a restart during those minutes drops them; the transaction just stays Uncategorized for you to set.

**Card bills.** A debit SMS that names a credit card and a bank account ("Rs.3000.00 debited from HDFC Bank A/c XX4821 … towards HDFC Bank Credit Card XX9012") is a card bill paid from the bank: **Credit Card Payment**. The check (`parser.is_card_bill_debit`, the same one the bank balance uses) runs on the whole SMS before the merchant keywords, so a bill "towards Amazon Pay ICICI Credit Card" isn't Shopping, and again after a Gemini full parse, whatever Gemini guessed. Card swipes never name a bank account and keep their merchant's category. The card's own "payment received" SMS is also Credit Card Payment; that rule is for credits only, because a debit saying "payment … card" can be a card autopay for a purchase.

**Self Transfer** is money moved between your own accounts. Nothing assigns it automatically except Gemini when the SMS itself says so ("to self", "own account"); you pick it by editing a transaction. **Transfer** is still a payment to or from another person.

**Salary** is only monthly pay ("salary", "sal", "payroll" in a credit SMS); other money in (interest, dividends, anything else) is **Income**. The dashboard counts salary paid on the 16th or later toward the next month. A debit that mentions interest is never income.

When the regex parser can't read an SMS and Gemini parses all of it, the merged name and a matching rule's category still apply (a rule's category replaces Gemini's; `parsed_by` stays `ai_agent`); otherwise your category for the merchant does. An SMS nobody could read (Needs review) gets neither.

`parsed_by` records where the category came from: `regex`, `regex+rule` (one of your category rules), `regex+user`, `regex+history`, `regex+ai`, `ai_agent`, `manual`, or `edited` (an SMS that needed review, completed by hand).

## What counts as spending

`spending.py` holds the one rule; the app's totals, the report and the spend limits all follow it.

| Set | Categories | Effect |
| --- | --- | --- |
| `NOT_COUNTED` | Credit Card Payment, Self Transfer | Neither spending nor income: the card spends already counted, and money between your own accounts isn't spent. |
| `INVESTED` | Investments & Finance | Neither; the app shows its debits as **Invested**. Investment credits (redemptions) count as nothing. |

Every other category counts, including Cash Withdrawal, Transfer, Uncategorized and no category: debits as spending, credits as income. Net flow is income − spending − invested. The app keeps a copy of both sets in `expense-tracker-client/lib/categories.ts`; `tests/test_spending.py` compares them (and the category order) when both repos sit side by side. Past months' figures change once when this ships, because card bills, self transfers and investments stop counting retroactively.

## Possible duplicates

Banks sometimes send two differently worded SMS for one payment, and an SMS can arrive for a payment you already added by hand. `duplicates.py` compares each newly saved transaction (SMS or hand-added) with your transactions saved **before** it whose time is within 3 days, with the same amount, direction and currency (unreadable SMS are never compared). It's a possible duplicate when:

1. **Same reference number** (UPI ref / UTR): `reason = "reference"`.
2. **Same details** within 10 minutes: the same merchant, ignoring case and spacing; when either side has no merchant, the same account instead (`"same_details"`). Two different merchants never match, even on one account.
3. **Hand entry and SMS** within 24 hours: exactly one of the two was added by hand as **Bank account** (cash never matches, and two hand entries never match): `"manual_entry"`.

The lowest rule wins, then the closest time, then the most recently saved. The windows are the constants `LOOKBACK`, `SAME_DETAILS` and `MANUAL_ENTRY` in `duplicates.py`.

The newer transaction (saved later) is flagged in the same commit as it is saved (`duplicate_flags`, a new table `create_all` adds) and **held out** of every total while you decide: Spent, Income, Invested, every spend limit (no limit alert for it) and the bank balance. Server code filters with `spending.not_held_out()` / `spending.counted_filter()` in SQL and `spending.is_held_out()` for one transaction; the app uses `isHeldOut()` / `needsReview()`. It shows greyed under **Needs review**, and a bell notification says so ("Possible duplicate: ₹500.00 at Swiggy" / "Same reference number as the payment at 10:02 am"), linking to `/activity?kind=review`.

```http
POST /api/v1/transactions/{id}/not-duplicate   -> 200 the transaction; 404 not yours; 409 "Not a pending duplicate."
```

**Not a duplicate** (session + `Origin`) clears the flag: the transaction counts again, its limit check runs, and it's never flagged again (the dismissed row stays). A second tap gets 409. Deleting the flagged transaction removes its flag; deleting the **earlier** one releases the newer one, which counts again (its `updated_at` moves, so the app sees it on the next poll) and gets its limit check. Nothing is ever deleted automatically.

Detection only runs when a transaction is saved: past transactions aren't scanned, and editing one doesn't re-check it (a flag stays until you decide). Two SMS saved at the same moment can't flag each other, so a race can leave both counted, never both held out. Every transaction response carries `duplicate_of`: the earlier transaction's id while this one is held out, else `null`.

## Bank balance

Set your current bank balance in the dashboard's Settings. From that moment, every parsed debit SMS is subtracted and every credit added. Saving a new amount restarts the count from then.

- Credit-card alerts (`BLOCK CC`, `Avl Limit`, `Cardmember`, "credit card" without a bank account) are **not** deducted. A card spend leaves your account when you pay the card bill, and that payment arrives as its own bank debit SMS. Card bills paid from an account (`… debited from A/c … towards … Credit Card`) do count.
- Unparsed SMS (`is_parsed=false`) and other currencies are not counted.
- Transactions added by hand as **Cash** are not counted either (they never pass through the bank). Ones added as **Bank account** count like an SMS.
- Possible duplicates waiting for your decision are not counted (see Possible duplicates).
- The value lives in the `balance_settings` table, which `create_all` adds automatically on the next server start.

```http
GET    /api/v1/balance            -> summary, or null if not set
PUT    /api/v1/balance            {"amount": "125000.50"}   (optional "as_of": ISO time with timezone)
DELETE /api/v1/balance
```

All three require the dashboard session. `PUT`/`DELETE` also require the dashboard `Origin`. When a balance is set, `success` and `duplicate` webhook responses include `balance` (`current`, `spent`, `received`, `excluded_card`, …). In Shortcuts you can add **Get Dictionary Value** `balance` → `current` and **Show Notification** "Balance left: …" after each SMS.

## Transactions added by hand

The dashboard's Activity page has **Add** for cash and anything that never sent an SMS (UPI Lite, a missed alert).

```http
POST   /api/v1/transactions        {"type": "DEBIT", "amount": "249.50", "merchant": "Chai Point",
                                    "transaction_time": "2026-10-22T19:30:00+05:30",
                                    "category": null, "paid_with": "cash"}
PATCH  /api/v1/transactions/{id}   any of: type, amount, merchant, category, transaction_time,
                                    paid_with (hand-added entries only), notes (up to 500
                                    characters; "" removes it); only the fields sent change
DELETE /api/v1/transactions/{id}   -> 204; any of your transactions, SMS or hand-added
```

All three need the dashboard session and `Origin`. A delete is permanent; if the Shortcut sends the same SMS again it's recorded again. `PATCH` works on bank SMS too: a new category is remembered for the merchant (see Categories), and giving an SMS that needed review an amount makes it count. They're stored with `parsed_by = "manual"`; cash entries get `account_ref = "Cash"`. The name you type becomes its merged name, if you have one for it. `category: null` ("Auto") picks one with your category rules (never ones with SMS words: there's no SMS), then your category for the merchant and the same keyword rules as SMS; a category you pick is kept and remembered under the merged name. Times more than 5 minutes in the future are rejected. They count in totals and spend limits like any SMS, and an expense that crosses a limit raises the same notification. A **Bank account** entry and a bank SMS for the same amount within 24 hours, in either order, flag the later one as a possible duplicate (see Possible duplicates).

## Names and rules

**Settings → Names and rules** in the app. Two things you make, both applied when a payment is saved:

- **Merged names**: "use *Swiggy* for every payment named *SWIGGY*". Made from a payment's sheet (Edit, change the name, tick "Also rename …").
- **Category rules**: "if the SMS has *XX4821* and money came in, the category is Self Transfer".

```http
GET    /api/v1/merchant-names        -> [{id, from_name, to_name, created_at}], newest first
POST   /api/v1/merchant-names        {"from_name": "SWIGGY", "to_name": "Swiggy"}
                                     -> 201 {"name": {…}, "changed": 12}  (past payments renamed)
DELETE /api/v1/merchant-names/{id}   -> 204; past payments keep their new name

GET    /api/v1/rules                 -> [{id, merchant_words, sms_words, min_amount, direction, category, created_at}],
                                        newest first (the order they're tried in)
POST   /api/v1/rules/preview         {"sms_words": "XX4821", "direction": "CREDIT", "category": "Self Transfer",
                                      "merchant_words": null, "min_amount": null, "id": null}
                                     -> {"count": 6, "examples": [{id, merchant, amount, currency, transaction_time,
                                        category}]}  (up to 5, newest first; writes nothing)
POST   /api/v1/rules                 {…the same fields, "apply_to_past": false} -> 201 {"rule": {…}, "changed": 0}
PUT    /api/v1/rules/{id}            {…every field, "apply_to_past": false} -> {"rule": {…}, "changed": 0}
DELETE /api/v1/rules/{id}            -> 204; payments it categorised keep their category
```

All need the dashboard session; writes (the preview included) also need the dashboard `Origin`. Another user's or a missing id gives 404 ("Merged name not found.", "Rule not found."). Amounts are decimal strings (`"15000.00"`). Both live in `merchant_aliases` and `category_rules`, which `create_all` adds on the next start.

**Validation (422).** A merge's `from_name` needs a name: empty or "Unknown" gives "This payment has no name to rename." `to_name` is put in NFC, trimmed and its spaces collapsed, and needs a word, at most 100 characters and no invalid characters ("Enter a merchant name.", "Merchant names can be up to 100 characters.", "Merchant names can't contain invalid characters."). A rule's words are cleaned the same way and an empty one is null; it needs at least one of name words, SMS words or an amount ("Add at least one condition."); words with no letters or digits give "Enter words that appear in the name." / "Enter words that appear in the SMS."; "Words can't contain invalid characters."; "Use up to 255 characters."; an amount of 0 or less, over 9999999999.99 or with more than 2 decimals gives "Enter an amount above zero."; the category must be one of the app's categories other than Uncategorized ("Pick a category from the list."), stored in its usual spelling.

**Matching.** Name words and SMS words match as whole words, in order and next to each other, in **any case** (`merchants.matches_any_case`); anything that isn't a letter or digit splits words, so "rahul@okaxis" matches "to VPA rahul@okaxis on" and "XX4821" matches "A/c XX4821". A smallest amount matches INR payments only, inclusive. Money out or in must equal the payment's direction; none means either. Every condition set must hold. Rules are tried **newest first** (by creation time, then id) and the first match wins, so a narrower rule added after a broad one makes an exception. SMS words never match a payment added by hand (its "Added by hand in Folio" text isn't an SMS). Merchant limits stay case-sensitive.

**Merges** are for one exact name in any case, extra spaces ignored (`categorize.merchant_key`), looked up by a hash so no collation changes which name it's for. Saving one, in one commit: if the new name is itself merged, the merge ends at its final name; the merge is created (or its new name replaced); merges that pointed at this name point at the new one (merges never chain); your category for the name moves to the new name per direction, unless the new name already has one (that one wins); and every past payment of yours (Shortcut SMS included, hand-added ones too) with this name is renamed, unless it's exactly the new name already. A case-only merge ("SWIGGY" → "Swiggy") moves no memory. A "Swiggy" limit then counts the renamed payments.

**Apply to past** is opt-in, after a preview: a rule changes parsed SMS rows (not `manual` or `edited`, whose category you chose) for which it's the first match of your whole list, with it in its place (a rule being edited keeps its place), and whose category differs, so past payments get the answer a new SMS would. Rows the regex parser read also get `parsed_by = "regex+rule"`; Gemini's keep `ai_agent`. Rows are read once and matched in Python; the writes are at most two `UPDATE … WHERE id IN (…) AND updated_at <= :started` in one commit, so nothing is half applied and an edit made meanwhile wins. Merchant memory isn't written. A merge renames the same way.

**Limits.** After a merge or an apply that changed rows, one limit check runs in the background for the newest changed parsed debit; it alerts only for the current week or month, so old payments never do. The app reloads all transactions afterwards (the 5-second poll can't carry hundreds of changed rows).

**Deleting** a merge or a rule stops it for future payments only: names and categories already changed stay. Editing a payment never runs merges or rules again.

## Spend limits, notifications and (optional) email alerts

Set a weekly (Mon–Sun, IST) and/or monthly limit in **Settings → Spending limits**. When a debit SMS takes your spending to or past a limit, Folio creates one **in-app notification** for that week/month: a red dot on the bell on Home, opening a drawer, with each notification linking to Settings → Spending limits. That needs no setup. **Email is optional and off** until you add the Gmail keys below; with them, the same event also sends one email. "Spent" matches the app: parsed debits, card spends included, except Credit Card Payment, Self Transfer and Investments & Finance (see What counts as spending), so a card bill or an SIP never trips a limit. The email goes out in the background after the webhook responds. A failed send is retried on the next payments (up to 3 tries), and the error shows in Settings. Raising a limit re-arms its alert.

**Buttons on notifications:** every item in `GET /api/v1/notifications` has `actions` (`[{label, path}]`, each a same-origin `POST` under `/api/v1/` with no body) and `resolved` (text shown in place of the buttons once decided). Both are empty for spend-limit alerts. A feature adds buttons to its own kind with `notifications.register_actions(kind, resolver)`: the inbox calls the resolver once per request with that kind's listed notifications and gets back buttons or resolved text per id. A resolver that fails is logged and its notifications just show no buttons. Kinds with buttons have `link: null`.

**Clearing notifications:** `DELETE /api/v1/notifications/{id}` (swipe to delete) and `DELETE /api/v1/notifications` (Clear) hide notifications instead of deleting them (`notification_dismissals`, added by `create_all`). Each notification is also the record that its alert went out, so a cleared limit alert isn't sent again on the next payment, and the meter in Settings still says "notified".

**Per category:** in the same card, *Limit for* switches between **All spending** (the overall limit above) and a category, each with its own weekly and/or monthly amount. A category limit counts only debits in that category, and crossing it raises its own notification ("Weekly Food & Dining limit crossed"). It's checked when an SMS arrives, when you add or edit a transaction, and when the background AI retry fills a category in later. Money-in categories (Salary, Income, Refunds & Cashback), Uncategorized and the categories that aren't spending (Credit Card Payment, Self Transfer, Investments & Finance) can't have limits (422 "Pick a spending category, or All."). Limits saved on those before this release are hidden and never alert; `scripts/recategorise_card_payments.py` deletes them. With Gmail set up, a category alert also sends one email, best effort (not retried).

```http
GET /api/v1/limits   -> overall `weekly`/`monthly`, plus `categories`: [{category, weekly, monthly}]
                        and `merchants`: [{id, merchant, counts, weekly, monthly}]
PUT /api/v1/limits   {"weekly_limit": "5000", "monthly_limit": null}                            (overall)
PUT /api/v1/limits   {"category": "Food & Dining", "weekly_limit": "1500", "monthly_limit": null}
```

Leaving both amounts null for a category removes its limit. Category limits live in the `category_limits` table, which `create_all` adds on the next server start.

**Per merchant:** a limit on one merchant name, such as Swiggy, with the same weekly/monthly periods, meter and one-time notification ("Monthly Swiggy limit crossed"). It counts every parsed INR debit (yours, plus SMS from the Shortcut) whose category counts as spending and whose merchant name contains the limit's name as the **same whole words with the same case**; anything that isn't a letter, digit or combining mark (such as the vowel signs in "स्विगी") separates words. Names are compared in Unicode NFC, so "Café" typed with a separate accent is the same name. Matching lives in `merchants.py` and runs in Python, so no database collation changes the answer. A "Swiggy" limit counts "Swiggy" and "Swiggy Ltd", but not:

- "SWIGGY" or "swiggy ltd" (different case; an SMS read by Gemini keeps the bank's capitals),
- "Swiggyone" or "Amazonpayindiap" for Amazon (not a whole word),
- "Cult.fit" for a "Cult Fit" limit ("fit" isn't "Fit"), and "Kolar Stores" never counts toward Ola.

Each limit's `counts` lists the names it has counted, all time, most payments first, so you can see what's included. It's checked on the same events as a category limit (an SMS, adding or editing a transaction, the AI retry), after the overall and category checks and on its own, so a failure in it can't stop theirs. One payment can cross the overall, a category and a merchant limit at once; each notifies separately. With Gmail set up, a merchant alert also sends one email, best effort (not retried), like a category alert.

```http
PUT /api/v1/limits/merchants   {"merchant": "Swiggy", "weekly_limit": null, "monthly_limit": "3000", "category": null}
                               -> the full summary, like PUT /api/v1/limits
GET /api/v1/limits/merchants/known              -> [{"name": "Swiggy", "payments": 42}, …]
GET /api/v1/limits/merchants/preview?merchant=…  -> {"known": true, "counts": ["Swiggy", "Swiggy Ltd"]}
```

The name is put in NFC, trimmed and its spaces collapsed; it needs at least one word and up to 255 characters, and can't contain control characters (such as NUL) or broken Unicode (a lone surrogate, or U+FFFD): 422 "Merchant names can't contain invalid characters." Saving:

1. A limit with exactly this name exists: its amounts change, or both null removes it. `category` is ignored.
2. Otherwise, both amounts null does nothing.
3. A **known** name (one you've paid, or typed before, with the same case once both are cleaned, so a payment stored as "Amazon  Pay" makes "Amazon Pay" known) gets the limit; `category` is ignored.
4. A new name needs `category`, a spending category (422 "Pick a category for this new merchant." without it). The name is saved as a known merchant exactly as typed, and the category goes into merchant memory, unless memory already has one for that name in any case (a typed "swiggy" never changes what "Swiggy" gets).
5. All in one commit; two simultaneous saves end with one limit.

Only spending counts here too, so a merchant you've paid only in Investments & Finance (or Credit Card Payment, Self Transfer) isn't known and a limit on it shows ₹0.

Known merchants are every name you've paid in INR ("Swiggy" and "SWIGGY" are two) plus the typed ones, cleaned (names that are the same once cleaned are one entry, their payments added), most payments first. The preview also reports a name with a limit as known. It never errors: an empty name, one with no words, one over 255 characters or one with invalid characters gives `{"known": false, "counts": []}`. Merchant limits live in `merchant_limits` and typed merchants in `known_merchants`, both added by `create_all`; a typed merchant stays known after its limit is deleted. Each table is unique on a SHA-256 of the exact name (`merchant_hash`, `name_hash`) rather than the name, because MySQL's default collation would treat "Swiggy" and "SWIGGY" as one. The production collation of `transactions.merchant` (`SHOW FULL COLUMNS FROM transactions LIKE 'merchant'`) is not recorded here yet; nothing depends on it.

Round trips to the database (about 277 ms each to TiDB from India):

| Path | Round trips |
| --- | --- |
| `GET /api/v1/limits`, no merchant limits | 3: limits, merchant limits, every meter in one query |
| `GET /api/v1/limits`, with merchant limits | 4: plus one read of your INR debits, matched in Python |
| Alert check after a debit | as before, +1 to load merchant limits, +1 more when one matches |
| `PUT /api/v1/limits/merchants` | about 8: the existing limit, the known check, the memory check, the writes, then the summary |
| SMS webhook | +1 for your merged name and category rules (one statement); a rule that sets the category also saves the Gemini call |
| `POST /api/v1/transactions` (adding by hand) | +1 for your merged name and category rules (one statement) |

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
GET  /api/v1/notifications       -> {"unread": 1, "items": [{id, kind, title, body, link, created_at, read, actions, resolved}]}
POST /api/v1/notifications/read  {"ids": ["…"]}  (omit ids to mark all read)
GET  /api/v1/limits              -> this week/month spent, limits, notified flag, email status
PUT  /api/v1/limits              {"weekly_limit": "5000", "monthly_limit": null}   (null = no limit)
POST /api/v1/limits/test-email
```

## One-time fix after this release

Card-bill debits saved before the card-bill check may sit in Shopping, Transfer or Uncategorized, and category limits may exist on categories that no longer count. `scripts/recategorise_card_payments.py` lists and fixes both:

```sh
uv run python scripts/recategorise_card_payments.py                    # dry run: lists what would change
uv run python scripts/recategorise_card_payments.py --apply            # changes every listed row
uv run python scripts/recategorise_card_payments.py --apply --ids a,b  # changes only these listed rows
uv run python scripts/recategorise_card_payments.py --email you@example.com   # default: DASHBOARD_OWNER_EMAIL
```

The dry run prints three lists: card-bill debits to change to Credit Card Payment, Credit Card Payment debits that don't look like a bank-side bill (information only; fix any by hand), and the category limits it would delete. Review the list, then `--apply` (or `--apply --ids` with the ids you agree with; any id not in the list stops it with nothing changed). Limits are deleted on any `--apply`. On a database that isn't on this machine it asks you to type "yes" first. It's idempotent. Run it once on production after this release is deployed: dry run, review, then `--apply`.

## Webhook review

Implemented:

- Authentication on transaction/dashboard reads; explicit frontend CORS origin.
- Fixed truncation of four-plus-digit amounts and added Indian comma grouping for amounts/balances.
- Atomic duplicate-insert recovery for concurrent retries using the existing unique hash constraint.
- Removed raw SMS logging; bounded input lengths and timezone validation.
- Ignored obvious OTP, failed/declined payment, payment-request and offer messages, and notices of money that hasn't moved yet: auto-debit / AutoPay pre-debit alerts ("is scheduled on", "will be debited", "due on/by") and card statements or bill reminders ("total/minimum due", "statement … generated"). A notice that also says the money moved ("debited via UPI AutoPay") is still recorded.
- Retained successful regex parsing when merchant is missing, avoiding unnecessary AI requests.
- Asynchronous Gemini calls with a 20-second timeout, validated fields and safe fallback after parser errors.
- Excluded unparsed transactions from analytics and omitted raw SMS from the dashboard response.

Remaining considerations:

- Configure request-body/rate limits at the public reverse proxy, use stable HTTPS, and introduce migrations when models change (`create_all` does not migrate existing columns).
- `GET /api/v1/dashboard` was removed (nothing called it; the app computes its totals from `/api/v1/transactions`). Queue ingestion if volume increases.
- Hash deduplication treats identical normalized SMS text as the same event. Distinct identical messages require a bank/event identifier to distinguish them. Differently worded SMS of one payment are saved, then flagged as possible duplicates and held out of totals (see Possible duplicates).
- Parsing order: regex (tuned for HDFC/ICICI account, card and UPI alerts; the earliest debit/credit keyword decides direction, and balance/limit/due amounts are never used as the transaction amount) → Gemini full parse if regex cannot find a type and amount → your merged names and category rules (`parsed_by=regex+rule`) → your category for the merchant, earlier payments → Gemini category-only call if regex parsed it but no keyword rule matched (`parsed_by=regex+ai`) → `Uncategorized`. Categories are listed in `parser.CATEGORIES`.
- Bank formats vary. The parser uses the first currency-labelled amount that is not a balance/limit; it does not extract embedded SMS dates. `received_at` (or receipt time) drives the dashboard date. Verify your bank's formats.
- On regex failure, the existing Gemini integration sends the SMS to Google. Remove `GEMINI_API_KEY` to preserve unmatched messages locally for review instead. Set `GEMINI_MODEL` to one available to your account; the original model default is retained. Editing review records is outside this read-only dashboard.

## Checks

```powershell
uv run python -m unittest discover -s tests -v
uv run python check_database.py
```

Tests use an isolated in-memory SQLite database. `check_database.py` makes a read-only count query against your configured database. Provider responses are mocked in OAuth tests; real Google login requires your credentials.
