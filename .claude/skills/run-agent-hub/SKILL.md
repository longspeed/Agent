---
name: run-agent-hub
description: Build, run, and drive Agent Hub (FastAPI + static-HTML web app for outreach-agent and lead-sourcing agent). Use when asked to start Agent Hub, run the outreach-agent server, take a screenshot of the hub/outreach/leads pages, or interact with the running app.
---

Agent Hub is a password-gated FastAPI server (`server.py`, repo root) that
serves three static pages (`/`, `/outreach`, `/leads`) and JSON APIs under
`/api/*`, backed by Python modules in `outreach-agent/` (Google Sheets,
Gmail, OpenRouter, Tavily). Drive it with the Puppeteer REPL driver at
`.claude/skills/run-agent-hub/driver.mjs` — there is no `chromium-cli` on
this (Windows) machine, so this driver is the harness. All paths below are
relative to the repo root (`c:\Users\HP\Desktop\solve`).

## Prerequisites

Already installed and verified working this session — nothing new needed
on a machine that looks like this one:

```bash
python --version   # Python 3.13.5
node --version     # Node with npm
```

```bash
pip install -r outreach-agent/requirements.txt   # fastapi, uvicorn, google-api libs, requests, python-dotenv, schedule
npm install puppeteer                            # already in package.json; run.mjs/screenshot.mjs/driver.mjs all use it
```

No `xvfb`/`apt-get` — Puppeteer's bundled headless Chromium runs directly
on Windows.

## Setup

Real secrets already live in `outreach-agent/.env` and are not something
this skill can generate — `credentials.json`/`token.json` (Google OAuth,
created via the one-time browser consent flow — see
`outreach-agent/README.md` if they're ever missing) and `.env` with:

```
OPENROUTER_API_KEY=   # required — email drafting + lead qualification
NOTIFY_EMAIL=         # required
GOOGLE_SHEET_ID=      # required
CALENDAR_BOOKING_LINK= # required
APP_PASSWORD=         # required — dashboard login
APP_SECRET_KEY=       # required — session cookie signing
TAVILY_API_KEY=       # optional — only /leads search needs this; app runs fine without it
```

If `.env` already has real values (it does in this repo), there's nothing
to configure. No separate build step — it's interpreted Python + static
HTML, nothing to compile.

## Run (agent path)

Start the server in the background from the **repo root** (it must be the
cwd — `server.py` does `sys.path.insert(0, ROOT / "outreach-agent")` and is
imported as a top-level module):

```bash
python -m uvicorn server:app --port 8000 &> /tmp/agent-hub.log &
for i in $(seq 1 20); do curl -sf -o /dev/null http://localhost:8000/ && break; sleep 0.5; done
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/
# → 307 (redirects to /login when logged out — that's "ready", not an error)
```

Drive it with the Puppeteer REPL driver — pipe a line-delimited script to
stdin, same command vocabulary as `chromium-cli` (`nav`, `wait-for`,
`click`, `fill`, `press`, `screenshot`, `console`, plus `wait-nav` and
`sleep`):

```bash
PW=$(grep '^APP_PASSWORD=' outreach-agent/.env | cut -d= -f2-)
node .claude/skills/run-agent-hub/driver.mjs <<EOF
nav http://localhost:8000/login
wait-for css=#password
fill #password $PW
click button[type=submit]
wait-nav
nav http://localhost:8000/
wait-for text=Outreach Agent
screenshot home
nav http://localhost:8000/leads
wait-for text=Find new leads
screenshot leads
console
EOF
```

Screenshots land in `./temporary screenshots/driver-<label>.png` (repo
root). `console` prints any captured `console.error`/`pageerror` text —
empty output means the page didn't throw.

| command | what it does |
|---|---|
| `nav <url>` | navigate |
| `wait-nav` | wait for a navigation to finish (after a submit/redirect) |
| `wait-for text=<t>` / `wait-for css=<sel>` | wait for visible text or a selector |
| `click <sel>` | click |
| `fill <sel> <value...>` | clear + type into a field |
| `press <key>` | keyboard press (e.g. `Enter`) |
| `screenshot [label]` | full-page PNG |
| `eval <js>` | `page.evaluate`, prints the JSON result |
| `console` | dump captured console errors |
| `sleep <ms>` | fixed wait, use sparingly — prefer `wait-for` |

Stop the server:

```bash
netstat -ano | grep ":8000" | grep LISTENING   # find the PID
powershell -Command "Stop-Process -Id <PID> -Force"
```

## Run (human path)

Same start command, then open `http://localhost:8000` in a real browser
and log in with `APP_PASSWORD`. Useless in a headless/agent context —
use the driver instead.

## Test

No automated test suite exists in this project. `python -m py_compile
server.py outreach-agent/*.py` is the closest thing to a smoke check —
catches syntax errors, not behavior.

---

## Gotchas

- **The `/leads` search flow makes a real OpenRouter LLM call before it can
  fail or succeed** (query expansion happens before the Tavily call). A
  `wait-for` after clicking `#search-btn` needs a generous timeout
  (20s+) — 15s isn't always enough, and hit the driver's own timeout in
  testing.
- **Auth is stateless** (HMAC-signed cookie, `outreach-agent/auth.py`) —
  restarting the server does **not** log out an already-open browser
  session. Don't assume you need to re-login after a restart.
- **The background `uvicorn` process can get reaped by this Claude Code
  environment's background-task tracking** even while it's actively
  serving requests fine (observed a "failed, exit code 127" notification
  fire on a process whose own log showed nothing but clean 200s right up
  to the end). If `curl http://localhost:8000/` returns nothing, just
  restart it — it isn't necessarily a real crash.
- **`waitUntil: 'networkidle0'` in Puppeteer intermittently times out**
  in this environment even against a healthy local server (30s timeout
  hit with nothing wrong on the server side). The driver uses
  `domcontentloaded` + explicit `wait-for`/`wait-nav` instead — more
  reliable here.
- **Google Sheets/Gmail API calls have no explicit timeout** (they go
  through `httplib2` via `google-api-python-client`'s default transport).
  A stalled connection can hang a background job (`/api/jobs/{id}` stays
  `"running"`) far longer than the call should normally take. Hit this
  once as a transient network blip; a retry a few seconds later worked
  fine. If a job seems stuck, it's more likely a stalled Google API call
  than broken logic.
- **Must run `uvicorn` from the repo root**, not `outreach-agent/` — the
  module path `server:app` only resolves if `server.py` is on `sys.path`
  via cwd.

## Troubleshooting

- **`FileNotFoundError: credentials.json`** when hitting any `/api/outreach/*`
  or `/api/leads/*` endpoint: the server wasn't started from the repo
  root (see Gotchas above) — `outreach-agent/config.py` resolves
  `credentials.json`/`token.json` relative to its own file, so this
  specific error is now fixed for cwd issues, but a truly missing
  `credentials.json` means Google OAuth was never set up — see
  `outreach-agent/README.md`.
- **`error: TAVILY_API_KEY is not set — add it to outreach-agent/.env`**
  from `/api/leads/search`: expected, not a bug — the lead-sourcing
  search feature needs a (free) Tavily key; the rest of the app works
  without one.
- **`curl -sf http://localhost:8000/` "fails" even though the server is
  up**: it isn't failing — `-f` only trips on 4xx/5xx, and `/` returns
  `307` (redirect to `/login`) when logged out, which `-f` treats as
  success. If you need a true liveness check independent of auth state,
  check for a non-empty response instead of relying on the exit code
  alone.
