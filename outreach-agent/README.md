# Outreach Agent

Sendkeep is a Gmail promise and follow-up memory layer. It discovers a bounded
window of recent Sent threads, watches conversations continuously, extracts
promised actions, and keeps one follow-up visible. Reply in Gmail or Gemini;
the optional reply-draft lane is secondary. An optional Google Sheet supports
the legacy first-touch lane; it is not required for Gmail monitoring.

When `VOICE_FEEDBACK_ENABLED=1` (the production default), sent drafts that a
human approved or edited are reduced to a bounded style hint for that same
account's future drafts. Unchanged approvals are weak signals; edits are
counted separately as stronger signals. AI-rewritten sends are excluded. The
hint contains aggregate shape such as approximate length and opening style,
never raw prospect text, examples, or a cross-account training corpus.

The app is multi-tenant: each customer signs in with "Continue with Google",
connects their own Gmail (and optionally Sheets) via a separate in-browser
"Connect Google" button, and sees only their own data. The steps below
set up the shared app once; individual customers onboard themselves through
the web UI.

Note the login flow is separate from the Gmail/Sheets connection flow —
"Continue with Google" only proves who you are (email identity, no Gmail
access granted); customers still connect Gmail from Settings afterward, and
it doesn't have to be the same Google account they signed in with.

## 1. Install dependencies

```
pip install -r requirements.txt
```

## 2. Google Cloud setup (the app's shared OAuth client)

1. Go to https://console.cloud.google.com/ and create a new project.
2. Enable the **Gmail API**, **Google Sheets API**, and **Google Drive API**
   for it (APIs & Services > Library). Drive is read-only (file names/ids
   only, never contents) and only powers the "choose your sheet from a
   list" picker in Settings.
3. Go to APIs & Services > Credentials > Create Credentials > OAuth client ID.
   - Application type: **Web application**
   - Add **both** `http://localhost:8000/api/google/callback` (incremental
     Gmail, sending, or optional Sheets grant) and `http://localhost:8000/auth/google/callback` ("Sign in
     with Google") as **Authorized redirect URIs** — plus your production
     callback URLs when you deploy; set `GOOGLE_OAUTH_REDIRECT_URI` and
     `GOOGLE_LOGIN_REDIRECT_URI` in `.env` to match.
   - If prompted, configure the OAuth consent screen first (External; while
     the consent screen is in "Testing" status, every customer's Google
     account must be added as a test user).
   - In **Google Auth Platform → Data Access**, configure the scopes the app
     may request incrementally: `openid`, `userinfo.email`, `gmail.readonly`,
     `gmail.send`, `spreadsheets`, and `drive.metadata.readonly`. Do not add
     `gmail.modify`: monitoring does not need edit or delete permission.
4. Download the credentials JSON and save it as `credentials.json` in this
   folder. This is the *app's* identity with Google — customers each grant it
   access to their own account, and their tokens are stored encrypted in the
   database (no `token.json` on disk).

## 3. Supabase setup

The database schema is versioned in `../supabase/migrations`. Those migration
files are the only DDL source of truth; do not paste schema fragments into
application code or this README.

For a new local database:

```powershell
supabase start
supabase db reset --local --no-seed
supabase test db --local
```

The reset reapplies every migration and the final command runs the pgTAP schema
contract in `../supabase/tests`. Never run `db reset` against a linked
production project.

For the existing hosted project, use the manual **Deploy database migrations**
GitHub Actions workflow. Configure its protected `production` environment with
`SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_PASSWORD`, and
`SUPABASE_PROJECT_ID`. The workflow links the project, previews the pending
DDL, and then runs `supabase db push`. The baseline migration is intentionally
idempotent so the first push reconciles the previously hand-maintained schema
without dropping customer tables or rows. Every later schema change must be a
new forward-only timestamped migration.

Production starts with strict schema enforcement automatically
(`APP_ENV=production`). Local development reports drift as warnings; set
`SCHEMA_ENFORCEMENT=strict` locally to reproduce the production gate.

## 4. Language model API key

You need at least one. They are tried in order and the first that answers
serves the request, so a blown daily quota degrades instead of stopping. All
three speak the same API shape, so adding one is a key in `.env`.

| Provider | Free allowance | Get a key |
| --- | --- | --- |
| **Groq** (recommended primary) | ~14,400 requests/day, 30 RPM. No card. Open-weight models only. | https://console.groq.com/keys |
| **Google AI Studio** | ~1,500 requests/day, 15 RPM. Gemini 2.5 Flash. | https://aistudio.google.com/apikey |
| **OpenRouter** | 50 requests/day on an account that has never been funded; 1,000/day once you have ever bought $10 of credit. | https://openrouter.ai/settings/keys |

Read that OpenRouter row before making it your primary: at 25 sends/day plus
the retry in `agent.py`, one active customer exhausts 50 requests outright.

Keys are shared by all accounts; per-account consumption is recorded in
`usage_events` (see the Usage panel in Settings), against whichever model
actually served each call.

**Free tiers and your privacy policy.** A free tier generally allows the
provider to train on what you send it. First-touch drafting is fine there — the
prompt holds a name, a company, a public research note and your own pitch. Reply
drafting is not: its prompt contains the prospect's own words. So replies are
routed only to a provider you have marked as paid (`GROQ_TIER=paid`,
`GEMINI_TIER=paid`, or a non-`:free` OpenRouter model) and are never failed over
to a free one. With no paid provider configured, replies do run on a free tier
and `providers.py` warns at startup; `/privacy` and `/dpa` disclose it, and
`REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT=1` refuses instead. `GET
/api/llm-providers` reports what is live.

## 5. Configure environment

```
cp .env.example .env
```

Fill in:
- At least one of `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` — step 4
- Optionally `GROQ_MODEL` / `GEMINI_MODEL` / `OPENROUTER_MODEL` to override the
  default model, and `{NAME}_TIER=free|paid` to declare the data terms your key
  is on. Check the default model ids against the provider's current list —
  models get retired.
- `LLM_PROVIDER_ORDER` — optional; reorders the chain and doubles as an
  allowlist, so `LLM_PROVIDER_ORDER=groq` pins everything to Groq
- `APP_SECRET_KEY` — random key used to sign login sessions and encrypt
  stored Google tokens, e.g.
  `python -c "import secrets; print(secrets.token_hex(32))"`. Changing it
  logs everyone out and invalidates stored Google tokens.
- `SESSION_SIGNING_KEY` — optional. Signs sessions/OAuth-state/unsubscribe
  tokens separately from `APP_SECRET_KEY`, so a leak of one secret doesn't
  also hand over Google-token decryption (or vice versa). Defaults to
  `APP_SECRET_KEY` when unset. To actually split them, set it to a
  different random value: this logs everyone out (sessions are short-lived)
  but does **not** break unsubscribe links already sent — those verify
  against the previous value too, forever.
- `TAVILY_API_KEY` — only needed for the lead sourcing agent
- `SUPABASE_URL` / `SUPABASE_SECRET_KEY` — from your Supabase project's
  Settings > API Keys page (the **secret** key, not the publishable one)
- `GOOGLE_OAUTH_REDIRECT_URI` / `GOOGLE_LOGIN_REDIRECT_URI` — must both match
  redirect URIs registered on the OAuth client in step 2

## 6. Run

From the repo root:

```
python -m uvicorn server:app --port 8000
```

Each customer then onboards themselves at `http://localhost:8000/login`:

1. Sign in with **Continue with Google** (creates the account on first use)
2. On the Settings page, click **Connect Gmail**. This first grant is Gmail
   read-only access for bounded Sent monitoring.
3. Enable Gmail sending only if they want Sendkeep to send approved messages.
   Connect Sheets & Drive separately only for the optional first-touch lane.
4. Click **Choose sheet** to pick their lead sheet from a list of their
   Google Sheets (a manual ID/URL paste is still available as a fallback)
5. Fill in sender name, calendar booking link, meeting purpose, and
   notification email
6. Use the Outreach and Leads pages as before — everything they see and send
   is scoped to their own account

The sheet needs this header row in its **first tab**:

| Name | Email | Company | Status | ThreadID | SentAt | EmailBody | LeadReason | EmailConfidence |
|------|-------|---------|--------|----------|--------|-----------|------------|-----------------|

### CLI (optional, per account)

`send_outreach.py` runs standalone, scoped to one account by email:

```
python send_outreach.py you@company.com
```

### Background reply watcher

`watch_replies.py` polls **every account with a configured sheet or an active
durable Gmail thread** in one process, sequentially, every 5 minutes:

```
python watch_replies.py --once   # one cycle across all accounts, then exit
python watch_replies.py          # every 5 minutes, forever
```

To restrict a run to one account for debugging (no worker heartbeat is
written in this mode, so it can't be mistaken for the real worker running):

```
python watch_replies.py you@company.com --once
```

For an always-on deployment, run it as its own service rather than a
foreground process — see `deploy/sendkeep-worker.service` for a systemd
unit template. It's independent of the `uvicorn` process: either can restart
without affecting the other.

Each account's `worker_heartbeat_at` and `last_error` columns record when the
worker last looked at that account and what happened — a stale heartbeat
means the worker isn't reaching that account (down, or stuck earlier in the
loop); a fresh heartbeat with a `last_error` set means the worker is running
fine but that account needs attention (most commonly "Google disconnected —
reconnect in Settings", since Testing-mode OAuth tokens expire every 7 days).

Reviewing a drafted reply still happens in the dashboard — this tool never
sends replies automatically.

### Durable thread tracking and reply commitments

The Sheet is a customer-facing mirror, not the worker's source of truth. New
sends and Sheet backfills register Gmail thread IDs in `tracked_threads`, and
the watcher continues checking those threads if the Sheet row is moved,
deleted, or temporarily unavailable.

When a contact reply contains a clear first-person promise such as “I'll send
the deck by Friday”, the worker stores a conservative candidate in
`commitments`. The Outreach page shows the evidence and detected date. The
operator must confirm or dismiss it. Once confirmed, the worker moves the
promise into the due queue at its detected date and sends a notification for a
manual check; detection never sends outbound mail or schedules outreach by
itself. The optional operator-entered value-at-stake field supports the
explicit promise-completed outcome handoff; values are never inferred from
email text. A dismissal reason records
whether an operator rejected an extraction as incorrect or merely cleared a
real due item. Only explicit incorrect detections count against the promise
precision metric; above 20%, new Gmail-monitor onboarding is paused.

Worker liveness is recorded in `worker_runs` and `worker_events` by the
continuous process. Configure `WORKER_HEALTH_TOKEN` so an external monitor can poll
`/internal/health/worker`.

All columns, indexes, RLS settings, and nullability rules described above are
owned by the canonical Supabase baseline in section 3. Durable Gmail tracking
can discover a reply after its Sheet row was deleted, or for an account that
does not use a Sheet at all, so `reviews.row_index` is deliberately nullable.
The web and worker use the Supabase service role; browser clients must not query
these tables directly. A production boot with schema drift fails before serving
traffic instead of silently running with weaker guarantees.

The promise graph also uses the account-scoped `worker_events` history for
operator-confirmed sales outcomes. The pipeline view can record
`meeting_booked`, `deal_won`, or `deal_lost` only after a human clicks the
corresponding action. A reply, a promise, or a calendar URL is never treated
as a booked meeting automatically. This keeps the evidence dashboard useful
without turning an unverified inference into a sales claim.

## Notes

- Re-running the send only targets rows with an empty `Status` column, so
  it's safe to add new rows to the sheet and re-run anytime.
- Reply-watching only checks rows with `Status = Sent`; once a reply is
  detected the row is marked `Replied` and won't be checked again.
- Google tokens are encrypted at rest with a key derived from
  `APP_SECRET_KEY`; an account can disconnect Google from Settings at any
  time.
- Accounts that connected Google before the Drive scope was added won't have
  it on their stored token; the sheet picker will prompt them to click
  **Reconnect** once to grant it (their outreach/leads data is unaffected).
