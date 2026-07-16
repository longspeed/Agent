# TODOS

Deferred from the multi-tenant hotfix pass (2026-07-16, `/plan-ceo-review`).
None of these block a single real customer from using the app end-to-end —
they matter once there's more than one customer, or once sending volume grows.

### Auth hardening
**What:** Rate limiting on `/login` and `/signup`, email verification, password reset flow.
**Why:** Currently anyone can brute-force login or mass-create accounts. Fine for one
trusted customer, not fine once signup is public.
**Pros:** Closes an obvious abuse vector before it's exploited.
**Cons:** Real implementation work — rate limiter needs storage (Redis or a Supabase
table), email verification needs a transactional email path.
**Context:** `outreach-agent/auth.py` currently has no throttling on login attempts and
`accounts_db.create_account` has no email-confirmation step.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** Nothing blocking; can land independently.

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
