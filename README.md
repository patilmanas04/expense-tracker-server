# Folio · Expense Tracker API

FastAPI backend for an iPhone bank-SMS webhook and a private Next.js dashboard. This is a **single-owner application**: only `DASHBOARD_OWNER_EMAIL` may sign in. The owner can see their own and existing unassigned transactions; other users' assigned rows are excluded. No real data is seeded or modified by tests.

## Run

Keep your existing `.env` values. Add the new keys from `.env.example` without overwriting your database or webhook credentials.

```powershell
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

`JOBS_SECRET` is optional: it only turns on the external trigger for the daily jobs (see Daily jobs and the SMS watchdog); the daily check runs inside the server without it.

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

Create a Message automation for your bank sender, then **Get Contents of URL**, POST, JSON body. Put the message variable in `message`, not the literal text "Shortcut Input". Set the secret header. `sender` and `received_at` are optional; any supplied date must be ISO 8601 with a timezone. Manual Play testing needs a Text action containing sample SMS text. `Authorization: Bearer ...` remains supported. The automation must forward **every** SMS from the bank sender, with no keyword filter: auto-debit notices and card bills (see Upcoming payments) don't contain "debited" or "credited", so a filtered automation never sends them.

`success`, `duplicate`, and `ignored` responses use HTTP 200. When the SMS looks like a second record of an earlier transaction (see Possible duplicates), the status is still `success` and the message reads "Transaction logged successfully via regex (possible duplicate: held out of totals)", so the Shortcut can show why Balance left didn't move. Unknown formats are preserved with `is_parsed=false`, marked for review and excluded from totals. Analytics separate currencies, calculate amounts using Decimal, and use Asia/Kolkata dates. Net cash flow is income minus expenses for the selected period, not an account balance. Table filters do not alter period charts/totals. There is no export endpoint: the app's Activity screen exports the transactions it shows as a CSV file.

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

**Self Transfer** is money moved between your own accounts. Gemini sets it when the SMS itself says so ("to self", "own account"), and Folio **suggests** it for a likely transfer between your own accounts (see Self-transfer suggestions); a suggestion sets it only when you tap **Confirm**. You can also pick it by editing a transaction. **Transfer** is still a payment to or from another person.

**Salary** is only monthly pay ("salary", "sal", "payroll" in a credit SMS); other money in (interest, dividends, anything else) is **Income**. The dashboard counts salary paid on the 16th or later toward the next month, unless 'Month starts on payday' is on, when months start on salary day (see Pay periods). A debit that mentions interest is never income.

When the regex parser can't read an SMS and Gemini parses all of it, the merged name and a matching rule's category still apply (a rule's category replaces Gemini's; `parsed_by` stays `ai_agent`); otherwise your category for the merchant does. An SMS nobody could read (Needs review) gets neither.

### Your own categories

`categories.py` adds categories of your own ("Pets", "Gym") next to the built-in list, in `custom_categories` (added by `create_all`; unique per user on the case-folded name, `uq_custom_category`). Up to 30.

```http
GET    /api/v1/categories                         -> {"built_in": [parser.CATEGORIES, Uncategorized included], "custom": [CustomCategoryOut, oldest first]}
POST   /api/v1/categories   {"name": "Pets", "icon": "PawPrint", "colour": "orange"}  -> 201 CustomCategoryOut
PUT    /api/v1/categories/{id}   {"icon": "Dumbbell", "colour": "teal"}              -> 200 CustomCategoryOut (the name never changes)
DELETE /api/v1/categories/{id}?move_to=Shopping                                     -> 200 {"moved": 3}
```

`CustomCategoryOut` = `{"id", "name", "icon", "colour"}`. Icons: PawPrint, Dumbbell, Gift, Baby, House, Plane, Coffee, Shirt, Wrench, BookOpen, Tag, Sparkles; colours: violet, blue, green, orange, pink, amber, teal, red, slate, gray (the app's `CUSTOM_ICONS` / `PALETTE` in `lib/categories.ts`; `tests/test_custom_categories.py` compares them). Session on every route, `Origin` on writes.

- **Names:** spaces collapsed, 1–40 characters of letters, digits, spaces and `& - ' . , ( ) /`, with at least one letter or digit (no ":", which notification keys use as a separator). 422 "Enter a name." / "Names can be up to 40 characters." / "Use letters, digits, spaces and & - ' . , ( ) /." / "Pick an icon from the list." / "Pick a colour from the list.".
- **Reserved, in any case** (409 "A category with this name already exists."): every built-in (Uncategorized and Self Transfer included), "All" (the overall limit), "Other" (the app's Report bucket), and your existing names. 409 "You can have up to 30 categories." at 30.
- **Using one:** a custom category counts like Transfer (debits as spending, credits as income). Adding or editing a transaction, category rules and spend limits (category and rollover, and a new merchant's category) accept it in any case and store its own spelling; one handler (`categories.canonical`) checks names against the built-ins plus yours. An unknown name is a 422 with a plain-text `detail`: "Unknown category." (transactions), "Pick a category from the list." (rules), "Pick a spending category, or All." (`PUT /limits`, `/limits/rollover`), "Pick a spending category." (`/limits/merchants`). Merchant memory remembers it, so the next SMS from that merchant gets it (`regex+user`). Category limits on your own categories are listed after the built-ins, by name.
- **Not in Gemini:** the parser's keyword rules and Gemini prompts only know the built-ins, so a custom category reaches a payment only through you: an edit, memory, a rule.
- **Delete with move:** its payments move to `move_to` (default Uncategorized; any built-in or another of yours, not itself, else 422 "Pick another category to move its payments to."), with `updated_at` moved so the app's poll sees them; its merchant memory moves too, or is forgotten for Uncategorized; its limit, rollover and rules are deleted (a moved `regex+rule` payment becomes `regex+user`, as its rule is gone). One commit. 404 "Category not found." for an id that isn't yours.

`parsed_by` records where the category came from: `regex`, `regex+rule` (one of your category rules), `regex+user`, `regex+history`, `regex+ai`, `ai_agent`, `manual`, or `edited` (an SMS that needed review, completed by hand). Picking another category by hand for a `regex+rule` payment makes it `regex+user`, so the app stops saying a rule set it (so do Confirm on a self-transfer suggestion and deleting a custom category with its rules). The rule still decides later SMS like it; the edit sheet asks `GET /api/v1/rules/match/{id}` to say so.

## What counts as spending

`spending.py` holds the one rule; the app's totals, the report and the spend limits all follow it.

| Set | Categories | Effect |
| --- | --- | --- |
| `NOT_COUNTED` | Credit Card Payment, Self Transfer | Neither spending nor income: the card spends already counted, and money between your own accounts isn't spent. |
| `INVESTED` | Investments & Finance | Neither; the app shows its debits as **Invested**. Investment credits (redemptions) count as nothing. |

Every other category counts, including Cash Withdrawal, Transfer, Uncategorized and no category: debits as spending, credits as income. Net flow is income − spending − invested. The app keeps a copy of both sets in `expense-tracker-client/lib/categories.ts`; `tests/test_spending.py` compares them (and the category order) when both repos sit side by side. Past months' figures change once when this ships, because card bills, self transfers and investments stop counting retroactively.

**Payments minus lend & borrow entries.** A transaction counts its **counted amount**: its amount minus its linked lend & borrow entries (see Lend & borrow). Splitting a ₹1,800 dinner with two friends at ₹600 each counts ₹600 as spending; a ₹600 repayment from a friend isn't income, and a ₹1,000 credit of which ₹600 was a repayment counts ₹400. Every total, limit meter, alert and merchant limit uses it (`spending.linked_totals()` / `counted_amount()` in SQL; `linked_amount` on every transaction for the app's `t.counted`). The category doesn't change.

## Possible duplicates

Banks sometimes send two differently worded SMS for one payment, and an SMS can arrive for a payment you already added by hand. `duplicates.py` compares each newly saved transaction (SMS or hand-added) with your transactions saved **before** it whose time is within 3 days, with the same amount, direction and currency (unreadable SMS are never compared). It's a possible duplicate when:

1. **Same reference number** (UPI ref / UTR): `reason = "reference"`.
2. **Same details** within 10 minutes: the same merchant, ignoring case and spacing; when either side has no merchant, the same account instead (`"same_details"`). Two different merchants never match, even on one account.
3. **Hand entry and SMS** within 24 hours: exactly one of the two was added by hand as **Bank account** (cash never matches, and two hand entries never match): `"manual_entry"`.

The lowest rule wins, then the closest time, then the most recently saved. The windows are the constants `LOOKBACK`, `SAME_DETAILS` and `MANUAL_ENTRY` in `duplicates.py`.

The newer transaction (saved later) is flagged in the same commit as it is saved (`duplicate_flags`, a new table `create_all` adds) and **held out** of every total while you decide: Spent, Income, Invested, every spend limit (no limit or payment alert for it) and the bank balance. Server code filters with `spending.not_held_out()` / `spending.counted_filter()` in SQL and `spending.is_held_out()` for one transaction; the app uses `isHeldOut()` / `needsReview()`. It shows greyed under **Needs review**, and a bell notification says so ("Possible duplicate: ₹500.00 at Swiggy" / "Same reference number as the payment at 10:02 am"), linking to `/activity?kind=review`.

```http
POST /api/v1/transactions/{id}/not-duplicate   -> 200 the transaction; 404 not yours; 409 "Not a pending duplicate."
```

**Not a duplicate** (session + `Origin`) clears the flag: the transaction counts again, its after-save checks run (limits and payment alerts, cause `restored`), and it's never flagged again (the dismissed row stays). A second tap gets 409. Deleting the flagged transaction removes its flag; deleting the **earlier** one releases the newer one, which counts again (its `updated_at` moves, so the app sees it on the next poll) and gets its after-save checks. Nothing is ever deleted automatically.

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
GET    /api/v1/balance/breakdown  -> breakdown, or 404 "No balance set."
GET    /api/v1/balance/safe-to-spend  -> safe to spend, or 404 "No balance set."
PUT    /api/v1/balance            {"amount": "125000.50"}   (optional "as_of": ISO time with timezone)
DELETE /api/v1/balance
```

All five require the dashboard session. `PUT`/`DELETE` also require the dashboard `Origin`. When a balance is set, `success` and `duplicate` webhook responses include `balance` (`current`, `spent`, `received`, `excluded_card`, …). In Shortcuts you can add **Get Dictionary Value** `balance` → `current` and **Show Notification** "Balance left: …" after each SMS.

The breakdown (the app's **How it's worked out**) lists every transaction of yours (or unassigned) recorded after `as_of`, in any parse state and currency. `balance.classify` gives each one at most one reason it was skipped, the first that matches in this fixed order: `unreadable` (needs review), `other_currency`, `duplicate` (held out), `cash`, `credit_card`; a row with no reason is counted. The response has `currency`, `start` (`amount`, `as_of`), `counted` (oldest first: `{id, effect, running}`, the signed effect and the balance right after it), `skipped` (all five groups, each `{count, total, ids}`, ids oldest first; `total` is the signed net the rows would have had, `"0.00"` when empty, and always `null` for `unreadable` and `other_currency`) and `current`, which equals the summary's `current`. Order is `transaction_time`, then `created_at`, then `id`. Amounts are decimal strings with two places (`"-1249.00"`). `PUT`'s `as_of` is the app's **Read at**: the moment you read the balance in your bank's app, so payments between then and saving still count. It can be any past time (at most 5 minutes ahead); the app limits it to the last 7 days, the server doesn't.

**Safe to spend** (`safe_to_spend.py`, the app's Settings → Bank balance row) is Balance left minus what is still due to leave the bank before the next payday: `safe = balance − Σ subtracted`, worked out on every read (nothing stored, no new table).

- **Next payday** reuses the SMS watchdog's rule: with at least 2 salary days (`pay_periods.salary_days`, category Salary) in the last 120 days (`watchdog.SALARY_MIN`, `watchdog.SALARY_WINDOW`), it's the usual payday after the newest salary (`pay_periods.expected_after(days, usual_offset(days))`). `payday_state` is `upcoming` when that date is after today, `today` when it is today and `late` when it has passed without a salary. Otherwise it's `month_end` and the next payday is the 1st of next month.
- **Which due dates count** (`until`, inclusive, not returned): `upcoming` → the day before payday; `today` and `late` → today; `month_end` → the last day of this month.
- **Items** are every Upcoming payments item up to `until` (`upcoming.list_upcoming`: notices not dismissed due from 7 days ago, expected payments of recurring series, late ones included), in its order. Each is checked in this order: one that doesn't leave the bank (`leaves_bank` false: a card-charged auto-debit, a series paid by card or in cash) subtracts 0 with note `card_or_cash`; a card bill subtracts what is still owed, `amount − paid_amount`, or 0 with note `paid` when nothing is left (its status is ignored: "paid" there only means the minimum was reached); otherwise `upcoming` and `late` items subtract their amount and `paid` / `not_seen` ones subtract 0 with their status as the note. The app lists an item under **Not subtracted** exactly when `note` is set.
- **Per day:** `per_day = floor(safe / days)` in whole rupees, only when `safe > 0` and the state is `upcoming` or `month_end`; otherwise `null`. `safe` may be negative (short).
- **Response:** `currency` (the balance's), `balance` (Balance left), `next_payday` (`YYYY-MM-DD`, IST), `payday_state`, `days` (days from today to payday for `upcoming` and `month_end`, at least 1; `null` for `today` and `late`), `due` (each `{date, payee, kind, status, amount, min_amount, paid_amount, subtracted, note}`), `safe`, `per_day`. Amounts are decimal strings with two places, never `"-0.00"`. Read-only, so no `Origin` check; 404 "No balance set." without a saved balance.
- **Cost:** 6–8 statements (the balance setting, the balance sum, salary days, then Upcoming payments' 3–5), only while the app's Settings is open.

Tests: `tests/test_safe_to_spend.py` (the payday rule and its states, the item rules, CUJs, boundaries, statement counts and the API).

Every transaction response carries `balance_kind`: `null` when the SMS has no balance, else `"bank"` when its "Avl Bal" is a bank account's and `"card"` when it is a credit card's limit left. It uses the same test as Balance left, so a card bill paid from the bank reads `"bank"`.

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
GET    /api/v1/rules/match/{transaction_id}
                                     -> {"rule": {…} | null}  (the first of your rules matching that payment's name,
                                        SMS, amount and direction: it decides later SMS like it, even after you
                                        pick another category by hand; 404 "Transaction not found."; writes nothing)
```

All need the dashboard session; writes (the preview included) also need the dashboard `Origin`. Another user's or a missing id gives 404 ("Merged name not found.", "Rule not found."). Amounts are decimal strings (`"15000.00"`). Both live in `merchant_aliases` and `category_rules`, which `create_all` adds on the next start.

**Validation (422).** A merge's `from_name` needs a name: empty or "Unknown" gives "This payment has no name to rename." `to_name` is put in NFC, trimmed and its spaces collapsed, and needs a word, at most 100 characters and no invalid characters ("Enter a merchant name.", "Merchant names can be up to 100 characters.", "Merchant names can't contain invalid characters."). A rule's words are cleaned the same way and an empty one is null; it needs at least one of name words, SMS words or an amount ("Add at least one condition."); words with no letters or digits give "Enter words that appear in the name." / "Enter words that appear in the SMS."; "Words can't contain invalid characters."; "Use up to 255 characters."; an amount of 0 or less, over 9999999999.99 or with more than 2 decimals gives "Enter an amount above zero."; the category must be one of the app's categories other than Uncategorized ("Pick a category from the list."), stored in its usual spelling.

**Matching.** Name words and SMS words match as whole words, in order and next to each other, in **any case** (`merchants.matches_any_case`); anything that isn't a letter or digit splits words, so "rahul@okaxis" matches "to VPA rahul@okaxis on" and "XX4821" matches "A/c XX4821". A smallest amount matches INR payments only, inclusive. Money out or in must equal the payment's direction; none means either. Every condition set must hold. Rules are tried **newest first** (by creation time, then id) and the first match wins, so a narrower rule added after a broad one makes an exception. SMS words never match a payment added by hand (its "Added by hand in Folio" text isn't an SMS). Merchant limits stay case-sensitive.

**Merges** are for one exact name in any case, extra spaces ignored (`categorize.merchant_key`), looked up by a hash so no collation changes which name it's for. Saving one, in one commit: if the new name is itself merged, the merge ends at its final name; the merge is created (or its new name replaced); merges that pointed at this name point at the new one (merges never chain); your category for the name moves to the new name per direction, unless the new name already has one (that one wins); and every past payment of yours (Shortcut SMS included, hand-added ones too) with this name is renamed, unless it's exactly the new name already. A case-only merge ("SWIGGY" → "Swiggy") moves no memory. A "Swiggy" limit then counts the renamed payments.

**Apply to past** is opt-in, after a preview: a rule changes parsed SMS rows (not `manual` or `edited`, whose category you chose) for which it's the first match of your whole list, with it in its place (a rule being edited keeps its place), and whose category differs, so past payments get the answer a new SMS would. Rows the regex parser read also get `parsed_by = "regex+rule"`; Gemini's keep `ai_agent`. Rows are read once and matched in Python; the writes are at most two `UPDATE … WHERE id IN (…) AND updated_at <= :started` in one commit, so nothing is half applied and an edit made meanwhile wins. Merchant memory isn't written. A merge renames the same way.

**Limits.** After a merge or an apply that changed rows, one limit check runs in the background for the newest changed parsed debit; it alerts only for the current week or month, so old payments never do. The app reloads all transactions afterwards (the 5-second poll can't carry hundreds of changed rows).

**Deleting** a merge or a rule stops it for future payments only: names and categories already changed stay. Editing a payment never runs merges or rules again.

## Settings

**Settings → Alerts** in the app. Every setting is one row in `settings_store.KEYS` (its default and its validation), stored as JSON text in `user_settings` (one row per setting, added by `create_all`), so a new setting needs no new column.

```http
GET /api/v1/settings   -> {"alerts.large_amount": null, "alerts.unusual": true, "limits.warn_percent": 80, "alerts.low_balance": null, "periods.payday": false, "watchdog.enabled": true, "watchdog.quiet_days": 4, "watchdog.salary_check": true, "watchdog.email": true}   (every key, always in this order)
PUT /api/v1/settings   {"alerts.large_amount": 5000}   -> the full object after saving
PUT /api/v1/settings   {"limits.warn_percent": null}   -> warnings Off
PUT /api/v1/settings   {"alerts.low_balance": 10000}   -> alert when Balance left drops below ₹10,000
PUT /api/v1/settings   {"periods.payday": true}        -> months start on payday (see Pay periods)
PUT /api/v1/settings   {"watchdog.quiet_days": 6}      -> warn after 6 days with no bank SMS (see Daily jobs and the SMS watchdog)
```

| Key | Value | Default |
| --- | --- | --- |
| `alerts.large_amount` | whole rupees, 1 to 1,00,00,000, or `null` (Off) | `null` (Off) |
| `alerts.unusual` | `true` / `false` | `true` |
| `limits.warn_percent` | the near-limit warning level: 50 to 95 in steps of 5, or `null` (Off) | `80` |
| `alerts.low_balance` | the low-balance level: whole rupees, 1 to 10,00,00,000, or `null` (Off); the app shows it under Settings → Bank balance | `null` (Off) |
| `periods.payday` | "Month starts on payday" (see Pay periods): `true` / `false` | `false` |
| `watchdog.enabled` | the daily SMS watchdog (see Daily jobs and the SMS watchdog): `true` / `false` | `true` |
| `watchdog.quiet_days` | days with no bank SMS before a warning: a whole number, 2 to 14 | `4` |
| `watchdog.salary_check` | warn when salary is more than 3 days past its usual date: `true` / `false` | `true` |
| `watchdog.email` | also email watchdog warnings (only when Gmail is configured): `true` / `false` | `true` |

Both need the dashboard session (401 "Please sign in…" without it); `PUT` also needs the dashboard `Origin` (403 "Invalid request origin."). `PUT` is partial: it changes only the keys given, and `{}` changes nothing. Validation is strict JSON typing: an amount must be a JSON integer (`5000.0` is taken as `5000`); `true`, `"5000"` and `5000.5` are rejected, and `alerts.unusual` takes only `true` or `false`. Every key is checked before anything is written, so a 422 writes nothing; it names the first bad key in the body's order:

- `{"detail": "Unknown setting: <key>"}` (the key cut to 64 characters),
- `{"detail": "alerts.large_amount: Enter a whole number of rupees from 1 to 1,00,00,000, or turn it off."}`,
- `{"detail": "alerts.unusual: Choose on or off."}`,
- `{"detail": "limits.warn_percent: Choose 50% to 95% in steps of 5, or Off."}` (45, 82, 100, 0, 50.5, `true`, `"80"`; `80.0` is taken as `80`),
- `{"detail": "alerts.low_balance: Enter a whole number of rupees from 1 to 10,00,00,000, or turn it off."}` (0, 100000001, `true`, `"5000"`, 5000.5; `5000.0` is taken as `5000`),
- `{"detail": "periods.payday: Choose on or off."}` (`"yes"`, `1`, `null`); the same "Choose on or off." for `watchdog.enabled`, `watchdog.salary_check` and `watchdog.email`,
- `{"detail": "watchdog.quiet_days: Choose 2 to 14 days."}` (1, 15, 0, 4.5, `true`, `"4"`, `null`; `5.0` is taken as `5`).

A body that isn't a JSON object gets FastAPI's own 422 (a list `detail`). A missing row means the default; a stored `null` is a value (for `alerts.large_amount`, Off). A stored value that no longer passes its key's rules is logged and read as the default. Two first saves of the same key at once end with one row (the loser's commit fails and it updates the winner's row). `KEY` is a reserved word in MySQL/TiDB: SQLAlchemy quotes it, and no raw SQL may use this table.

## Pay periods

**Settings → Appearance → Month starts on payday** (`periods.payday`, default off). Off, months are calendar months, as before. On, each "month" starts on the day your salary arrives, and Home, Report, Activity's month filter and the monthly limits (overall, category and merchant, their alerts and near-limit warnings) all follow it. Weekly limits stay Monday–Sunday. The rules live once on the server, in `pay_periods.py`; the app loads the result and keeps its own copy of the date maths in `expense-tracker-client/lib/periods.ts`.

- **Salary days**: the IST dates of parsed INR credits in category **Salary** (hand-added ones count; yours or with no owner), not possible duplicates waiting for your decision.
- **Name**: a salary day pays for its own month on the 1st–15th and for the next month on the 16th or later (the dashboard's income rule). 31 Oct → "Nov"; 2 Nov → "Nov"; 16 Jan → "Feb".
- **Start**: for each name, the earliest salary day with that name starts its period; later Salary credits with the same name (a bonus, a split salary) start nothing. When the previous name has no period and that day is on or after the 1st of the named month, the period starts on the 1st (a first salary ever on 5 Nov starts "Nov" on 1 Nov).
- **End**: the day before the next period starts, and no later than its latest end, the 15th of the month after the name ("Nov" ends by 15 Dec).
- **Days outside every period** (before the first salary, after a latest end) count in their calendar month, cut to those days (16–31 Dec after "Nov" reached 15 Dec).
- **The open period** (the newest, while today is in it) has an expected last day: the day before the same date one month after its start (31 Oct → 29 Nov; 31 Jan → 27 Feb), never before today and never after its latest end. It's used for the limit windows, the dates in alerts and "days left" only, never to decide which period a payment belongs to. Nothing is predicted: a late salary just extends the current period ("Nov · 31 Oct – today" on 2 Dec), and the next period starts the day it arrives.

```http
GET /api/v1/periods   -> {"enabled": true,
                          "periods": [{"month": "2026-10", "start": "2026-09-30", "end": "2026-10-30"},
                                      {"month": "2026-11", "start": "2026-10-31", "end": "2026-12-15"}]}
```

Every period back to the first salary, oldest first; `end` is inclusive, and the newest period's is its latest end (the app works out the expected last day). Off: `{"enabled": false, "periods": []}`; on with no salary found: `{"enabled": true, "periods": []}` (months stay 1st–end). It needs the dashboard session (401 without it); it's read-only, so no `Origin` check.

Monthly limit windows use the pay period for the current day when the setting is on (`limits.current_windows`); alert keys, `SpendAlert` rows and wording are unchanged and built from the window's first day (`spend_limit:monthly:2026-10-31:10000.00`, "this month (Sat 31 Oct – Sun 29 Nov)"). A period's first day only changes if you edit or delete a Salary credit, and past alerts aren't recomputed then. Cost: reading periods is 1 statement with the setting off (the setting) and 2 with it on (plus the salary days), once per entry point (`GET /api/v1/limits`, the merchant preview, the after-save check); the after-save check already has your settings, so it adds 0 off and 1 on.

Tests: `tests/test_pay_periods.py` (the rules, windows, salary days, loading and the API), `tests/test_alerts.py` (`PaydayLimitTests`: overall and category alerts, a late salary, the summary), `tests/test_limit_warnings.py` (`PaydayWarningTests`) and `tests/test_merchant_limits.py` (a merchant limit in pay mode).

## After a payment is saved

One background task, `post_save.run(transaction_id, cause)`, runs every check that follows a saved, edited or re-categorised payment, after the response is sent. It resolves the owner, loads the transaction and your settings once (4 queries), then runs each registered check whose causes include this one, in the order they're registered (one block in `main.py`). Each check runs on its own: one failing is logged and the others still run; nothing it does can fail the save.

| Cause | When |
| --- | --- |
| `sms` | the webhook saved an SMS |
| `manual` | you added a payment by hand |
| `edit` | you edited a parsed debit and changed more than its note |
| `ai_retry` | Gemini filled the category in later |
| `restored` | "Not a duplicate", or deleting the original of a held-out row made it count again |

| Check | Causes |
| --- | --- |
| `limits` (overall, category and merchant limits) | all five |
| `payment_alerts` (large or unusual payment) | `sms`, `manual`, `ai_retry`, `restored` (not `edit`: an edit is your own change) |
| `low_balance` (Balance left dropped below your level) | `sms`, `manual`, `restored` (only "Not a duplicate"; a row released by deleting its original doesn't alert) |
| `recurring` (a new recurring payment, or its price changed) | all five (only the newest payment of a series notifies, so an edit to an older one or a late SMS doesn't) |
| `refunds` (is this credit the refund you're expecting? See Refund watch) | `sms`, `manual`, `restored` (a held-out credit is suggested once you mark it "Not a duplicate") |
| `transfers` (a likely transfer between your own accounts? See Self-transfer suggestions) | `sms` only (new SMS; hand-added rows, edits and restored rows never pair) |

A possible duplicate waiting for your decision counts nowhere, so it gets no checks until it's restored; other users' rows are skipped too. Merges and rule applies (Names and rules) still run the limit check alone (`limits.check_spend_limits`).

Cost per saved SMS once every planned check ships, one check at a time and after the reply: the runner's 4 queries, limits 6–8 (one more with 'Month starts on payday' on), payment alerts 1, low balance 2, recurring about 5, refunds 1–2 for credits, transfers 1–2 for parsed SMS: about 20 round trips, 4–6 s on TiDB, which nothing waits for. A restart drops queued tasks.

## Payment alerts

Two switches in **Settings → Alerts**, each giving an in-app notification (no email):

- **Payments over** (`alerts.large_amount`, Off by default): a payment over the amount ("Large payment"). Equal is not over.
- **Unusual for the merchant** (`alerts.unusual`, On by default): a payment at least **3×** what you usually spend at that merchant and at least **₹500** ("Unusual payment"). "Usually" is the median of your earlier payments there in the last **180 days**, needing at least **3** of them: parsed INR debits that count as spending (What counts as spending), not held-out duplicates, yours or unassigned SMS rows, before this payment's time. Merchants are matched by `categorize.merchant_key`, so "Swiggy", "SWIGGY" and " swiggy " share a history, but "Swiggy Instamart" doesn't. Amounts are whole payments.

The four numbers are first guesses, kept as constants in `payment_alerts.py` (`UNUSUAL_FACTOR`, `UNUSUAL_FLOOR`, `HISTORY_DAYS`, `MIN_HISTORY`).

One notification per payment (`kind = "payment_alert"`, dedupe key `payment_alert:<id>`); when both match it's one "Large and unusual payment". It links to `/activity?txn=<id>`, which opens the payment in Activity. Bodies use the money format of every other notification:

- "₹8,000.00 at Croma"; with no merchant name (none, blank or "Unknown"), "₹8,000.00 payment";
- unusual (alone or with large): "₹3,200.00 at Swiggy — about 9× what you usually spend there (₹350.00)" (the multiple rounded half up).

Skipped: credits, other currencies, payments that need review, categories that aren't spending (Credit Card Payment, Self Transfer, Investments & Finance), and possible duplicates until you decide (after "Not a duplicate" the payment is checked then). Edits never alert. The check reads at most one query (the merchant's history), and none when the unusual rule is off, the payment is under ₹500 or has no merchant name.

## Low-balance alert

**Alert me below** in **Settings → Bank balance** (`alerts.low_balance`, whole rupees, Off by default): one in-app notification when a payment takes Balance left (Bank balance; HDFC and ICICI together) from at or above the level to below it. `low_balance.py` keeps no state. After each saved payment it works out `current` (Balance left now, `balance.summarize`) and `before = current + amount`, and alerts when `before ≥ level > current`:

- **Once per dip.** Later payments while the balance stays below start below the level, so they can't cross it. Landing exactly on the level is not below it.
- **Re-arms by itself.** Once a credit or a newly saved balance lifts Balance left back to the level or above, the next payment that crosses it alerts again.
- **Never alerts** for credits, cash entries, credit-card spends (a card bill paid from the bank account does count), possible duplicates waiting for your decision, payments that need review, other currencies (and nothing at all when the saved balance isn't in INR), payments at or before the saved balance's `as_of` (they're already in it), edits, AI category retries, deletes, saving a balance or changing the level. "Not a duplicate" on a debit is checked like a new payment. A held-out row that counts again because you deleted its original isn't: the balance didn't move.
- **Known limit:** two SMS saved at the same moment are both checked after both are saved, so each sees the final balance. Only a payment whose own amount spans the level alerts, and if neither does, nothing alerts until the next payment.

One notification per payment (`kind = "low_balance"`, dedupe key `low_balance:<id>`), in-app only (never emailed), linking to `/settings/balance` (the breakdown):

- title "Balance low: ₹9,200.00 left" (the money format of every other notification; below zero, "Balance low: -₹500.00 left"),
- body "Below your ₹10,000 alert (HDFC and ICICI together)." (the level, whole rupees).

Queries: none when Off or for a credit, 1 without a saved balance, 2 normally (the saved balance, then the sum), 1 more for "Not a duplicate", plus the notification's own 2 when it alerts.

## Recurring payments

`recurring.py` finds payments that repeat every week, month or year (subscriptions, EMIs, rent, SIPs) in your own history. Nothing about a series is stored: every read works it out from the payments, so deletes, merges and "Not a duplicate" show at once.

- **What counts:** your parsed INR debits (unassigned SMS rows included; hand-added cash too), not Credit Card Payment or Self Transfer, not a possible duplicate waiting for your decision. Investments & Finance **does** count (SIPs recur), so this is not the "What counts as spending" rule. Payments are grouped by `categorize.merchant_key` (case and extra spaces ignored; no name or "Unknown" never groups) and dated by their IST day.
- **Window: the last 855 days** (2 × 380 + 95), not the design's 400. With 400 a yearly pair would only be found for about 35 days after each renewal; 855 keeps it listed through its renewal and its "late?" grace, and it drops off once its older payment leaves the window.
- **Cadence** comes from the newest gap: 5–9 days weekly, 26–35 monthly, 350–380 yearly. 10–18 and 52–70 days are one missed week or month. Any other gap, including 0 (two payments on one day), gives no series until the next payment.
- **The run** walks back from the newest payment while each gap fits the cadence, allowing one missed week or month per run (none for yearly). It **forms a series** when some 3 consecutive run payments (2 for yearly) are all within **±15%** of their median.
- **Period** = the median of the run's gaps (a missed gap counts as two halves), rounded down to whole days; **next expected** = last payment + period (a 28-day plan stays on 28 days).
- **Amount** is the newest payment's. **"Was"** (`previous_amount`) is the payment before it, only while the two differ by more than **both 5% and ₹10**.
- **Late** once today (IST) is after next expected + period ÷ 4 (whole days); **stopped** (not returned) once today is after last payment + 2 × period + period ÷ 4.

Your decisions live in `recurring_decisions` (added by `create_all`): one row per merchant key and cadence, unique on a SHA-256 of the key (`uq_recurring_decision`, like `merchant_limits`), with `status` `dismissed` or `cancelled` and `decided_at`. "Not recurring" (dismissed) moves the series to `hidden`. "Cancelled" holds while its last payment is on or before the decision's IST day; a later payment makes it active again (the row stays). A decision with no current series is ignored.

```http
GET  /api/v1/recurring                   -> {"currency": "INR", "monthly_total": "24067.00", "items": [...], "hidden": [...]}
PUT  /api/v1/recurring/decisions         {"merchant_key": "netflix", "cadence": "monthly", "status": "dismissed" | "cancelled"}  -> 204
POST /api/v1/recurring/decisions/clear   {"merchant_key": "netflix", "cadence": "monthly"}                                         -> 204
```

Each series: `merchant_key`, `name` (the newest payment's, spaces collapsed), `cadence`, `amount` and `previous_amount` (2-decimal strings), `payments`, `first_date`, `last_date`, `next_expected`, `late`, `status`. `items` lists active series (late first, then by next date, then name) followed by cancelled ones (by name); `hidden` the dismissed ones. `monthly_total` adds active series only (weekly × 52 ÷ 12, monthly × 1, yearly ÷ 12), exactly, then rounds half up to whole rupees. The writes need the session and the app's `Origin` (403 "Invalid request origin."); the key is normalised like `merchant_key`, and an empty key or "Unknown" (422 "Enter a merchant name."), invalid characters or more than 255 characters, or a cadence or status outside the lists gives 422. A write replaces the one decision for that merchant and cadence; clearing one that isn't there is still 204. No 404 or 409.

Notifications (in-app, linking to `/report/recurring`), from the `recurring` post-save check, only when the saved payment is the newest of its series and never for a dismissed merchant and cadence:

- **New** (`kind = "recurring_new"`, dedupe key `recurring_new:<cadence>:<sha256 of the key>`): the series wouldn't exist with that cadence without this payment, so subscriptions you've had for months send nothing when this ships. "New recurring payment: Spotify" / "₹119 a month, next around 20 Nov". Once per merchant and cadence, ever.
- **Price** (`kind = "recurring_price"`, dedupe key `recurring_price:<transaction id>`): "was" is set and the two payments before the newest were within 5% of each other. "Netflix went up: ₹649" / "Was ₹499. Next around 13 Dec." ("went down" for a drop). A bill that varies (electricity) sends none.

Money in these texts drops a trailing ".00" ("₹649", "₹10,688.10"); names over 120 characters are cut with "…".

## Upcoming payments

`upcoming.py` lists what is about to leave your accounts in the next days, worked out on every read (nothing about paid or missed is stored), from three kinds:

- **Auto-debit** (`auto_debit`): a bank's pre-debit notice for a mandate or AutoPay ("AutoPay for NETFLIX, debit of Rs 649.00 is scheduled on …").
- **Card bill** (`card_bill`): a credit-card statement or bill reminder, with its total and minimum due.
- **Expected** (`expected`): an active recurring series (see Recurring payments) whose next payment falls in the range, late ones included at their past date.

Notices are stored in `upcoming_payments` (added by `create_all`; no `user_id`, every row is the owner's like unassigned SMS rows; unique on `notice_key`, `uq_upcoming_notice`).

- **Merge:** an expected item is left out when an auto-debit notice, dismissed or not, shares a meaningful word with the series name (`merchants.share_word`: 3+ characters with a letter, case ignored, common words such as "bank", "hdfc", "upi", "autopay" ignored) and is due within 7 days of the expected date. The notice has the real amount and date (a price rise shows at its new price).
- **Paid** (payments are your parsed INR rows, unassigned SMS included, not a possible duplicate waiting for your decision; hand-added and edited ones count at once, deleted ones stop counting):
  - auto-debit: a debit dated from 2 days before to 5 days after the due date (IST), within ±1% of the amount, sharing a word with the payee (the merchant or the SMS text);
  - card bill: the card's own "payment received" credits (category Credit Card Payment, the same `account_ref` compared without case or extra spaces, dated on or after the day the notice arrived) add up to the minimum due, or the total when there is no minimum. The bank-side "towards … Credit Card" debit is not used: its account is the bank's.
- **No payment seen** (`not_seen`): not paid and today is more than 5 days after an auto-debit's due date, or after a card bill's due date. Otherwise `upcoming`; an expected item is `late` when its series is late.
- **Listed:** notices not dismissed and due from 7 days ago up to the end of the range, plus the expected items; sorted by date, then card bills, auto-debits, expected, then payee (case ignored).
- **Dismiss** hides a notice for good (`dismissed_at`); expected items are hidden from Recurring payments instead ("Not recurring").

```http
GET  /api/v1/upcoming?days=30          -> {"currency": "INR", "items": [...]}   (days 1–62, default 30, today included)
POST /api/v1/upcoming/{id}/dismiss     -> 204   (also when already dismissed)
```

Each item: `id` (null for expected items), `kind`, `payee`, `account_ref` (null for expected items), `amount` and `min_amount` (2-decimal strings; `min_amount` only for card bills that give one), `date` (the due date, or the expected date), `status` (`upcoming`, `paid`, `not_seen`, `late`), `paid_amount` and `leaves_bank`. There is no total: the app adds up the range it shows. Both routes need the owner session (401); dismiss also needs the app's `Origin` (403 "Invalid request origin."), a UUID id (422) and an existing notice (404 "Upcoming payment not found."). `days` outside 1–62 is 422.

For Safe to spend: `paid_amount` is the sum of the matching card-payment credits for a card bill (whether or not it reaches the minimum), so what is still owed is `amount − paid_amount`; it is "0.00" for the other kinds. `leaves_bank` is true for card bills, for auto-debits unless the notice is about a credit card (`balance.affects_bank_balance`), and for expected items when their series' newest payment came from the bank (not a card spend and not Cash).

The rules are written and tested against HDFC and ICICI wording only; other banks' notices may not be recognised.

### Notices (storing, Phase 0)

**How a notice is read** (`notices.py`, pure): an SMS the parser ignores is checked by `notices.parse_notice`. Requests (OTPs, collect and payment requests, pre-approved offers) and failed payments are never notices.

- **Card bill:** the word "card", a card reference ("HDFC XX9012"), a rupee total right after a total label ("total (amount) due", "amount due", "bill of", "bill amount"; the minimum after "min(imum) (amount) due", in either order) and a due date right after the first "due on/by/date" or "pay(ment) by". The payee is "HDFC card XX9012" ("Card XX9012" when no bank is named). "Payment due date IMMEDIATE", a missing or past due date: nothing.
- **Auto-debit:** "will be debited/deducted/charged", "to be debited" or "is/are scheduled on/for", with a rupee amount (the first one that is not a balance or limit; another currency, 0 or a missing amount gives nothing), a payee after towards/for/to/by/at (skipping dates, amounts and words such as "mandate", "autopay", "your", "the", "upi") and a date: "today", "tomorrow" or the first day-first date (12-12-26, 12/12/2026, 12.12.26, 12-Dec-26, 12 Dec 2026, 12Dec26, 12 Dec, 2026-12-12; three-letter months only; no year means the received year, or the next one when that date is more than 60 days back). A past date gives nothing. "INR 500 will be debited tomorrow" has no payee, so it is not a notice.
- **One row per bill or debit:** `notice_key` is a SHA-256 of kind | card (card bills) or payee, case and spaces ignored (auto-debits) | due date. A repeat or a reworded reminder (the statement, then "bill is due") stores nothing new, except filling a missing minimum. The row keeps the SMS text in `raw_message` and its received time in `created_at`.

**Not stored yet.** Auto-debit and card-bill notices are recognised but **not stored** until `notices.STORE_NOTICES` is turned on with real samples (BUILD_ORDER step 21, `docs/designs/gated_steps_decision.md`). Until then the table stays empty, every read works with no rows, and the Upcoming list shows expected items from your recurring payments only. The webhook writes one log line per recognised notice to the server's output (INFO, logger `main`), `INFO:     main: Notice recognised, not stored (STORE_NOTICES off): kind=card_bill bank=HDFC`: the kind and bank only, never the text, amount, payee, account or date. That line shows whether the Shortcut forwards notices at all. `STORE_NOTICES` is a code constant: there is no environment variable, setting or button for it.

**Turning storing on (Phase 0, the owner):**

1. Collect one auto-debit / AutoPay pre-debit notice and one card statement or bill-due SMS from each bank (HDFC and ICICI).
2. Check the server log for the "Notice recognised, not stored" line for each, to confirm the Shortcut forwards them.
3. Mask them (account and card numbers, mandate IDs, names) and add them to `REAL_NOTICES` in `tests/test_notices.py`, each with the `Notice` it must give (read the comment above it for the received date used).
4. Amend the rules in `notices.py` until they pass, and the design's Notice rules (`docs/designs/upcoming_payments.md`).
5. Set `STORE_NOTICES = True` in `notices.py` in the same commit.

`test_store_notices_needs_real_samples` fails the suite when storing is on without a sample of each kind, and `test_real_notices_parse` is skipped ("pending real HDFC/ICICI notice SMS (Phase 0)") until samples exist. Once storing is on, a recognised notice replies `{"status": "ignored", "message": "Saved as an upcoming payment."}`, or "Upcoming payment already saved." for a repeat; any error while reading or storing one is logged ("Could not handle a notice SMS") and the reply is the plain "Message is not a completed transaction.". No transaction is ever saved for a notice. Notices received before that commit are not recovered.

Tests: `tests/test_upcoming.py` (the paid and not-seen rules at their edges, the list, merge, `leaves_bank`, order and statement counts, the API and dismiss, the reminder) and `tests/test_notices.py` (the notice rules on synthetic text, storing, the webhook with storing on and off, the Phase 0 gate).

## Refund watch

`refunds.py` keeps track of money you're owed back: a return, a cancelled order, a failed booking. You mark a payment "Expect a refund"; when a matching credit arrives Folio asks "Is this your Myntra refund?" with Yes / No; if nothing has come by the date you picked, it reminds you once.

Watches live in `refund_watches` (added by `create_all`; no column is added to an existing table):

| Column | |
| --- | --- |
| `id` | UUID |
| `user_id` | the owner (FK `users`, cascade) |
| `transaction_id` | the payment (FK `transactions`, cascade; unique, `uq_refund_watch_transaction`: one watch per payment) |
| `expected_amount` | `Numeric(12, 2)`, more than zero and at most the payment |
| `due_date` | "expected by", an IST date |
| `credit_id` | the credit you confirmed (FK `transactions`, set null; unique, `uq_refund_watch_credit`: one credit settles one payment) |
| `created_at`, `closed_at` | `closed_at` is set when you confirm a credit |

A watch is **open** (`expected`) while `credit_id` is null and **refunded** once it's set; "No refund expected" deletes the row. Every transaction response carries `refund` (`"expected"`, `"refunded"` or `null`: this row is a watched payment) and `refund_for` (on the confirmed credit, the payment's id, else `null`). Every write that changes either moves `updated_at` on each row it changes, in the same commit, so the app's poll picks them up.

```http
POST   /api/v1/refunds                                       {"transaction_id": "<uuid>", "amount": "2499", "due_date": "2026-11-29"}  -> 201 RefundWatchOut
GET    /api/v1/refunds/{transaction_id}                      -> 200 RefundWatchOut
DELETE /api/v1/refunds/{transaction_id}                      -> 204   ("No refund expected")
POST   /api/v1/refunds/{transaction_id}/accept/{credit_id}   -> 200 RefundWatchOut (state "refunded")   (Yes)
POST   /api/v1/refunds/{transaction_id}/reject/{credit_id}   -> 204   (No)
```

`RefundWatchOut` = `{"transaction_id": "<uuid>", "expected_amount": "2499.00", "due_date": "2026-11-29", "credit_id": "<uuid>" | null, "state": "expected" | "refunded"}`. Every route needs the owner session (401); the writes also need the app's `Origin` (403 "Invalid request origin."). A malformed UUID, date or amount (more than 2 decimal places or 12 digits) is FastAPI's 422 with a list `detail`. Errors, checked in this order:

- **Mark:** 404 "Transaction not found." (unknown, or another user's; unassigned SMS rows are yours) · 422 "Only payments can expect a refund." (a credit, or an SMS that couldn't be read) · 422 "Enter a refund amount more than zero." · 422 "The refund can't be more than the payment (₹2,499)." · 422 "Pick a date 1 to 60 days from today." (IST; the app suggests 10 days) · 409 "A refund is already expected for this payment." (open or refunded; also when two taps race). Cancel first to change one.
- **Get / cancel:** 404 "No refund is expected for this payment." Cancelling a refunded watch makes its credit a plain credit again.
- **Accept:** 404 "No refund is expected for this payment." · 404 "Credit not found." (not your parsed credit) · 409 "This refund is already confirmed." (even for the same credit) · 409 "That credit is already matched to another refund." It sets `credit_id` and `closed_at`.
- **Reject:** always 204. It deletes that suggestion (its `refund:{payment}:{credit}` notification, and its swipe-away row); the watch stays open.

**Suggestion** (post-save check `refunds`, for `sms`, `manual` and `restored`): a saved credit is offered for **one** open watch. The credit must be parsed, more than zero, not Credit Card Payment or Self Transfer (a card payment or your own transfer is never a refund), and not already a watch's refund. A watch is a candidate when its payment has the same currency and was made from 90 days before the credit up to the credit (inclusive), and the credit is **exactly** the expected amount (from anyone: a refund via UPI may come from a payment company's name), or **less** and shares a meaningful word with the payment's merchant (`merchants.share_word` on the credit's merchant and SMS text, so a credit whose merchant wasn't read still matches on its text; bank names and other common words don't count). The best wins: exact amount first, then the nearest payment, then the older watch. A larger credit is never suggested. One notification per payment and credit, ever:

- `kind = "refund_match"`, "Is this your Myntra refund?" ("Is this a refund?" when the payment has no merchant) / "₹2,499 from Myntra Designs BANGALORE on 21 Oct" ("₹2,499 on 21 Oct" when the credit has none; the credit's IST date), link `null`, dedupe key `refund:{payment id}:{credit id}`.
- Buttons (`notifications.register_actions`, the first real resolver): Yes → `POST /api/v1/refunds/{payment}/accept/{credit}`, No → `POST /api/v1/refunds/{payment}/reject/{credit}` while the watch is open and the credit still exists; once decided, "Marked as refunded" (this credit closed it) or "No longer applies" (another credit closed it, the watch was cancelled, or the credit was deleted). At most 2 statements per inbox read.

**Reminder** (daily job `refund_overdue`, see Daily jobs): one bell notification per open watch whose date has passed.

**Deleting a transaction** (`DELETE /api/v1/transactions/{id}`, same commit): a watched payment's watch is deleted (its confirmed credit's `refund_for` clears and the credit is bumped); deleting a confirmed credit makes its watch open again (`credit_id` and `closed_at` cleared, the payment bumped), so a later credit can be suggested.

**Limits:** edits don't touch watches (editing a watched payment's amount or type, or a confirmed credit, leaves the watch as it is; cancel it to start over). A rejected suggestion can come back only if the same credit is saved again (deleted and re-sent). Cost: the check is free for anything but a counted credit, then 1 statement, or 2 when an open watch is in the window, plus one commit when it notifies.

Tests: `tests/test_refunds.py` (marking and every error, suggestions and their edges, Yes / No, the buttons and their resolved texts, the reminder job, cleanup on delete, statement counts).

## Self-transfer suggestions

`transfers.py` spots money you moved between two of your own accounts (ICICI → HDFC, or one HDFC account to another) and asks "Looks like a transfer between your accounts" with **Confirm** / **Not a self transfer**. Nothing changes until you tap Confirm: both legs keep counting as spending and income until then.

**Which SMS can pair.** Both legs are parsed INR SMS, more than zero, not added by hand or completed by hand (`parsed_by` not `manual` or `edited`), whose text changes a bank balance **and names a bank account** ("A/c", "A/C", "Acct", "Acc", "Account", outside card wording), so debit-card alerts ("Spent Rs.… From HDFC Bank Card x5566") never pair: their account number is the card's. Each has an account number (`account_ref`) that isn't Cash. Neither category is Credit Card Payment, **Cash Withdrawal** (an ATM withdrawal is never a transfer to another of your accounts) or Investments & Finance. Not both are already Self Transfer (one may be). Neither is a possible duplicate waiting for your decision, and neither is already in a pending or confirmed pair. Only newly saved SMS are checked (post-save check `transfers`, cause `sms`), plus the re-check after Not a self transfer; past transactions are never paired.

**What a match is.** The opposite type, the same amount, a **different** account number, and a time within **30 minutes either way, inclusive**. The second leg to arrive finds the first. The closest in time wins; a tie gives no suggestion. A pair that was rejected or expired is never suggested again.

Pairs live in `transfer_pairs` (added by `create_all`; no column is added to an existing table): `id`, `user_id` (the owner, FK `users`, cascade), `debit_id` and `credit_id` (always the DEBIT and the CREDIT leg, FK `transactions`, cascade; unique together, `uq_transfer_pair`), `status` (`pending`, `confirmed`, `rejected` or `expired`), `created_at`, `decided_at`. The pair and its notification are written in one commit; if both legs' checks find each other at the same moment, the unique key keeps one.

**The notification:** `kind = "self_transfer"`, "Looks like a transfer between your accounts" / "₹10,000.00 from ICICI XX310 at 10:02 to HDFC XX4821 at 10:04" (each leg's account and IST time), link `null`, dedupe key `pair:{pair id}`. While pending it has **Confirm** → `POST /api/v1/transfers/pairs/{id}/confirm` and **Not a self transfer** → `POST /api/v1/transfers/pairs/{id}/reject`; once decided, "Marked as self transfer", "Not a self transfer" or "No longer applies" (expired, or the pair is gone). One statement per inbox read.

```http
POST /api/v1/transfers/pairs/{pair_id}/confirm   -> 200 TransferPairOut (status "confirmed")
POST /api/v1/transfers/pairs/{pair_id}/reject    -> 200 TransferPairOut (status "rejected")
```

`TransferPairOut` = `{"id": "<uuid>", "status": "pending" | "confirmed" | "rejected" | "expired", "debit_id": "<uuid>", "credit_id": "<uuid>", "decided_at": "<UTC time>" | null}`. Both need the owner session (401) and the app's `Origin` (403 "Invalid request origin."); a malformed id is FastAPI's 422.

- **Both:** 404 "Suggestion not found." (unknown, or another user's) · 409 "This suggestion was already answered." (not pending, including a double tap).
- **Confirm** re-checks both legs first: each still exists and is yours, both are parsed, the debit is a DEBIT and the credit a CREDIT of the same amount and currency, neither category is Credit Card Payment, Cash Withdrawal or Investments & Finance, and neither leg is in another confirmed pair. If that fails the pair becomes `expired` and you get 409 "This suggestion no longer applies.". Otherwise one commit marks the pair `confirmed` and sets both legs to Self Transfer with `updated_at` bumped (a leg a category rule set goes from `regex+rule` to `regex+user`), so the app's poll picks them up and both stop counting. Confirm writes **no** merchant memory.
- **Not a self transfer** marks the pair `rejected`; both legs are unchanged and keep counting. Each leg is then checked again in the background, so a real partner that lost to a coincidence can still be suggested.

Dismissing the notification (swipe or Clear) only hides it: the pair stays pending and both legs keep counting. **Deleting either leg** (`DELETE /api/v1/transactions/{id}`, same commit) deletes its pairs in any status; the other leg keeps its category.

**Limits (accepted):** a limit alert for the debit leg may already have gone out before you confirm. Past transactions are never paired, and there is no matching on the UPI reference / UTR.

**Status:** pairing is live but not yet checked against a real HDFC/ICICI pair: `REAL_PAIRS` in `tests/test_transfers.py` is empty, so `test_real_pairs_pair` is skipped (the skip reason shows in every test run). To close that gap, find one past transfer between your own HDFC and ICICI accounts in Activity, copy its two SMS, mask the account numbers, names and references, and add them to `REAL_PAIRS`. If the test fails, the parser reads that wording differently: fix the parser, not the pairing rules. If both legs share a UTR, reopen Alternative F (reference matching, count_real_spending.md).

**Turning it off:** delete the `post_save.register("transfers", …)` line in `main.py`. There is no setting: every suggestion needs your Confirm, and Not a self transfer handles a noisy one.

**Cost:** 0 statements for an SMS that can't pair, 1 query otherwise, plus create_once's check and one commit when it suggests. The resolver is 1 statement per inbox read; Confirm at most 5 statements and one commit.

Tests: `tests/test_transfers.py` (every pairing rule from raw SMS, both stricter rules, the window edges, closest and tie, races, Confirm / Not a self transfer and every error, the buttons and their resolved texts, cleanup on delete, statement counts, and the skipped real-pair gate).

## Lend & borrow

`ledger.py` records splits and loans with people: who owes you, and whom you owe. Entries live in `ledger_entries` (added by `create_all`): `person` (as typed, spaces collapsed, up to 60 characters), `person_key` (spaces collapsed, case folded: "Rahul" and "rahul " are one person), `kind`, `amount` (more than zero), and either `transaction_id` (a share of a parsed INR transaction) or `day` (a cash entry, IST, today or earlier), plus an optional `note` (up to 200 characters).

| Kind | On | Sign |
| --- | --- | --- |
| `lent` ("Lent to") | a payment, or cash | +1 |
| `repaid_by_me` ("Repaid to") | a payment, or cash | +1 |
| `borrowed` ("Borrowed from") | money received, or cash | −1 |
| `repaid_to_me` ("Repayment from") | money received, or cash | −1 |

A person's balance is Σ sign × amount: **positive means they owe you**, negative that you owe them. Overpaying turns it around. Linked entries lower the transaction's counted amount (see What counts as spending); every transaction carries `linked_amount` (0 when none).

```http
GET    /api/v1/ledger                                   -> {"people": [{"person": "Rahul", "key": "rahul", "balance": 1600.0, "entries": [EntryOut, newest first]}]}
POST   /api/v1/ledger   {"person": "Rahul", "kind": "lent", "amount": "500", "day": "2026-11-18", "note": null}   -> 201 EntryOut (cash)
DELETE /api/v1/ledger/{id}                              -> 204
GET    /api/v1/transactions/{id}/ledger                 -> [EntryOut, oldest first]
PUT    /api/v1/transactions/{id}/ledger   {"entries": [{"person": "Rahul", "kind": "lent", "amount": "600", "note": null}]}
                                                        -> {"linked_amount": 1200.0, "updated_at": "…", "entries": [EntryOut]}
```

`EntryOut` = `{"id", "person", "kind", "amount", "day" (the cash day, or the transaction's IST date), "transaction_id" | null, "merchant" (the transaction's) | null, "note" | null}`. People are ordered by the size of their balance (ties by name), then settled people, the most recent first; `person` is the spelling of their newest entry. A new entry (POST, or PUT on a transaction) for a person you already have is saved in their existing spelling ("rahul " becomes "Rahul"), so typing another case never relabels their history; a person whose only entries a PUT replaces takes the new spelling. Session on every route, `Origin` on writes.

- **PUT** replaces the transaction's entries (0–20; `[]` removes them all). Checked in this order (422): "Only payments Folio has read can have lend & borrow entries.", "Lend & borrow is in rupees only.", "On a payment, use Lent to or Repaid to." / "On money received, use Repayment from or Borrowed from.", "Shares add up to more than the payment." (equal is fine). Field errors: "Enter a name.", "Names can be up to 60 characters.", "Names can't contain invalid characters.", "Enter an amount above zero." (also for more than 2 decimals), "Notes can be up to 200 characters.", "Up to 20 entries per payment.", and for cash "The date can't be in the future.". It moves the transaction's `updated_at` (the app's poll sees the new `linked_amount`) and, on a payment, runs the spend-limit check again. The category isn't touched and nothing goes into merchant memory.
- **DELETE** of a linked entry moves its transaction's `updated_at` in the same commit and, for a parsed payment, runs the limit check again (its counted amount rose). 404 "Entry not found.".
- **Editing a transaction with entries** (`PATCH /api/v1/transactions/{id}`): changing its type is 409 "Remove its lend & borrow entries first."; an amount below the entries is 409 "The amount can't be less than the ₹1,200.00 in lend & borrow entries."; a category edit keeps them.
- **Deleting a transaction** deletes its entries in the same commit (`ledger.release`); cash entries stay.

Cost: `GET /ledger` is one query; the counted amount is one grouped outer join inside the queries that already sum spending, so `GET /api/v1/limits` keeps its round trips. Tests: `tests/test_ledger.py`.

## Savings goals

`goals.py` stores goals in `savings_goals` (added by `create_all`): a name (1–60 characters), a `target` (more than zero), an optional `target_month` ("YYYY-MM"), a `tag`, `manual_saved` ("Saved so far" added by hand, zero or more) and `archived`. Progress is worked out in the app: `manual_saved` plus the counted amount of every parsed INR payment tagged `#tag` in its note, so moving money to a savings account (category Self Transfer, not spending) and tagging it counts toward the goal.

```http
GET    /api/v1/goals          -> [Goal]: active ones oldest first, then archived ones
POST   /api/v1/goals   {"name": "New phone", "target": "60000", "target_month": "2027-03", "tag": "#phone", "manual_saved": 0, "archived": false}  -> 201 Goal
PUT    /api/v1/goals/{id}     (replaces every field; "Add money" sends the new manual_saved)  -> 200 Goal
DELETE /api/v1/goals/{id}     -> 204
```

`Goal` = `{"id", "name", "target", "target_month" | null, "tag", "manual_saved", "archived", "created_at"}`. Tags follow the app's tag rule (`lib/tags.ts`): trimmed, one leading "#" dropped, NFC, lower case; 1–30 letters (any language, with their marks), digits, "_" and "-", with at least one letter. Only one **active** goal can use a tag: 409 "Another goal uses #phone." (archived goals keep theirs). 422: "Enter a name." / "Names can be up to 60 characters." / "Names can't contain invalid characters.", "Enter a target above zero.", "Pick a month like 2027-03.", "Saved so far can't be below zero.", "Enter a tag." / "Tags use letters, digits, _ and -." / "Tags can be up to 30 characters." / "A tag needs at least one letter.". 404 "Goal not found." for an id that isn't yours. Tests: `tests/test_goals.py`.

## Daily jobs and the SMS watchdog

Every number in Folio depends on the iPhone Shortcut forwarding bank SMS. If it stops (an iOS update, a revoked permission, a changed Shortcut), Folio simply receives nothing. A daily check run by the server itself warns you.

### The daily job runner (`jobs.py`)

- **When:** once a day at **21:00 IST**, inside the server process (an `asyncio` task started at start-up and cancelled at shutdown). **120 seconds after start-up** it also runs a catch-up, so a server that was down or asleep at 21:00 catches up (the delay lets an SMS that woke a sleeping host be saved first).
- **Due:** a job is due when it hasn't run since the most recent 21:00 IST. The scheduler, the catch-up and the external trigger all use that one rule.
- **Claim:** before a job runs, its row in `job_runs` (`name` primary key, `last_run`; created by `create_all`, runner state with no owner) is updated only if `last_run` is before that 21:00 (or inserted on the first run; a unique-key clash means another worker won). So several workers, a restart or a cron added later never run a job twice in one slot. A job that fails after its claim is logged and waits for the next day.
- **Jobs** are registered in one block in `main.py` (`jobs.register("watchdog", watchdog.daily)`, then `jobs.register("upcoming_reminders", upcoming.daily)`, then `jobs.register("refund_overdue", refunds.daily)`) and run in that order, each in its own session, with the owner (`DASHBOARD_OWNER_EMAIL`; none configured or not signed up yet → nothing runs) and today's IST date.

**External trigger.** If your host sleeps when idle, the in-process timer never fires. Set `JOBS_SECRET` in `.env` (a long random string, different from `WEBHOOK_SECRET`) and have a free cron service, or the host's cron, call this daily at **21:05 IST (15:35 UTC)**:

```bash
curl -fsS -X POST -H "X-Jobs-Secret: $JOBS_SECRET" https://<your-server>/api/v1/jobs/daily
```

```http
POST /api/v1/jobs/daily              -> 200 {"ran": ["watchdog", "upcoming_reminders", "refund_overdue"]}   (the jobs that completed in this call; [] when nothing was due)
POST /api/v1/jobs/daily?force=true   -> runs every job now, even if it already ran since 21:00 IST
```

- `401 {"detail": "Invalid or missing jobs secret."}`: `X-Jobs-Secret` missing or wrong. The webhook secret is always refused here, even if `JOBS_SECRET` was set to the same value.
- `503 {"detail": "JOBS_SECRET is not configured."}`: no `JOBS_SECRET` (the trigger is off; the in-process scheduler still runs).
- Other methods get 405. No session, no `Origin` check and no body; `X-Jobs-Secret` is not in the CORS headers (server to server only).

**Never give a cron service the webhook secret**: anyone holding it can post fake bank SMS. A leaked jobs secret can only run the daily check.

### Card-bill reminder (`upcoming_reminders`)

`upcoming.daily` reads the upcoming list for today and tomorrow and, for each card bill still `upcoming` (not paid, not dismissed) due today or tomorrow, adds one bell notification: `kind = "card_bill_due"`, "HDFC card XX9012 bill due tomorrow" ("… due today" on the due date) / "₹4,500 due on 5 Dec (minimum ₹500). No payment seen yet." (no "(minimum …)" when the bill gives none), link `/upcoming`, dedupe key `card_bill_due:<notice id>`. The 21:00 run normally sends it the evening before; if that run is missed, the catch-up or the due-date run sends it. **Once per bill**, and never emailed. A failure is logged and rolled back, and the job still counts as run. Until notices are stored (`STORE_NOTICES` off, see Upcoming payments) it finds no card bills and creates nothing.

### Refund reminder (`refund_overdue`)

`refunds.daily` finds your open refund watches (see Refund watch) whose "expected by" date is before today (IST) and adds one bell notification each: `kind = "refund_overdue"`, "Refund from Myntra still pending" ("Refund still pending" when the payment has no merchant) / "₹2,499 expected by 13 Nov." (₹1,000.50 for a partial amount), link `/activity?txn={payment id}` (Activity opens that payment), dedupe key `refund-overdue:{watch id}`. **Once per watch**, never emailed; a refunded or cancelled watch gets none. A failure is logged and rolled back, and the job still counts as run. Cost: 1 statement, plus one commit per new reminder.

### The SMS watchdog (`watchdog.py`)

Settings → Alerts: `watchdog.enabled` (default on; off, nothing below runs), `watchdog.quiet_days` (N, 2–14, default 4), `watchdog.salary_check` (default on), `watchdog.email` (default on). **Received SMS** are stored rows not added by hand (`parsed_by` other than `manual`), readable or not, edited and held-out possible duplicates included, yours or unassigned. Ignored SMS (OTPs, offers, exact repeats) are never stored, so they don't count: a working Shortcut is reported quiet only after N days with no stored bank SMS at all (no payments either). That's accepted rather than writing a heartbeat on every webhook call.

| Check | Fires when | Title | Dedupe key | Link |
| --- | --- | --- | --- | --- |
| Quiet pipeline | the newest received SMS is more than N days old (no SMS ever: nothing) | No bank SMS for 4 days — check your iPhone Shortcut | `watchdog:quiet:{IST date of that SMS}` | `/settings#alerts` |
| Quiet bank (skipped while the pipeline is quiet) | a bank with at least 3 SMS in the last 60 days has none for longer than N days or twice its longest gap between SMS in those 60 days (whichever is longer), while another bank has one within N days | No HDFC SMS for 4 days | `watchdog:quiet:{BANK}:{IST date}` | `/settings#alerts` |
| Salary late (`watchdog.salary_check`) | at least 2 salary days in the last 120 days, and today is more than 3 days past the expected payday | Salary usually arrives around 30 Nov; not seen yet | `watchdog:salary:{expected date}` | `/settings#alerts` |
| Unreadable | 5 or more received SMS not parsed in the last 24 hours | 6 SMS couldn't be read today | `watchdog:unreadable:{today}` | `/activity?kind=review` (Needs review) |

`n` in the titles is the whole days since that SMS. `BANK` is `parser.detect_bank`'s name (HDFC, ICICI, SBI, Axis, Kotak), from the sender or the text; rows with no bank are ignored by the bank check. Bodies:

- Quiet pipeline: "The last bank SMS reached Folio on 16 Nov. On your iPhone, open Shortcuts → Automation and check that the bank-SMS automation is still on and runs without asking."
- Quiet bank: "ICICI SMS still arrive, but the last HDFC SMS reached Folio on 15 Nov. Check that your iPhone automation still forwards HDFC's messages."
- Salary late: "No credit in the Salary category since 31 Oct. If it has arrived, set its category to Salary in Activity."
- Unreadable: "They're in Activity under Needs review. If your bank changed its SMS format, more will fail until Folio's parser is updated."

All are `kind = "watchdog"`, no buttons. **Once per episode:** the dedupe key names the episode, so the same silence never warns twice; the next SMS (or salary) clears it, and a later silence has a new key and warns again. Each check runs on its own (one that fails is logged and the others still run). Cost: 5 statements when nothing fires (settings, newest SMS, the banks' SMS of 60 days, salary days, unreadable count; 4 while the pipeline is quiet or with the salary check off), 1 when off, +2 per new warning.

**Usual payday** (`pay_periods.py`, the one rule for "when salary usually arrives"): each salary day (see Pay periods) becomes its distance from the nearest 1st, `day_offset` (1st–15th → day − 1, so 2 Nov → 1; 16th or later → minus the days to the next 1st, so 31 Oct, 30 Nov and 28 Feb → −1, 25 Oct → −7). `usual_offset` is the lower median of the last 3 (at least 2 needed); `expected_after` is that offset from the 1st of the month after the newest salary's name (`payday_in`). Example (CUJ 3): salary on 31 Aug, 30 Sep and 31 Oct → offset −1 → expected 30 Nov; on 4 Dec, with nothing since 31 Oct, it's late (more than 3 days past) and warns "Salary usually arrives around 30 Nov; not seen yet". A plain median of the day of the month would have said "the 30th/31st" of the wrong month. Salary detection depends on the Salary category being set correctly, and a mid-month bonus in Salary counts as the newest salary (it moves the expected date to the following month).

**Email:** a warning is also emailed only when it was newly created, `watchdog.email` is on and Gmail is configured (see Spend limits, notifications and email alerts): to `ALERT_EMAIL_TO` (or the owner), subject = the title, text = the body plus "Open Folio: Settings → Alerts.". If Gmail refuses it, the error is logged and one bell notification per day says "Email alerts aren't being sent" ("Gmail refused the last email. Run scripts/gmail_authorize.py again (see the server README).", key `watchdog:email-failed:{today}`). While the OAuth app is in *Testing*, Google expires the refresh token every 7 days, so email would stop silently; publish the app (see [Optional: get the Gmail OAuth credentials](#optional-get-the-gmail-oauth-credentials-one-time), step 3).

Tests: `tests/test_jobs.py` (the slot, the claim, races, failures, the scheduler, the lifespan task and the trigger), `tests/test_watchdog.py` (each check at its threshold, once per episode, settings, statement counts, email mocked, the trigger end to end), `tests/test_upcoming.py` (`ReminderTests`: the card-bill reminder), `tests/test_refunds.py` (`OverdueTests`) and `tests/test_pay_periods.py` (`UsualPaydayTests`).

## Spend limits, notifications and (optional) email alerts

Set a weekly (Mon–Sun, IST) and/or monthly limit in **Settings → Spending limits**. Monthly limits follow calendar months, or your pay periods when "Month starts on payday" is on (see Pay periods); their keys and wording are the same either way. When a debit SMS takes your spending to or past a limit, Folio creates one **in-app notification** for that week/month: a red dot on the bell on Home, opening a drawer, with each notification linking to Settings → Spending limits. That needs no setup. **Email is optional and off** until you add the Gmail keys below; with them, the same event also sends one email. "Spent" matches the app: parsed debits, card spends included, except Credit Card Payment, Self Transfer and Investments & Finance (see What counts as spending), so a card bill or an SIP never trips a limit. The email goes out in the background after the webhook responds. A failed send is retried on the next payments (up to 3 tries), and the error shows in Settings. Raising a limit re-arms its alert.

**Warning before a limit is reached:** once spending on a limit is at or above the warning level (`limits.warn_percent` in Settings; 80% by default, Off with `null`) and still under the limit (level ≤ spent < limit, compared exactly, so ₹4,000.00 of ₹5,000 warns at 80%), Folio creates one in-app notification, `kind = "spend_warning"`: "80% of your monthly Food & Dining limit used", "80% of your weekly spend limit used" (overall) or "80% of your monthly Swiggy limit used" (a merchant name over 120 characters is cut with "…"). The body is the same as the reached alert's ("₹4,150.00 of ₹5,000.00 on Food & Dining this month (Thu 1 Oct – Sat 31 Oct) · 83%"; " at Swiggy" for a merchant, nothing for overall) and it links to `/settings/limits`. It goes out once per limit, period, limit amount and level, by its dedupe key: `spend_warn:{period}:{start}:{limit}:{level}` overall, `spend_warn:{category}:{period}:{start}:{limit}:{level}` for a category, `spend_warn:merchant:{limit_id}:{period}:{start}:{limit}:{level}` for a merchant (for example `spend_warn:Food & Dining:monthly:2026-10-01:5000.00:80`); so changing the limit or the level allows one more. Warnings are never emailed. A payment that jumps straight past the limit gives only the reached alert ("… limit crossed"), no warning. The rule depends only on the current total, so every check gives the same answer: an edit that brings spending down from over the limit back into the band warns once; a late SMS for a past week or month never warns (only the payment's own current period is checked, as for reached alerts); and `check_spend_limits` (merges and rule applies) warns too. The same guards apply as for reached alerts: the overall limit only for payments in its currency, a category limit only for the payment's own category, merchant limits only where the name matches. `GET /api/v1/limits` is unchanged: a meter's `alerted` means the reached alert only.

**Buttons on notifications:** every item in `GET /api/v1/notifications` has `actions` (`[{label, path}]`, each a same-origin `POST` under `/api/v1/` with no body) and `resolved` (text shown in place of the buttons once decided). Both are empty for spend-limit alerts; Refund watch's `refund_match` and Self-transfer suggestions' `self_transfer` notifications have buttons. A feature adds buttons to its own kind with `notifications.register_actions(kind, resolver)`: the inbox calls the resolver once per request with that kind's listed notifications and gets back buttons or resolved text per id. A resolver that fails is logged and its notifications just show no buttons. Kinds with buttons have `link: null`.

**Clearing notifications:** `DELETE /api/v1/notifications/{id}` (swipe to delete) and `DELETE /api/v1/notifications` (Clear) hide notifications instead of deleting them (`notification_dismissals`, added by `create_all`). Each notification is also the record that its alert went out, so a cleared limit alert isn't sent again on the next payment, and the meter in Settings still says "notified".

**Per category:** in the same card, *Limit for* switches between **All spending** (the overall limit above) and a category, each with its own weekly and/or monthly amount. A category limit counts only debits in that category, and crossing it raises its own notification ("Weekly Food & Dining limit crossed"). It's checked when an SMS arrives, when you add or edit a transaction, and when the background AI retry fills a category in later. Money-in categories (Salary, Income, Refunds & Cashback), Uncategorized and the categories that aren't spending (Credit Card Payment, Self Transfer, Investments & Finance) can't have limits (422 "Pick a spending category, or All."). Limits saved on those before this release are hidden and never alert; `scripts/recategorise_card_payments.py` deletes them. With Gmail set up, a category alert also sends one email, best effort (not retried).

```http
GET /api/v1/limits   -> overall `weekly`/`monthly`, plus `categories`: [{category, weekly, monthly}]
                        and `merchants`: [{id, merchant, counts, weekly, monthly}]
PUT /api/v1/limits   {"weekly_limit": "5000", "monthly_limit": null}                            (overall)
PUT /api/v1/limits   {"category": "Food & Dining", "weekly_limit": "1500", "monthly_limit": null}
```

Leaving both amounts null for a category removes its limit. Category limits live in the `category_limits` table, which `create_all` adds on the next server start. Your own categories (see Categories) can have limits too.

**Rollover:** a monthly limit, overall or a category's, can roll over. Its limit then adds what you didn't spend of last month's limit, or takes off what you overspent, between ₹0 and twice the limit: `limit = base + max(−base, min(base, base − last month's spending))`. ₹5,000 with ₹4,200 spent last month gives ₹5,800; ₹6,500 spent gives ₹3,500; ₹12,000 gives ₹0. "Last month" is the previous calendar month, or the previous pay period with 'Month starts on payday' on (no carry when there is none). Carry starts with the first month that begins after the IST day you turned it on (`enabled_at`); until then the limit is the base. Weekly and merchant limits never roll over.

```http
PUT /api/v1/limits/rollover   {"category": "Food & Dining", "enabled": true}   ("All" or null for the overall limit)  -> the full summary
```

422 "Set a monthly limit first." when turning it on without a monthly amount; 422 "Pick a spending category, or All." for a category that can't have a limit. Turning it on again keeps the first `enabled_at`; turning it off always succeeds. Removing a category limit, or saving any limit without a monthly amount, turns its rollover off. Rows live in `limit_rollovers` (unique per user and scope, `uq_limit_rollover`). In `GET /api/v1/limits`, a monthly meter where carry applies shows the rollover limit as `limit` (it can be `0.0`), with `base` (the limit as set) and `carried` (`limit − base`; both `null` elsewhere), and `rollover` is true on the summary (overall) or the category while it's on. Alerts compare spending with the rollover limit and their texts show it, but their dedupe keys and `spend_alerts.limit_amount` use the base, so a late SMS for last month that changes the carry never sends the same alert twice. A ₹0 rollover limit alerts on the first payment ("100%"). Cost: last month's spending joins the summary's one query; an alert check adds one query only when carry applies. Tests: `tests/test_rollover.py`.

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
| Limit check after a debit | as before, +1 to load merchant limits, +1 more when one matches |
| `PUT /api/v1/limits/merchants` | about 8: the existing limit, the known check, the memory check, the writes, then the summary |
| SMS webhook | +1 for your merged name and category rules (one statement); a rule that sets the category also saves the Gemini call |
| SMS webhook, a recognised notice | 0 while storing is off; 2 when on (the `notice_key` lookup, then the insert or the minimum fill) |
| `POST /api/v1/transactions` (adding by hand) | +1 for your merged name and category rules (one statement) |
| After-save checks (background, after the reply) | 4 for the runner (owner, transaction, held-out test, settings), then the limit checks as above, then up to 1 for a payment alert's merchant history |
| `GET /api/v1/recurring` | 2 after the session: your debits in the window (4 columns), then your decisions |
| `GET /api/v1/upcoming` | 3–5: notices, then your payments since the earliest open notice (only when one is open), recurring's 2, then the newest payment of each expected series (only when one is listed) |
| Card-bill reminder (daily job) | the same 3–5, +2 per new notification |
| `GET /api/v1/balance/safe-to-spend` | 6–8: the balance setting, the balance sum, your salary days, then the same 3–5 as Upcoming payments |
| Recurring check after a debit (background) | 1 (your debits in the window), +1 for decisions when the payment is the newest of a series, +2 for a notification |

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
- Ignored obvious OTP, failed/declined payment, payment-request and offer messages, and notices of money that hasn't moved yet: auto-debit / AutoPay pre-debit alerts ("is scheduled on", "will be debited", "due on/by") and card statements or bill reminders ("total/minimum due", "statement … generated"). A notice that also says the money moved ("debited via UPI AutoPay") is still recorded. Auto-debit and card-bill notices are then read by `notices.py` and, once `STORE_NOTICES` is on, stored as upcoming payments (see Upcoming payments); the reply stays `ignored`.
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

Tests use an isolated in-memory SQLite database. Exactly one test is skipped: `test_real_pairs_pair` in `tests/test_transfers.py`, until a real HDFC/ICICI self-transfer pair is added (see Self-transfer suggestions). `check_database.py` makes a read-only count query against your configured database. Provider responses are mocked in OAuth tests; real Google login requires your credentials.
