# Sendkeep

The reply queue for Gmail outbound. Sendkeep drafts first-touch emails, watches
the threads, and drafts replies when prospects write back. Manual review is the
default; auto-send can be enabled for first-touch outreach once the sender
trusts the list and settings. Replies still stay in the review queue.

Multi-tenant: one running instance serves any number of customers, each
with their own login, their own connected Gmail/Sheets, and their own
review queue and usage metering in Supabase. No tenant can see another's
data.

## How it works

```
                    ┌─────────────────────────────────────────┐
                    │              Google Sheet                │
                    │   (customer-owned lead list + status)     │
                    └──────────────────┬────────────────────────┘
                                        │ read / write (per-row status)
                                        ▼
┌──────────────┐   drafts   ┌─────────────────────┐   sends via   ┌──────────┐
│  Lead Agent   │──────────▶│   server.py (FastAPI) │──────────────▶│  Gmail   │
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

Every account approves every send. There's no code path where an email
leaves without a human clicking send — see `outreach-agent/send_outreach.py`
and `static/security.html`.

## Quickstart

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
python tests/test_outreach_agent.py          # 338 cases, no network, no real credentials
python tests/test_build_freshness.py         # 6 cases
cd outreach-agent && python -m unittest discover tests   # 18 cases, run from this dir
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

## Project docs

- [`outreach-agent/README.md`](outreach-agent/README.md) — full setup, one
  migration explained per step, what degrades until it runs
- [`TODOS.md`](TODOS.md) — deferred work, with the reasoning kept, not just
  a bullet
- [`BUGS.md`](BUGS.md) — live bug log from manual QA passes
- [`VALIDATION-WEEK.md`](VALIDATION-WEEK.md) — operational runbook for a
  live demand-validation pass
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
   booking link, meeting purpose, notification email, and send mode. Manual
   mode queues drafts for review; Auto mode sends only newly generated drafts
   after the operator confirms Prepare.

Everything they see and send is scoped to their account — reviews and usage
rows carry an `account_id` in Supabase, background jobs are only visible to
the account that started them, and no tenant can read another's data.

## License

MIT — see [`LICENSE`](LICENSE).
