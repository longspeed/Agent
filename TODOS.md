# TODOS

Deferred from the multi-tenant hotfix pass (2026-07-16, `/plan-ceo-review`).
None of these block a single real customer from using the app end-to-end —
they matter once there's more than one customer, or once sending volume grows.

### Auth hardening
**What:** ~~Rate limiting on `/login` and `/signup`~~ **done 2026-07-17** — see
below. Still open: email verification on signup, password reset flow.
**Why:** Currently anyone can mass-create accounts with an unverified email, and
there's no way to recover a lost password. Fine for one trusted customer, not
fine once signup is public.
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
