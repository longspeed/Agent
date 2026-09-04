# Sendkeep

The Gmail promise and follow-up memory layer for people sending 15–50 emails a
day who already have a sending workflow. Sendkeep reads a bounded set of recent
Gmail Sent threads, extracts promised actions, and keeps one follow-up visible.
Reply in Gmail or Gemini; Sendkeep is not a second reply inbox. A Google Sheet
is optional for first-touch sending and is not the source of truth.

The core workflow is read-only against Gmail Sent. Optional first-touch sending
is capped on purpose and remains manual by default. If a follow-up send result
is ambiguous, the worker verifies the exact message in Gmail before closing it;
it never silently retries a possibly-sent message.

Multi-tenant: one running instance serves any number of customers, each
with their own login, their own connected Gmail/Sheets, and their own
review queue and usage metering in Supabase. No tenant can see another's
data.

## How it works

```
                    ┌─────────────────────────────────────────┐
                    │          Google Sheet (optional)          │
                    │     (first-touch list + status mirror)    │
                    └──────────────────┬────────────────────────┘
                                        │ read / write (per-row status)
                                        ▼
┌──────────────┐   drafts   ┌─────────────────────┐   sends via   ┌──────────┐
│ Lead Agent   │ (optional) │   server.py (FastAPI) │──────────────▶│  Gmail   │
│ (web search,  │            │   outreach-agent/*    │◀──────────────│ (OAuth,  │
│  qualifies    │            │   review queue, plans, │   watches    │  per-    │
│  contacts)    │            │   suppressions, usage   │   for reply  │  account)│
└──────────────┘            └──────────┬─────────────┘              └──────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
             ┌────────────┐     ┌──────────────┐     ┌──────────────┐
             │  app/       │     │  site/        │     │  static/      │
             │ (Settings,  │     │ (logged-out   │     │ (legacy pages │
             │  Getting    │     │  landing page,│     │  not yet      │
             │  Started —  │     │  served at /  │     │  migrated to  │
             │  React,     │     │  when logged  │     │  React:       │
             │  builds to  │     │  out — React, │     │  outreach,    │
             │  static/app)│     │  builds to    │     │  leads, login)│
             └────────────┘     │  static/      │     └──────────────┘
                                 │  landing)     │
                                 └──────────────┘
                                        │
                                        ▼
                             ┌─────────────────────┐
                             │  watch_replies.py     │
                             │  (background worker,   │
                             │   every 5 min, all      │
                             │   accounts, sequential) │
                             └─────────────────────┘
                                        │
                                        ▼
                              ┌───────────────────┐
                              │  Supabase (Postgres)│
                              │  accounts, reviews,  │
                              │  drafts, usage, RLS   │
                              │  on every table       │
                              └───────────────────┘
```

The core layer does not depend on how a message was sent. A follow-up is
cancelled if a reply, bounce, opt-out, or operator dismissal appears before
send. Reply composition stays in Gmail; the optional draft lane is secondary.

## Quickstart

### Local demo, no credentials required

Use this path first when you are changing the UI, queue states, or developer
workflow. It does not connect to Google, Supabase, an LLM provider, or any real
mailbox:

```bash
pip install -r outreach-agent/requirements.txt
python dev_server.py
```

Open `http://127.0.0.1:8000/demo`. The page contains seeded warm replies,
promised follow-ups, a reset action, and a protected-send test that must return
`409`. The local API contract is available at
`http://127.0.0.1:8000/api/docs` and `/api/openapi.json`.

Restarting the demo restores the seed data. The demo is a separate app from
`server.py`, so it cannot accidentally inherit production credentials or Gmail
tokens.

### Production-shaped app

Full setup (Google Cloud OAuth client, Supabase schema, `.env`) is in
[`outreach-agent/README.md`](outreach-agent/README.md) — it's the
authoritative setup guide, this section is just the map:

```bash
pip install -r outreach-agent/requirements.txt
cp outreach-agent/.env.example outreach-agent/.env   # fill in the values
python -m uvicorn server:app --port 8000
```

Customers then self-onboard at `/signup` — no code changes needed per
customer. See "Sharing Sendkeep" below.

Two frontend projects build into `static/`:

```bash
cd app && npm run build    # Settings, Getting Started -> static/app/
cd site && npm run build   # logged-out landing page   -> static/landing/
python check_build_freshness.py   # confirms both match current source
```

## Tests

```bash
python tests/test_outreach_agent.py          # network-free main suite
python tests/test_build_freshness.py         # generated-bundle checks
cd outreach-agent && python -m unittest discover tests   # outreach-agent suite
```

All three pass with sockets blocked:

```bash
python -c "import socket,unittest;socket.socket.connect=lambda *a:(_ for _ in ()).throw(AssertionError('network'));unittest.TextTestRunner().run(unittest.TestLoader().discover('outreach-agent/tests'))"
```

Typecheck: `python -m py_compile server.py outreach-agent/*.py` (syntax
only — no static type checker is configured).

## Deploying

A systemd unit template for the web server and the background reply
watcher live in `outreach-agent/deploy/`. Both need a named Cloudflare
Tunnel in front (not a quick tunnel — see `VALIDATION-WEEK.md`), since
`server.py` trusts `CF-Connecting-IP` for rate limiting and binds
`127.0.0.1` only.

The watcher is an independent process and must be running continuously for
reply detection to work when nobody has `/outreach` open. Set
`WORKER_TELEMETRY_ENABLED=1` and a long random `WORKER_HEALTH_TOKEN` in the
shared `.env`. The worker records each cycle in `worker_runs` and each account
outcome in `worker_events`; `GET /internal/health/worker` accepts the token in
`X-Worker-Health-Token` and is intended for an external uptime monitor. A
fresh worker heartbeat with an account error means the worker is alive but that
Gmail connection needs attention. A stale health response means the worker
process or host needs attention.

The database schema is versioned in `supabase/migrations/`; that directory is
the only DDL source of truth. Rebuild a local database with `supabase db reset
--local --no-seed`, then run `supabase test db --local`. For the hosted project,
use the protected **Deploy database migrations** workflow on `main`; it previews
pending DDL and requires an explicit production confirmation plus environment
approval. Never paste migration fragments into application code or run a local
reset against production. `tracked_threads` is the durable Gmail-thread watch
list, so a deleted or unavailable Sheet row no longer removes a sent
conversation from reply coverage. `commitments` stores conservative,
reviewable promises found in replies; detected dates and actions appear in the
Outreach desk and require confirmation before they become confirmed work. The
optional `estimated_value` field turns confirmed promises into account-scoped
pipeline at stake; it is operator-entered and never inferred from message text.

## Project docs

- [`docs/development.md`](docs/development.md) — credential-free local demo,
  runtime boundaries, CLI smoke checks, and verification commands
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — safe local workflow and verification
  expectations
- [`SECURITY.md`](SECURITY.md) — sensitive surfaces and vulnerability reporting
- [`CHANGELOG.md`](CHANGELOG.md) — unreleased developer workflow changes
- [`docs/operations.md`](docs/operations.md) — preflight, migrations, worker
  health, deployment, and release verification
- [`app/README.md`](app/README.md) and [`site/README.md`](site/README.md) —
  frontend-specific development and build instructions
- [`outreach-agent/README.md`](outreach-agent/README.md) — agent setup,
  canonical Supabase schema workflow, and what degrades until it runs
- [`TODOS.md`](TODOS.md) — deferred work, with the reasoning kept, not just
  a bullet
- [`BUGS.md`](BUGS.md) — live bug log from manual QA passes
- [`VALIDATION-WEEK.md`](VALIDATION-WEEK.md) — operational runbook for a
-  historical Sheet-first validation context
- [`docs/pilot-validation.md`](docs/pilot-validation.md) — current Gmail-first
  pilot, evidence, interview, and safety checklist
- [`docs/founder-dogfood.md`](docs/founder-dogfood.md) — founder-owned
  distribution loop, operator log, and agency expansion criteria
- [`docs/founder-dogfood-messages.md`](docs/founder-dogfood-messages.md) —
  reviewed Gmail-native pilot message starting points
- [`FUNCTIONS.md`](FUNCTIONS.md) — every backend function, one line each

## Sharing Sendkeep with another person

Sendkeep is multi-tenant: one running instance serves any number of
customers, each with their own login, their own connected Gmail, their own
Google Sheet, and their own review queue and usage metering in Supabase.

To give someone access, just send them the URL — they onboard themselves
with no code or config changes:

1. **Sign in** at `/login` with **Continue with Google** — that's the whole
   account, no separate password to set.
2. **Connect Google** from the Settings page — an in-browser OAuth consent
   for their Gmail and Sheets. Their token is stored encrypted on their
   account row; it never touches disk. (While the app's Google OAuth consent
   screen is in "Testing" status, you must first add their Google account as
   a test user in Google Cloud Console → APIs & Services → OAuth consent
   screen.)
3. **Fill in Settings**: their Google Sheet ID, sender name, calendar
   booking link, meeting purpose, notification email, send mode, and follow-up
   delay (1-14 business days). Manual
   mode queues drafts for review; Auto mode sends only newly generated drafts
   after the operator confirms Prepare.

Everything they see and send is scoped to their account — reviews and usage
rows carry an `account_id` in Supabase, background jobs are only visible to
the account that started them, and no tenant can read another's data.

## License

MIT — see [`LICENSE`](LICENSE).
