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
    calendar_booking_link text,
    notify_email text,
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
    status text not null default 'pending',
    created_at timestamptz not null default now()
);
create index reviews_account_status_idx on public.reviews (account_id, status);

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
-- then run the two create table statements above, their indexes, and their
-- `enable row level security` lines.
```

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

## 4. OpenRouter API key

Sign up at https://openrouter.ai/ and create an API key
(https://openrouter.ai/settings/keys). The default model in `.env.example`,
`openai/gpt-oss-20b:free`, is a free model — no payment needed to get
started. This key is shared by all accounts; per-account consumption is
recorded in `usage_events` (see the Usage panel in Settings).

## 5. Configure environment

```
cp .env.example .env
```

Fill in:
- `OPENROUTER_API_KEY` — from step 4
- `OPENROUTER_MODEL` — leave as the default free model, or swap for another
- `APP_SECRET_KEY` — random key used to sign login sessions and encrypt
  stored Google tokens, e.g.
  `python -c "import secrets; print(secrets.token_hex(32))"`. Changing it
  logs everyone out and invalidates stored Google tokens.
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

The batch scripts also run standalone, scoped to one account by email:

```
python send_outreach.py you@company.com
python watch_replies.py you@company.com --once
python watch_replies.py you@company.com        # poll every 5 minutes
```

When a customer replies, the account's notification email gets the reply plus
an AI-drafted response. Review it in the dashboard and send it from there —
this tool never sends replies automatically.

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
