# Agent Hub — Function Reference

Every function in the backend, grouped by file, with a one-line description of
what it does. Frontend (static HTML + the `site/` landing) is summarized at the
end. Generated 2026-07-21.

Column order used everywhere in the lead sheet:
`Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason, EmailConfidence`

---

## server.py — HTTP API and page routes

### Middleware & error handling
- **`_is_public_landing_asset(path)`** — True if a path resolves (after
  normalizing `..`) under `/static/landing/`; used to let the marketing bundle
  bypass auth without opening a path-traversal hole.
- **`require_auth(request, call_next)`** — Middleware on every request. Public
  paths and landing assets pass through; logged-out `/` gets the marketing
  landing page; unauthenticated `/api/*` gets 401; any other unauthenticated
  page redirects to `/login?next=…`. Otherwise attaches `account_id` and continues.
- **`runtime_error_handler(request, exc)`** — Turns any `RuntimeError` ("account
  isn't set up yet" states) into a clean 409 with the message.
- **`google_api_error_handler(request, exc)`** — Turns a raw Google `HttpError`
  (bad Sheet ID, deleted thread) into a readable 4xx instead of a 500.

### Helpers
- **`_account(request)`** — Loads the full account row for the logged-in user
  (404 if it vanished).
- **`_secure_cookies()`** — Reads `SECURE_COOKIES` env against an explicit
  truthy set (`1/true/yes`) so `"0"`/`"false"` correctly stay off.
- **`_set_session_cookie(response, account_id)`** — Attaches the signed session
  cookie (HttpOnly, SameSite=lax, Secure when configured, 7-day expiry).

### Page routes (serve static HTML)
- **`home()`** `GET /` — App home (agents) for logged-in users. Logged-out users
  never reach it: the middleware serves the landing page instead.
- **`outreach_page()`** `GET /outreach` — Outreach agent page.
- **`leads_page()`** `GET /leads` — Lead sourcing agent page.
- **`settings_page()`** `GET /settings` — Settings page.
- **`login_page()`** `GET /login` — One-button "Continue with Google" page.
- **`signup_page()`** `GET /signup` — 302s straight to `/auth/google/login`;
  kept as a route only because the marketing site's "Start free" CTA and
  bookmarked links point here.

### Auth
- **`logout()`** `POST /logout` — Clears the session cookie.
- **`google_login_start(next)`** `GET /auth/google/login` — Redirects to
  Google's "Sign in with Google" identity consent (no Gmail/Sheets scopes). If
  `?next=` is a validated same-site path, sets a short-lived HttpOnly cookie
  carrying it for the callback to pick up.
- **`google_login_callback(request, state, code, error)`** `GET /auth/google/callback` —
  Verifies the CSRF state, exchanges the code for the Google identity, links or
  creates the account, and drops the user at the `next` cookie's path if
  present, else `/` or `/settings` by onboarding state.

### Account / settings
- **`me(request)`** `GET /api/me` — Returns the logged-in user's email, Google
  connection state, settings, and whether onboarding is complete.
- **`update_settings(request, payload)`** `PUT /api/settings` — Saves sender
  name, meeting purpose, calendar link, notify email, and/or sheet ID.
- **`get_usage(request)`** `GET /api/usage` — Per-account usage totals (LLM
  tokens, searches).
- **`get_plan(request)`** `GET /api/plan` — Pilot plan pricing + the hard limits
  (daily send cap, 10 lead searches/hr).

### Google connection (Gmail + Sheets access)
- **`_google_error_reason(exc)`** — Extracts Google's machine error `reason`
  from an `HttpError`.
- **`list_google_sheets(request)`** `GET /api/google/sheets` — Lists the
  connected user's spreadsheets; translates "Drive API not enabled" and
  "reconnect needed" into clear 409s.
- **`google_connect(request)`** `GET /api/google/connect` — Starts the
  per-account Gmail+Sheets OAuth flow (state = signed account token).
- **`google_callback(request, state, code, error)`** `GET /api/google/callback` —
  Verifies the state matches the logged-in account (CSRF), exchanges the code,
  and stores the encrypted token.
- **`google_disconnect(request)`** `POST /api/google/disconnect` — Clears the
  stored Google token.

### Background jobs
- **`_run_job(job_id, account_id, fn)`** — Runs a job function with usage
  attributed to the account; records result or error in the `JOBS` map.
- **`start_job(account_id, fn)`** — Spawns a daemon thread for `fn`, returns a
  job id to poll.
- **`get_job(request, job_id)`** `GET /api/jobs/{job_id}` — Returns a job's
  status/result; only the account that started it can see it.

### Outreach agent
- **`list_campaigns(request)`** `GET /api/outreach/campaigns` — All contacts from
  the sheet with status, sent time, and verification.
- **`_campaign_preview(account)`** — Computes how many contacts can send today
  vs. the 24h cap.
- **`campaign_preview(request)`** `GET /api/outreach/campaigns/preview` — The
  send preview (eligible / sent today / daily limit / capped).
- **`send_campaigns(request, payload)`** `POST /api/outreach/campaigns/send` —
  Rate-limited (5 batches/hr). Requires explicit `confirmed`; refuses if nothing
  eligible; takes the per-account send lock; spawns the send job. Releases the
  lock even if the thread fails to spawn.
- **`check_replies(request)`** `POST /api/outreach/replies/check` — Rate-limited
  (45/hr). Spawns the reply-check job for the account.
- **`list_replies(request)`** `GET /api/outreach/replies` — Pending review cards
  for the account.
- **`check_single_reply(request, row)`** `POST /api/outreach/campaigns/{row}/check-reply`
  — Read-only "has this one person replied?" check; no drafting, no writes.
- **`send_reply(request, review_id, payload)`** `POST /api/outreach/replies/{id}/send`
  — Sends the (edited) reply in-thread, marks the review sent.
- **`dismiss_reply(request, review_id)`** `POST /api/outreach/replies/{id}/dismiss`
  — Dismisses a review without sending.

### Lead sourcing agent
- **`search_leads(request, payload)`** `POST /api/leads/search` — Rate-limited
  (10/hr). Spawns the lead-search job (limit clamped 1–25).
- **`list_leads(request)`** `GET /api/leads` — Leads awaiting review (status
  "Ready for review" / "Needs verification").
- **`approve_lead(request, row)`** `POST /api/leads/{row}/approve` — Clears the
  status so the lead becomes sendable.
- **`discard_lead(request, row)`** `POST /api/leads/{row}/discard` — Marks a lead
  "Discarded".

---

## outreach-agent/auth.py — signed tokens

- **`_sign(payload)`** — HMAC-SHA256 of a payload with `APP_SECRET_KEY`.
- **`create_session_token(account_id, ttl)`** — Signed `account_id.expiry.hmac`
  session token (7-day default).
- **`verify_session_token(token)`** — Returns the account id if the token is
  valid, correctly signed, and unexpired; else None.
- **`create_oauth_state(ttl)`** — Signed, short-lived (10 min) CSRF nonce for the
  Google login flow; one-dot format so it can't be confused with a session token.
- **`verify_oauth_state(token)`** — Validates that nonce.

## outreach-agent/accounts_db.py — accounts & secrets (Supabase)

- **`_get_client()`** — Lazily builds the Supabase client.
- **`encrypt_secret(plaintext)` / `decrypt_secret(ciphertext)`** — Fernet
  encrypt/decrypt for the stored Google token.
- **`get_account_by_email / get_account / get_account_by_google_id`** — Row
  lookups by each key.
- **`link_or_create_google_account(email, google_id)`** — For Google sign-in:
  reuse the Google-linked account, else link the matching email account, else
  create a fresh account.
- **`_normalize_sheet_id(value)`** — Accepts a full Sheets URL or a bare id and
  returns just the id.
- **`update_settings(account_id, settings)`** — Writes only the editable settings
  fields (normalizing a pasted sheet URL).
- **`set_google_token(account_id, token_json)`** — Stores the Google token
  encrypted, or clears it.
- **`get_google_token(account)`** — Returns the decrypted token JSON, or None.
- **`record_usage(account_id, kind, quantity, detail)`** — Inserts one usage event.
- **`usage_summary(account_id)`** — Totals usage per kind.

## outreach-agent/google_auth.py — Google OAuth

- **`_flow()`** — Builds the OAuth flow for the Gmail+Sheets scopes (confidential
  web client; PKCE off by design).
- **`build_auth_url(state)`** — Consent URL requesting offline access (refresh
  token) for background sends.
- **`exchange_code(code)`** — Trades the callback code for token JSON.
- **`get_credentials(account)`** — Builds live `Credentials` from the stored
  token, auto-refreshing (and persisting) when expired; clears a dead token and
  raises a "reconnect" error if refresh permanently fails.
- **`_login_flow()`** — OAuth flow for identity-only "Sign in with Google".
- **`build_login_auth_url(state)`** — Identity consent URL (online access, no
  refresh token).
- **`exchange_login_code(code)`** — Trades the code for `{email, google_id}`.

## outreach-agent/gmail.py — Gmail send & reply detection

- **`_get_service(account)`** — Per-account Gmail API client (never cached —
  thread safety).
- **`send_email(account, to, subject, body)`** — Sends a new email; returns the
  thread id.
- **`_header(headers, name)`** — Case-insensitive header lookup.
- **`send_reply(account, thread_id, to, body)`** — Sends a reply threaded into an
  existing conversation via In-Reply-To/References.
- **`_extract_body(payload)`** — Pulls the plain-text body out of a Gmail message
  payload (recursing into parts).
- **`strip_quoted(text)`** — Trims the quoted-history tail so only what this
  message added remains; preserves inline replies woven between `>` lines.
- **`get_latest_reply(account, thread_id, contact_email)`** — Just the newest
  reply text (or None).
- **`get_latest_reply_with_history(...)`** — Newest message *if it came from the
  contact* (matched by From address, not the SENT label), plus the full prior
  conversation oldest-first for grounding the draft.

## outreach-agent/agent.py — LLM (OpenRouter)

- **`_chat(system, user_prompt)`** — One chat completion with retry on 429 and on
  null-content refusals; meters token usage; routes to a local proxy in dev only.
- **`_sender_context(account)`** — Sender name, meeting purpose, calendar link
  with sensible fallbacks.
- **`generate_outreach_email(account, name, company)`** — Writes a cold email
  (requires a calendar link); splits subject from body on the first newline.
- **`draft_reply(account, name, company, customer_reply, history)`** — Drafts a
  reply grounded in the whole thread and the prospect's newest message.

## outreach-agent/sheets.py — Google Sheets as the lead DB

- **`_get_service(account)`** — Per-account Sheets client (not cached).
- **`_sheet_id(account)`** — The connected sheet id, or a "no sheet" error.
- **`_validate_header(values)`** — Read gate: row 1 must match the expected
  columns as a prefix (min Name+Email); rejects reordered/foreign layouts.
- **`get_all_rows(account)`** — All data rows as `(1-based row_index, padded
  values)`, header validated.
- **`get_pending_rows(account)`** — Approved-but-unsent rows (blank Status).
- **`sent_in_last_24_hours(rows)`** — Counts app-generated send timestamps in the
  trailing 24h (ignores untrusted manual timestamps).
- **`campaign_readiness(account)`** — The sendable rows under the daily cap plus
  the accounting (eligible / sent today / remaining / capped).
- **`get_reply_check_rows(account)`** — Rows worth checking for replies (Sent or
  Replied — a thread can get more than one message).
- **`_row_update_cells(...)`** — Builds the A1-range/value pairs for a partial
  row update.
- **`require_full_header(account)`** — Write gate: row 1 must carry **all nine**
  labels before the app writes into columns D–I (consent to own those columns).
- **`_verify_row_index(account, row_index, expect_email)`** — Guards against stale
  row numbers: re-reads and follows the contact by email, or fails loudly rather
  than writing into the wrong row.
- **`update_row(...)`** — Writes status/thread/sent-at/body/confidence to one row
  (verifying by email first when asked).
- **`append_rows(account, rows)`** — Appends new lead rows to the sheet.

## outreach-agent/send_outreach.py — the send batch

- **`_send_one(account, row_index, name, email, company)`** — The per-contact
  pipeline: draft → send → mark row "Sent" (retry ×3 with backoff). The marking
  happens immediately after the irreversible send; if it permanently fails it
  raises a **labeled** error ("email WAS sent") so the operator fixes the row
  instead of re-emailing.
- **`main(account)`** — Requires the full header, gathers eligible rows, sends
  them concurrently (4 workers), emails a summary, returns per-batch counts.

## outreach-agent/watch_replies.py — reply detection

- **`check_for_replies(account)`** — For each Sent/Replied row: detect a new
  reply, skip if already reviewed (before drafting, to save LLM calls), draft a
  response, queue the review **first**, then mark the sheet "Replied". Rows are
  isolated so one bad thread doesn't stop the rest. Returns
  `{reviews, row_errors}`.
- **`main()`** — CLI entry: `--once` for a single check, otherwise polls every
  5 minutes (standalone script mode).

## outreach-agent/reviews_db.py — review queue (Supabase, per-account)

- **`_get_client()`** — Lazy Supabase client.
- **`find_review_id(account_id, thread_id, customer_reply)`** — Existing review
  id for this exact reply on this thread (keyed on thread+reply so later replies
  aren't swallowed), or None.
- **`add_review(...)`** — Inserts a pending review, deduped via `find_review_id`.
- **`list_pending_reviews(account_id)`** — Pending reviews, oldest first.
- **`get_review(account_id, review_id)`** — One review (account-scoped).
- **`mark_sent(account_id, review_id, sent_body)`** — Marks a review sent, storing
  what was actually sent.
- **`dismiss(account_id, review_id)`** — Marks a review dismissed.

## outreach-agent/leads.py — lead sourcing

- **`_expand_queries(target_description)`** — LLM turns an ICP sentence into 3
  web-search queries aimed at named individuals.
- **`_extract_candidates(target_description, results)`** — LLM extracts real named
  people (with a derivable email) from search results as JSON; never invents names.
- **`find_leads(account, target_description, limit)`** — Runs the searches, extracts
  candidates, dedupes against the sheet (and within the batch), appends new
  "Needs verification" rows; returns found/added/skipped counts.

## outreach-agent/search.py
- **`tavily_search(query, max_results)`** — Web search via Tavily; meters usage;
  returns `[{title, url, content}]`.

## outreach-agent/drive.py
- **`list_spreadsheets(account)`** — Lists the connected user's spreadsheets
  (id + name, most-recent first); read-only, never reads contents.

## outreach-agent/usage.py — per-account metering
- **`set_account(account_id)`** — Sets the account that current usage is charged to.
- **`run_as(account_id, fn, …)`** — Runs `fn` with usage attributed to an account
  (needed across worker threads that don't inherit contextvars).
- **`record(kind, quantity, detail)`** — Records a usage event best-effort
  (metering never breaks the metered action).

## outreach-agent/notify.py
- **`notify(account, subject, message)`** — Emails the account's own notify
  address from its own Gmail; silent no-op if none set, never raises.

## outreach-agent/ratelimit.py
- **`check(key, limit, window_seconds)`** — In-memory fixed-window limiter;
  returns True if allowed (and records the hit), False if over. Single-process only.

## outreach-agent/config.py
- Loads env/config constants (secret keys, Supabase URL, OAuth redirect URIs,
  scopes, model names, `DAILY_SEND_LIMIT`, `SHEET_RANGE`). No functions.

---

## Frontend

- **`static/*.html`** — The app UI (login, settings, outreach, leads), Tailwind
  via CDN, vanilla JS calling the `/api/*` routes above. `login.html` is the
  only entry point (`/signup` redirects to it via Google); it sanitizes the
  `next=` redirect to same-site paths only before forwarding it to
  `/auth/google/login`, which is what actually sets the redirect-target cookie.
- **`site/`** — The React/Vite marketing landing page (source), built into
  **`static/landing/`** and served at `/` for logged-out visitors. Components:
  `hero.tsx` (nav, hero, how-it-works), `mockup.tsx` (review-queue mockup),
  `pricing.tsx`, `sections.tsx` (trust, integrations, security, FAQ, CTA,
  footer), `primitives.tsx` (logo, buttons, noise filters).
- **`landing/`** — A separate standalone design exercise (light theme, unrelated
  to the app); not wired into the server.
