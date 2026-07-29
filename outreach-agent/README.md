# Outreach Agent

Reads customers from a Google Sheet, sends each a personalized outreach email
via Gmail, notifies you when the batch is done, and watches for replies —
drafting an AI response you review and send yourself.

The app is multi-tenant: each customer signs up (with a password, or via
"Continue with Google"), connects their own Gmail/Sheets via an in-browser
"Connect Google" button, picks their own sheet, and sees only their own
data. The steps below set up the shared app once; individual customers
onboard themselves through the web UI.

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
   - Add **both** `http://localhost:8000/api/google/callback` (Gmail/Sheets
     connect) and `http://localhost:8000/auth/google/callback` ("Sign in
     with Google") as **Authorized redirect URIs** — plus your production
     callback URLs when you deploy; set `GOOGLE_OAUTH_REDIRECT_URI` and
     `GOOGLE_LOGIN_REDIRECT_URI` in `.env` to match.
   - If prompted, configure the OAuth consent screen first (External; while
     the consent screen is in "Testing" status, every customer's Google
     account must be added as a test user).
4. Download the credentials JSON and save it as `credentials.json` in this
   folder. This is the *app's* identity with Google — customers each grant it
   access to their own account, and their tokens are stored encrypted in the
   database (no `token.json` on disk).

## 3. Supabase setup

Create a project at https://supabase.com/ and run this in the SQL editor
(skip if the migration `multi_tenant_schema` has already been applied):

```sql
create extension if not exists pgcrypto;

create table public.accounts (
    id uuid primary key default gen_random_uuid(),
    email text not null unique,
    -- Nullable: accounts created via "Sign in with Google" have no password
    -- (accounts_db.link_or_create_google_account inserts password_hash = NULL).
    password_hash text,
    -- Set only for Google-linked accounts; used by get_account_by_google_id.
    google_id text unique,
    google_token text,
    google_sheet_id text,
    sender_name text not null default 'the team',
    sender_company text not null default '',
    meeting_purpose text not null default 'a quick intro call to see if there''s a fit to work together',
    -- Free-form sender preferences injected into the writing prompt (tone,
    -- length, things to always mention/avoid, language).
    custom_instructions text not null default '',
    calendar_booking_link text,
    notify_email text,
    -- How many hard bounces the operator has reviewed and chosen to continue
    -- past (bounces.acknowledge). Lets a bounce pause be cleared without
    -- deleting rows from the sheet; new bounces push the count above it and
    -- pause again. Reads as 0 if absent, so an un-migrated database keeps the
    -- pre-acknowledgement behaviour instead of erroring.
    bounce_ack_count integer not null default 0,
    -- Set by the background reply-watcher (watch_replies.py) once per
    -- account per cycle, success or failure -- the only way to tell "the
    -- worker is alive for this account" from "the worker is down" without
    -- reading process logs. last_error is cleared (NULL) on a successful
    -- check, so a set value always means "still true as of the heartbeat
    -- above," not "happened once, ages ago."
    worker_heartbeat_at timestamptz,
    last_error text,
    created_at timestamptz not null default now()
);

create table public.reviews (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    row_index integer not null,
    name text not null default '',
    email text not null default '',
    thread_id text not null default '',
    customer_reply text not null default '',
    draft_reply text not null default '',
    -- Frozen copy of what the model proposed. draft_reply above is mutable:
    -- mark_sent replaces it with what the operator actually sent. The diff
    -- between the two is the only record of how this sender's voice differs
    -- from the model's, and it cannot be reconstructed after the fact.
    original_draft_reply text,
    status text not null default 'pending',
    gmail_message_id text,
    -- True when the Send-As alias lookup behind sender classification failed
    -- and fell back to just the primary address (see gmail.get_own_addresses).
    -- A degraded lookup under-recognizes "ours", which can misclassify the
    -- operator's own alias reply as a stranger -- this makes that visible on
    -- the review card instead of only in a log.
    degraded_classification boolean not null default false,
    created_at timestamptz not null default now()
);
create index reviews_account_status_idx on public.reviews (account_id, status);
-- Dedupe guard. Keyed on Gmail's own message id, never the reply body: two
-- identical short replies collide, a long quoted chain 414s as a PostgREST GET
-- parameter, and btree caps index entries near 2704 bytes so a body column
-- could not carry this index at all. Nullable because rows written before the
-- column existed have no id -- Postgres treats NULLs as distinct in a unique
-- index, so legacy rows neither collide with each other nor block new inserts.
create unique index if not exists reviews_thread_message_idx
    on public.reviews (account_id, thread_id, gmail_message_id);

create table public.usage_events (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    kind text not null,
    detail text not null default '',
    quantity integer not null default 1,
    created_at timestamptz not null default now()
);
create index usage_events_account_idx on public.usage_events (account_id, created_at);

-- Outreach emails awaiting human approval. Nothing is emailed from here until
-- someone presses Send on the /outreach page.
create table public.outreach_drafts (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    row_index integer not null,
    name text not null default '',
    email text not null default '',
    company text not null default '',
    subject text not null default '',
    body text not null default '',
    -- Frozen copies of what the model wrote. subject/body above are mutable:
    -- mark_sent replaces them with the copy the operator approved. Keeping both
    -- is what makes an edit a labelled example rather than a deletion.
    original_subject text,
    original_body text,
    status text not null default 'pending',  -- pending | sent | discarded
    created_at timestamptz not null default now()
);
create index outreach_drafts_account_status_idx on public.outreach_drafts (account_id, status);
-- Dedupe guard: preparing drafts twice (double-click, overlapping jobs) must
-- never queue the same sheet row twice. Partial, so a row can be drafted again
-- after an earlier draft was sent or discarded.
create unique index outreach_drafts_pending_row_idx
    on public.outreach_drafts (account_id, row_index) where status = 'pending';

-- Addresses that asked to stop. Checked when building a batch AND again
-- immediately before each send.
create table public.suppressions (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    email text not null,
    source text not null default 'unsubscribe_link',
    created_at timestamptz not null default now(),
    unique (account_id, email)
);

alter table public.accounts enable row level security;
alter table public.reviews enable row level security;
alter table public.usage_events enable row level security;
alter table public.outreach_drafts enable row level security;
alter table public.suppressions enable row level security;
```

Upgrading an existing project (the tables above are new as of 2026-07-24):

```sql
alter table public.accounts add column if not exists sender_company text not null default '';
alter table public.accounts add column if not exists custom_instructions text not null default '';
-- then run the two create table statements above, their indexes, and their
-- `enable row level security` lines.
```

For the bounce-pause acknowledgement (new as of 2026-07-26). Until this runs,
everything works as before except the "Acknowledge and keep sending" button,
which returns a 409 naming this statement:

```sql
alter table public.accounts add column if not exists bounce_ack_count integer not null default 0;
```

For the daily-cap send log (also 2026-07-26). The cap used to be counted from the
sheet's SentAt column, which the operator can edit — deleting the Sent rows,
clearing that column, or changing its date format all reset the allowance. It is
now counted from `outreach_drafts`, which needs a send timestamp. **Run this
before the next batch:** without it every send raises, because `mark_sent` writes
the column.

```sql
alter table public.outreach_drafts add column if not exists sent_at timestamptz;
update public.outreach_drafts set sent_at = created_at where status = 'sent' and sent_at is null;
create index if not exists outreach_drafts_sent_at_idx
    on public.outreach_drafts (account_id, status, sent_at);
```

For plans and billing (new as of 2026-07-26). Until this runs, `check_schema()`
warns at boot and every account is treated as the free trial: paid customers
silently get trial quotas, and the Stripe webhook has nowhere to write the plan
it just collected money for.

The default is `'trial'`, which is deliberate and is the whole decision in this
migration. `not null default 'trial'` backfills every existing row to the free
tier, so the day this lands, current accounts drop to trial quotas (100 drafts
and 50 sourced leads) and are refused past them. That is truthful to billing --
nobody has paid -- but it is a downgrade for people mid-campaign, so either
send them to checkout first or backfill the ones you intend to grandfather:

```sql
alter table public.accounts add column if not exists plan text not null default 'trial';

-- Optional: grandfather specific existing accounts instead of dropping them to
-- the trial. Run this BEFORE announcing the change, not after the first refusal.
-- update public.accounts set plan = 'pilot' where email in ('someone@example.com');
```

`plan` is a plain text column rather than an enum on purpose: adding a tier
should not require a migration, and `plans.get()` already falls back to the
trial for any value it does not recognize, so an unknown string fails toward
the smallest entitlement rather than toward free unlimited service.

If your project predates "Sign in with Google", also bring the accounts table
in line with the code (harmless if the column already exists):

```sql
alter table public.accounts add column if not exists google_id text;
create unique index if not exists accounts_google_id_key on public.accounts (google_id);
-- Google-linked accounts have no password, so this column cannot be NOT NULL.
alter table public.accounts alter column password_hash drop not null;
```

RLS is enabled with no policies: the publishable/anon key can read nothing,
and the backend (which uses the secret key and bypasses RLS) scopes every
query by `account_id`.

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

Each customer then onboards themselves at `http://localhost:8000/signup`:

1. Create an account (email + password)
2. On the Settings page, click **Connect Google** and grant Gmail + Sheets +
   Drive (read-only) access
3. Click **Choose sheet** to pick their lead sheet from a list of their
   Google Sheets (a manual ID/URL paste is still available as a fallback)
4. Fill in sender name, calendar booking link, meeting purpose, and
   notification email
5. Use the Outreach and Leads pages as before — everything they see and send
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

`watch_replies.py` polls **every account with a configured sheet** in one
process, sequentially, every 5 minutes:

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
foreground process — see `deploy/agent-hub-worker.service` for a systemd
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
