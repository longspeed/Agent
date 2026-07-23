# TODOS

Deferred from the multi-tenant hotfix pass (2026-07-16, `/plan-ceo-review`).
None of these block a single real customer from using the app end-to-end —
they matter once there's more than one customer, or once sending volume grows.

## Deferred from the product CEO review (2026-07-19, HOLD SCOPE)

### Google verification path (blocks billing)
**What:** Plan the exit from OAuth "Testing" status: Google verification for
the restricted `gmail.modify` scope, including the paid third-party CASA
security assessment.
**Why:** Two consequences of Testing status are live today: (1) every
account's refresh token expires after 7 days — candidates' Google connections
self-destruct and it reads as product breakage (the reconnect flow works and
says "reconnect Google", but nobody warns them it's coming); (2) the $49
Pilot plan in `/api/plan` cannot actually be sold until verification is done,
and the CASA assessment is an unpriced cost sitting in front of it.
**Pros:** Unblocks real billing; kills the 7-day token death.
**Cons:** Verification is slow (weeks) and CASA costs real money; worth
researching whether narrower scopes could reduce the burden first.
**Context:** `outreach-agent/config.py` SCOPES; `google_auth.py`
`get_credentials()` handles the expiry cleanly already. Validation week
mitigation: warn candidates, reconnect is one click (see VALIDATION-WEEK.md).
**Effort:** M (human, mostly waiting/paperwork) → M (CC can't compress Google)
**Priority:** P2
**Depends on:** Comes before Billing (P3) can ship.

### Background reply watcher
**What:** Server-side scheduler for reply checking, so detection doesn't
depend on someone having /outreach open in a browser tab.
**Why:** The homepage says the agent "watches your inbox" — today that's only
true while a tab is open (100s auto-poll; `watch_replies.py` as a daemon is a
manual script nobody runs). A prospect replying at night sits undetected.
**Pros:** Makes the core agent claim true 24/7.
**Cons:** Needs an always-on host first (currently a dev laptop + tunnel).
**Context:** `server.py` check_replies endpoint + `static/outreach.html`
auto-poll; `outreach-agent/watch_replies.py` `main()` already loops with
`schedule` — the missing piece is running it per-account somewhere durable.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** A real deployment (see Job persistence).

### Job persistence
**What:** Persist background jobs (send batch, reply-check, lead search)
instead of the in-memory `JOBS` dict in `server.py`.
**Why:** Process restarts silently lose in-flight/recent job state — the
frontend sees a bare 404 with no way to tell "lost to a restart" from "bad
job id."
**Pros:** Removes a real failure mode; matters more as usage grows.
**Cons:** Real infra work (Redis or a Supabase jobs table) — not warranted
for a 1-3 user validation week.
**Context:** `server.py` `JOBS = {}`, `start_job()`, `_run_job()`.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** Nothing blocking.

### Split session-signing and token-encryption keys
**What:** `APP_SECRET_KEY` currently signs session/OAuth-state tokens (HMAC)
*and* derives the Fernet key encrypting every stored Google token. Split into
two secrets.
**Why:** One secret's compromise means both session forgery and decryption of
every connected Gmail. Rotating it today also permanently breaks every stored
token.
**Pros:** Standard key-separation hygiene; makes rotation safe.
**Cons:** Existing accounts must reconnect Google once when split.
**Context:** `outreach-agent/config.py`, `accounts_db.py` Fernet derivation.
**Effort:** S → S
**Priority:** P2
**Depends on:** Nothing blocking.

### Reply-detection correctness: scan since last outgoing, not newest-only
**What:** `gmail.get_latest_reply_with_history` only inspects `messages[-1]`.
Replace with "scan all messages after our last outgoing message" (or a
per-thread `historyId` cursor), and decide a policy for replies arriving
from an address other than the sheet's Email cell (assistant/alias).
**Why:** Two silent drop paths (eng review 2026-07-19, outside voice):
(a) operator replies manually from the Gmail UI before a poll runs → the
contact's earlier reply is buried and never reviewed; (b) contact replies
from a different address → dropped every poll, forever. Both lose booked-
meeting signal with zero operator indication.
**Pros:** Closes the only known ways to silently lose a reply; the cursor
also kills the ~900 Gmail calls/hour polling waste (same mechanism).
**Cons:** Real design work (unknown-address policy: auto-accept vs flagged
review); touches the most safety-critical read path.
**Context:** `outreach-agent/gmail.py` `get_latest_reply_with_history`;
week mitigation is procedural (VALIDATION-WEEK.md: reply from inside the
app only).
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** Nothing blocking.

### Operational hygiene bundle
**What:** (a) structured logging — timestamp + account id + action + outcome
on send/reply paths instead of bare `print()`; (b) `JOBS` dict cleanup (drop
entries after N hours); (c) HTML-only replies reach the LLM/review UI as raw
markup — strip tags in `gmail._extract_body`'s fallback; (d) narrow
`sheets._verify_row_index` to an email-column-only range read — today every
guarded write re-reads the whole sheet (~2 Sheets calls per send; fine at
25-row scale, brushes read quota at 500-row sheets with concurrent polling).
**Why:** None bites at current scale; all four bite with the second tenant.
**Pros:** Debuggability three weeks after the fact; less quota burn.
**Cons:** Pure hygiene, no user-visible change.
**Context:** `server.py`, `outreach-agent/watch_replies.py`, `gmail.py`,
`outreach-agent/sheets.py`.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Nothing; natural trigger is "before the second real tenant."

### Sheet read pagination + table virtualization
**What:** Paginate Sheets reads and virtualize the campaigns/leads table
render instead of loading and rendering the whole sheet every call.
**Why:** `sheets.get_all_rows()` has no range limit; the frontend renders the
entire table in one `innerHTML` write.
**Pros:** Prevents slowdown as lead lists grow.
**Cons:** Irrelevant at today's scale.
**Context:** `outreach-agent/sheets.py`, `static/outreach.html`, `static/leads.html`.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Real usage growth to justify it.

### Per-account send lock — done 2026-07-19
**What:** ~~Prevent concurrent `send_outreach.main()` runs for the same
account.~~ Landed from the stash into `server.py` (`_accounts_sending` guard),
plus per-row Sent marking in `send_outreach.py` so a crash mid-batch can't
cause re-sends either. Both duplication paths closed.

### Security hardening pass — done 2026-07-22
**What:** Five fixes from a backend review, all with tests
(`tests/test_outreach_agent.py`, +5 cases → 61 total):
1. **Rate limiter was a no-op behind the tunnel.** `/login` + `/signup`
   keyed on `request.client.host`, which is `127.0.0.1` for every visitor
   behind cloudflared — so the per-IP limits were global (one attacker's 10
   login attempts locked out every customer). Now keyed on
   `_client_ip()` → `CF-Connecting-IP` (authoritative at Cloudflare's edge),
   falling back to the socket peer for local/dev. `X-Forwarded-For` is
   deliberately not trusted (spoofable without a known proxy chain). This
   corrects the "rate limiting done 2026-07-17" claim below, which was only
   ever verified on localhost.
2. **Pre-registration account takeover via Google SSO.** `signup` never
   verified emails, and `link_or_create_google_account` silently merged a
   Google identity into any existing account with the same email. An attacker
   who pre-registered `victim@corp.com` with a password got the victim welded
   into that account on their first Google sign-in — attacker keeps the
   password, gains any Gmail token the victim later connects. Now the merge
   refuses a password-holding account (`AccountLinkBlocked` → `/login?
   google_error=account_exists`, "sign in with your password"); only
   password-less accounts auto-link.
3. **Job errors leaked internals.** `_run_job` returned `str(e)` for every
   exception. Now only `RuntimeError` (the app's user-safe message convention)
   is surfaced; anything else is logged server-side and returns a generic
   message.
4. **`ratelimit._hits` grew unbounded** — one-off IP keys never evicted. Added
   an opportunistic sweep (drops keys whose newest hit predates the largest
   window in use).
5. **`tavily_search` docstring lied** ("returns [] on failure" but raised).
   Now genuinely returns `[]` on a request/JSON failure (so one flaky query
   doesn't abort a 3-query lead search) and meters only on success; still
   raises loudly for the missing-key config error.
**Still open (follow-ups, NOT done here):**
- Email verification on signup + password reset flow (needs a transactional
  email provider — unchanged from below). The takeover vector is closed
  without it, but verified signup is still the proper long-term fix and would
  let password users also enable Google SSO (currently they're told to use
  their password).
- Authenticated "link Google identity to my existing account" flow, so a
  password user can opt into SSO after proving ownership.

### Auth hardening
**What:** ~~Rate limiting on `/login` and `/signup`~~ **done 2026-07-17**
(proxy-IP correctness fixed 2026-07-22, see the security pass above). Still
open: email verification on signup, password reset flow.
**Why:** Currently anyone can mass-create accounts with an unverified email, and
there's no way to recover a lost password. Fine for one trusted customer, not
fine once signup is public. (The account-*takeover* consequence of unverified
signup was closed 2026-07-22; the mass-creation/abuse vector remains.)
**Pros:** Closes remaining abuse/support-load vectors.
**Cons:** Real implementation work — email verification and password reset both
need a transactional email path (provider not chosen).
**Context:** `accounts_db.create_account` has no email-confirmation step.
Rate limiting (`outreach-agent/ratelimit.py`) is wired into all four
endpoints an earlier commit's message claimed but never actually
connected server-side — `server.py` had zero references to `ratelimit`
until this fix. Discovered while doing an unrelated `/loop` pass: wiring
it in surfaced a stale/incorrect commit message worth knowing about.
Verified live: 11 rapid `/login` attempts → 401 ×10, then 429.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** Nothing blocking; email verification / password reset can land
independently of each other.

### Deliverability infra
**What:** SPF/DKIM/DMARC (DNS-level, not code), per-account send warm-up ramp,
multi-inbox rotation guidance.
**Why:** Sending from a fresh domain/account without warm-up gets flagged as spam;
this determines whether outreach actually lands in inboxes.
**Pros:** Directly determines whether the product's core value (emails that get read)
actually works at scale.
**Cons:** Partly outside the codebase (DNS records), partly a product decision
(how aggressive is the warm-up ramp).
**Context:** `outreach-agent/send_outreach.py` currently sends without any ramp-up
or rotation logic.
**Effort:** L (human) → M (CC + gstack)
**Priority:** P2
**Depends on:** Having a second real customer to warm up for.

### Billing
**What:** Plans/limits tied to the existing `usage_events` table.
**Why:** Usage is already metered (`accounts_db.record_usage`) but nothing enforces
limits or charges for it.
**Pros:** Turns metering data already being collected into actual revenue/limits.
**Cons:** Needs a payment provider integration and plan-tier design first.
**Context:** `usage_summary()` in `outreach-agent/accounts_db.py` already aggregates
per-account usage by kind — the raw data this would build on already exists.
**Effort:** L (human) → M (CC + gstack)
**Priority:** P3
**Depends on:** Auth hardening (P2) probably lands first — no point billing accounts
that can be created via a brute-forceable signup.

### Removed NeverBounce entirely — done 2026-07-18
**What:** Deleted `outreach-agent/email_verification.py` and every call site
(`leads.py`'s verify pass, `server.py`'s `/api/outreach/campaigns/verify`
endpoint, `static/outreach.html`'s "Verify pending emails" button). Removed
`NEVERBOUNCE_API_KEY` from `config.py`.
**Why:** NeverBounce's trial credits were exhausted (0 balance, see the
three entries below) and blocked every real send. Decision: replace
automated verification with manual verification — `sheets.py::is_verified()`
already just checks whether the sheet's `EmailConfidence` column literally
says `"verified"`, so a sheet owner can type that in by hand for any contact
they've personally confirmed. The fail-closed gate itself (unverified
contacts are never eligible to send) is unchanged.
**Context:** New leads from lead-sourcing now always land as `"unverified"`
in the sheet — no automated verification attempt happens at all anymore.
The three entries below (logging, parallelization, tests) describe work
done on the now-deleted module; kept for history, not actionable.
**Effort:** M (human) → S (CC + gstack)
**Priority:** Done.
**Depends on:** Nothing.

### Log email_verification failures / surface NeverBounce credit exhaustion — done 2026-07-17
**What:** `email_verification.py` has no logging. Two independent /autoplan
outside-voice reviews (CEO phase + Eng phase, 2026-07-17) converged
separately on the same underlying risk: `NEVERBOUNCE_API_KEY` is a single
server-wide credential shared across all tenants in this multi-tenant app,
and NeverBounce meters credits, not per-tenant. When credits run out, every
account's `verify()` calls silently fall back to `"unverified"` — identical
to the "not configured" state this plan is trying to fix — with zero signal
to the operator about which tenant burned the pool or that it happened.
**Why:** Without a log line (or NeverBounce's balance-check endpoint), a
credit-exhaustion event is indistinguishable from "these leads are bad" or
"the feature was never turned on," during exactly the live-send week this
is meant to unblock.
**Pros:** Cheap observability win; closes a gap two independent reviews
found from different code paths.
**Cons:** None identified.
**Context:** No `logging` import or print statement anywhere in
`outreach-agent/email_verification.py`. Consider also polling NeverBounce's
balance endpoint and moving to per-account keys before onboarding a second
real paying customer.
**Effort:** XS (human) → XS (CC + gstack)
**Priority:** P2
**Depends on:** Nothing.
**Resolution:** Logging added. Immediately useful: it surfaced that this
deployment's real NeverBounce trial credits are already exhausted (0
balance) — right now every verification silently fails closed to
`"unverified"` for that reason, not because addresses are unverifiable.
Needs a real top-up before the verification gate does anything.

### Batch/parallelize email_verification calls in lead discovery — done 2026-07-17
**What:** `leads.py:109` calls `email_verification.verify()` synchronously,
one HTTP call per candidate, 20s timeout each, no concurrency or backoff.
**Why:** A `limit=10` lead search can add up to ~200s of latency to one
request in the worst case (found by /autoplan's eng-phase outside voice,
2026-07-17).
**Pros:** Meaningfully faster lead searches at moderate limits.
**Cons:** Adds concurrency complexity to a currently simple, easy-to-reason-
about loop; NeverBounce rate limits would need checking before parallelizing.
**Context:** `outreach-agent/leads.py:109`, inside `find_leads()`.
**Effort:** S (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Nothing blocking; lower priority than the misclassification
fix above since latency (not correctness) is the cost.
**Resolution:** Split into a sequential filter/dedup pass followed by a
`ThreadPoolExecutor(max_workers=4)` verification pass, same concurrency
pattern as `send_outreach.py`. Dedup/limit semantics unchanged, verified by
`outreach-agent/tests/test_leads.py`. NeverBounce's actual concurrent-request
limit was not independently verified — worth confirming if lead-search volume
grows.

### Unit tests for email_verification.py response parsing — done 2026-07-17
**What:** Add tests covering `verify()`'s branches: valid, invalid, timeout,
malformed/no-result response, missing key.
**Why:** This module gates whether real emails get sent to real people —
worth locking down given it's fail-closed by design and easy to regress.
**Pros:** Catches regressions in the fail-closed logic before they ship.
**Cons:** Repo has zero test infrastructure today (confirmed via `/health`,
2026-07-16) — this would be the first test in the project.
**Effort:** S (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Picking a test framework for the repo (not scoped here).
**Resolution:** Added `outreach-agent/tests/` using stdlib `unittest` +
`unittest.mock` (zero new dependencies) rather than picking a framework —
9 cases for `email_verification.py`, plus 5 for `leads.py` and 5 for
`ratelimit.py` added alongside other fixes this session (19 total).
**Note:** a stray `tests/__pycache__/test_rate_limit.cpython-313-pytest-9.1.1.pyc`
exists at the repo root with no matching `.py` source on disk — evidence an
earlier session wrote a `pytest`-based test at `tests/test_rate_limit.py`
that never got committed (or was deleted). `pytest` is installed locally but
not in `requirements.txt`. Worth an explicit decision (`pytest` vs stdlib)
before this grows into two parallel conventions.

### Cleanup: stale reviews.db
**What:** Remove `outreach-agent/reviews.db` (old SQLite file).
**Why:** Dead since the migration to Supabase Postgres (see commit `8481239`).
Harmless but confusing to a new engineer wondering which store is live.
**Pros:** Removes a red herring from the codebase.
**Cons:** None — purely dead weight.
**Context:** Confirmed unused by any current code path as of the 2026-07-16 review.
**Effort:** S (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Nothing.

## From /qa run (2026-07-20, pre-connect candidate state)

### Serve HTML routes with Cache-Control: no-cache
**What:** Page routes (`/outreach`, `/settings`, `/login`, `/signup`, `/`)
send no Cache-Control header, so browsers heuristically cache the HTML+JS.
**Why:** Observed live during QA: after a fix landed in `static/outreach.html`,
a plain reload still ran the old JS; only a cache-busted URL picked it up.
If a fix ships mid-validation-week, a candidate's browser may keep the old
page until a hard refresh — right when a stale bug matters most.
**Fix:** Add `Cache-Control: no-cache` to the HTML page responses in
`server.py` (static assets can stay cacheable).
**Effort:** S · **Priority:** P2 (before shipping any mid-week fix to candidates)

### Add a favicon
**What:** `/favicon.ico` 404s for logged-in users — one console error per tab.
**Why:** Cosmetic, but it's the only recurring console 404 and shows up in
every QA console sweep as noise.
**Effort:** S · **Priority:** P3

## From /qa run (2026-07-23, branch feat/lead-sheet-template-and-guide)

Fixed in that run and not repeated here: the five missing legal pages
(`/privacy`, `/terms`, `/acceptable-use`, `/dpa`, `/security`), `/docs` serving
Swagger UI, and the raw-JSON 404. Report:
`.gstack/qa-reports/qa-report-localhost-8000-2026-07-23.md`.

### Decide the LLM route that sees prospect reply text
**What:** `agent.draft_reply()` sends the prospect's own message to OpenRouter,
and `config.OPENROUTER_MODEL` defaults to `openai/gpt-oss-20b:free`.
**Why:** Free inference routes commonly permit the provider to retain or train
on submitted prompts. The prompt here is a third party's private email, which
the customer is data controller for. It is the first thing a serious vendor
review will ask about, and the new `/privacy` and `/dpa` pages now disclose it.
**Fix:** either pin a paid model carrying a no-training term, or keep the free
route and state the tradeoff as a deliberate product position.
**Context:** `outreach-agent/agent.py:173`, `outreach-agent/config.py:22`.
**Effort:** S (human) → S (CC + gstack) · **Priority:** P2
**Depends on:** Nothing; it is a decision, not a build.

### Legal pages need a lawyer's review before charging anyone
**What:** `/privacy`, `/terms`, `/acceptable-use` and `/security` were written
from the codebase and are factually accurate, but are marked "draft pending
legal review" on the page itself. `/dpa` deliberately publishes no contract
text, since a DPA is a signed Article 28 agreement rather than a notice.
**Why:** They unblock the broken links and Google's OAuth verification
requirement today. They are not a substitute for drafted terms once money
changes hands, and `/terms` in particular lacks governing law, jurisdiction,
and entity-appropriate warranty disclaimers.
**Fix:** have a lawyer produce the real versions; keep the sub-processor table
in `/dpa` in sync with the code when a provider is added or removed.
**Context:** `static/privacy.html`, `terms.html`, `acceptable-use.html`,
`dpa.html`, `security.html`.
**Effort:** M (human, mostly external) · **Priority:** P2 (before billing)
**Depends on:** Billing work; the two land together.

### Session for a deleted account 500s instead of clearing the cookie
**What:** A validly signed session whose `account_id` no longer resolves raises
an unhandled `postgrest.APIError` → 500.
**Why:** Not reachable today: tokens are HMAC-signed, so the id cannot be chosen
without `APP_SECRET_KEY`. It becomes reachable the day account deletion ships,
or after any accounts-table migration, where the right behaviour is to clear the
cookie and redirect to `/login`.
**Effort:** S · **Priority:** P3
**Depends on:** Pairs naturally with account deletion.

### Legacy pages still load Tailwind from the CDN
**What:** `/login` (and the other not-yet-migrated pages) pull
`cdn.tailwindcss.com`, which compiles styles in the browser on every load.
**Why:** Puts a third-party origin on the critical render path of the sign-in
page; if the CDN is slow or blocked, sign-in renders unstyled. The React pages
under `static/app/` already compile Tailwind at build time.
**Fix:** falls out of the React migration already in progress. Tracked so it is
not mistaken for a regression in the meantime.
**Effort:** absorbed by the migration · **Priority:** P3
**Depends on:** The page-by-page React conversion.

### Delete the QA account
**What:** `/qa` created `qa-2026-07-23@example.com` to test the logged-in pages.
It has no Google token and no sheet, so it can send nothing.
**Why:** It is a real row in the accounts table, and self-service account
deletion does not exist yet, so it needs removing by hand.
**Effort:** S · **Priority:** P3
