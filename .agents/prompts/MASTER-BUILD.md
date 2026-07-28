# Master prompt — ship order

Paste as the first message of a Claude Code session.

---

You are the technical lead on Agent Hub. Read this whole prompt before touching
anything.

## Where this project actually is

The engineering is excellent and pointed at the wrong end of the funnel. The
bounce path has per-recipient RFC 3464 parsing, a windowed rate, an append-only
watermark that fails toward safety, and 193 passing tests. `public.suppressions`
has zero rows — it has never fired. Meanwhile `/api/plan` (`server.py:403`)
returns a hardcoded dict containing `"Checkout is coming next."`, there is no
Stripe integration anywhere in the repo, and the pricing page sells three tiers
the backend does not know about.

Your job is to fix the ordering, not to make the good parts better.

## Anti-goals — violating these is the main failure mode

- **Do not touch the bounce path.** `bounces.py`, `gmail.find_bounce`, and their
  tests are done. They are the most interesting code here and that is exactly
  why you will be drawn to them. Leave them alone.
- **Do not add features** that are not in the phases below.
- **Do not create a fifth frontend.** There are already four Vite projects
  (`landing/`, `site/`, `web/`, `app/`) plus hand-written `static/*.html`.
- **Do not refactor for elegance.** Every phase below is gated on shipping, not
  on the code being nicer.
- **Do not start phase N+1 until phase N meets its definition of done.**

## Phase 0 — The product must be able to take money

Nothing else matters until a stranger can pay you.

- Add a `plan` column to `accounts` (migration, following the existing
  `add_accounts_*` naming, reasoning in the migration body).
- Replace the hardcoded `/api/plan` with a real lookup. Three tiers.
- Enforce monthly quotas. `usage.py` already records per-account events with
  cost buckets (`SOURCE_LEADS`, `DRAFT_EMAIL`, `DRAFT_REPLY`) — read quotas off
  that, do not build a second counter.
- Enforce at `prepare_drafts` and `find_leads`, the same two gates
  `bounces.assert_sendable` uses. The CLI bypasses the API; the gate belongs on
  the operation, not the endpoint.
- Stripe Checkout plus a webhook that sets the column. Test mode is fine.
- `DAILY_SEND_LIMIT` becomes per-plan rather than a global constant.

**Done when:** a fresh account can sign up, pay in Stripe test mode, receive the
right plan, and be refused at the quota boundary — verified end to end, not by
unit test alone.

## Phase 1 — Stop shipping claims that aren't true

Two are legal exposure, one sends duplicate mail to real people.

**1a. The verification claim.** `site/src/components/sections.tsx` says "Verified
emails only" and "Unverified leads never go out." `leads.py:31-34` has a model
guess `first.last@domain`, and `config.py:53-57` states there is no verification
gate. Pick one and close it:

- Build the free checks — MX lookup, syntax, disposable-domain blocklist,
  role-account detection. Reuse the DNS-over-HTTPS pattern already in
  `dns_check.py`; it needs no new dependency and works on Windows. MX alone
  removes most of the guessed domains.
- Layer free API tiers behind those checks (Reoon ~600/mo, ZeroBounce 100/mo)
  so no credit is spent on a domain with no MX record.
- Write the real verdict into `EmailConfidence` and gate sending on it.
- Do **not** implement SMTP `RCPT TO` probing. It gets the IP blocklisted and
  returns false confidence against Gmail and Outlook anyway.

If you cannot make the claim true, change the copy instead. Do not leave it.

**1b. Write ordering on the send path.** `send_prepared_draft`
(`send_outreach.py:158`) calls `gmail.send_email()` and then `mark_row_sent()`.
If that write throws, `send_all_prepared`'s `except Exception` files it as
*failed* — but the mail already left. `remaining` never decrements, so the daily
cap under-counts, and the draft stays pending and goes out **again** on the next
batch. This is the same class already fixed in `bounces.record()`, on the path
where the action is irreversible. Fix it the same way and test the recovery.

**1c.** Real business address in the footer (`sections.tsx:328` is still a
placeholder) and in every outgoing email. CAN-SPAM requires it.

**Done when:** every claim on the marketing site is true of the code, and a
failed sheet write cannot cause a second email to a prospect.

## Phase 2 — Draft quality and the quota cliff

`providers.py` and `eval_models.py` already exist. Finish the swap, do not
rebuild it.

- Move off `gpt-oss-20b:free`. OpenRouter's unfunded tier is ~50 requests/day —
  one customer at 25 sends plus the two-attempt retry exhausts it.
- Groq free (~14,400 req/day) as primary, Gemini Flash free as fallback.
- Use `agent._validate_outreach` as the eval — it already returns per-generation
  problems. Report first-attempt pass rate per model. A model that halves the
  retry rate halves request consumption, which is the real constraint.
- **Privacy split:** first-touch drafts may go to free endpoints (name, company,
  public research note, your own pitch). Reply drafting carries the prospect's
  own words and free-tier terms often permit training on it — keep replies off
  free endpoints, or update `privacy.html` to disclose it. `privacy.html:82`
  currently names OpenRouter specifically; keep it accurate.

**Done when:** measured pass rates are recorded, the provider is switched, and
the privacy disclosure matches what actually happens.

## Phase 3 — Two security fixes, both cheap

- `accounts_db.py:50` derives the Fernet key from `APP_SECRET_KEY`, which also
  signs unsubscribe tokens. Rotating that key kills every opt-out link ever
  sent — permanently, and `auth.py:88-96` correctly documents that those must
  work forever. Split into separate secrets and make unsubscribe verification
  accept a key list so old links survive rotation.
- Logout doesn't revoke. Add a `session_epoch` to the account row, include it in
  the signed payload, bump on password change and log-out-everywhere.

## Phase 4 — Delete three frontends

Keep `app/`. Confirm which project built the live `static/landing/` before
deleting. Migrate `outreach.html` and `leads.html` into `app/`. Delete
`static/settings.html`, which `server.py:216-218` admits is dead.

## Phase 5 — Follow-up sequences

The largest retention lever and the clearest justification for a price above
$49. Two steps is most of the lift. Same thread, not a new one. Cancel on reply
or unsubscribe. Each step still passes the approval queue — that is the product.

## Explicitly deferred

Redis-backed rate limiting and durable jobs (`ratelimit.py` is single-process by
its own docstring — a real ceiling, around 50 customers, not now). Gmail push
via Pub/Sub. RLS policies: enabled-with-no-policies is deny-by-default for the
anon key, but the backend uses the secret key and bypasses it, so tenant
isolation rests entirely on every query scoping `account_id`. Know that; don't
fix it today.

## How you work

- `python tests/test_outreach_agent.py` and
  `python -m py_compile server.py outreach-agent/*.py` after every change.
  Report counts before and after.
- Tests assert **recovery**, not repair. "And then it's fixed" passes for a
  permanently disabled guard.
- Negative tests must assert they would fail the naive implementation.
- Fix structurally, not textually. Parse the format; don't tighten the regex.
- When you fix a bug shape, grep every module for it. Report clean sweeps too.
- Comments here are the design record. Update them with the code.
- Anything measured against the Google Sheet is user-editable and cannot be
  trusted by a guard.
- Never mark a phase done with a failing test or a partial implementation.

## Stop and ask before

Any change to what gets sent, suppressed, charged, or deleted. Any migration.
Any file deletion. Show the diff and wait.

You are good at correctness and blind to whether a change should exist. This
sends email from customers' own domains.

## Report per phase

What shipped, what it's gated on, test delta, what you swept, what's open.
Terse. No victory laps.
