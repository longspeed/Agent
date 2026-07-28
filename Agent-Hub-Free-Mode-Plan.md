# Agent Hub — Free mode change plan

The decision: no charging for now. This is what has to change so "free" is true,
safe, and doesn't brick itself in week one.

Ordering is deliberate. Phase 0 is days of work and makes the current state
coherent. Phase 1 starts today and runs in the background for weeks — it is the
only thing on this list where waiting costs more than working.

---

## Phase 0 — Make "free" true (this week)

### 0.1 The free tier expires, which defeats the point

`plans.py` sets `DEFAULT_PLAN = "trial"` with `lead_allowance=50` and
`lead_window="lifetime"`. That number was designed as a taster before a
purchase. As your actual product it means a user runs out permanently in week
one and is left with an account that cannot do the thing it exists to do.

- Move the free tier to `lead_window="month"`.
- Pick the allowance from what free-tier inference can actually sustain, not
  from the old trial number. Work backwards: Groq allows ~14,400 requests/day
  across all accounts; a lead search costs a query-expansion call plus an
  extraction call; a draft costs one call plus retries.
- Keep `monthly_drafts` and the daily send cap. They are no longer pricing —
  they are the only thing protecting a shared pooled key.

### 0.2 The pricing page sells things you don't sell

`site/src/components/pricing.tsx` still shows Pilot at $49/mo and Team at
$149/mo, with a monthly/yearly toggle advertising two months free.

- Collapse to one tier.
- Label it **"Free during early access"** — not "Free". Unqualified free is a
  promise you will have to break, and breaking it reads as bait-and-switch to
  exactly the trust-sensitive buyer this product targets.
- State the exit terms now, while they cost nothing: *"We'll give 30 days'
  notice before this changes, and your data is exportable either way."*
- Remove the billing toggle. It currently switches between two prices you don't
  charge.

### 0.3 Turn billing off without deleting it

`billing.py` is already inert when `STRIPE_SECRET_KEY` is unset — `configured()`
returns false and `create_checkout_session` raises `BillingNotConfigured`
rather than failing. Nothing to rip out.

- Unset the Stripe env vars.
- Leave `billing.py` and `plans.py` in place. The tier definitions become fair-
  use limits; the checkout wiring waits.
- Update `plans.describe()`'s `note` string if it implies purchasing.

### 0.4 Google sign-in only

This is the free fix for the abuse vector, and free mode makes it urgent: there
is now no payment friction at all between a script and your pooled API key.

Email verification on signup is blocked on a transactional email provider you
don't have. Google SSO gives you a verified email for nothing — and every user
must connect Google anyway for the product to function, so it costs no
conversion.

- Remove password signup.
- Keep password *login* for existing accounts, or migrate them.
- This also deletes the auth surface that produced the pre-registration
  takeover bug you already had to patch once.

### 0.5 Let people leave

You cannot delete an account. On a paid product that's a support ticket; on a
free product with Google SSO it's the only exit that exists, and it's a GDPR
obligation regardless.

- Self-service account deletion: revoke the Google token, delete the rows,
  confirm in writing.
- Delete the leftover QA account while you're in there.

**Phase 0 done when:** a stranger can sign up with Google, use the product for a
month without hitting a wall, see honest terms, and delete themselves.

---

## Phase 1 — Start the Google clock TODAY (weeks, in parallel)

Going free does **not** unblock this. CASA and OAuth verification are required
for the restricted `gmail.modify` scope regardless of whether money changes
hands.

Two live consequences, both product-killing:

1. **Every connection dies after 7 days.** Testing-mode policy. Your users' Gmail
   self-destructs mid-use and reads as the product breaking.
2. Every new user must be manually added to a Google Cloud console allowlist by
   you, personally, before they can sign up at all.

That second one means you do not have a signup flow. You have a guest list.

- Submit OAuth verification and book the CASA Tier 2 assessment now.
- Before that: check whether narrower scopes clear the bar.
  `gmail.modify` is heavier than `gmail.send` + `gmail.readonly` — if the app
  can work with less, the assessment gets cheaper and faster.
- Interim: warn users about the 7-day expiry *in the product*, not in a runbook
  you read to them.

**Also in phase 1, and it takes an afternoon:** move off the laptop. A $5 VPS
removes "keep the laptop awake" from your operating instructions and is the
prerequisite for everything in phase 2.

---

## Phase 2 — Make the product able to tell you something happened

A prospect replying is the only time-critical, high-value event in the system,
and today it is undetectable unless a browser tab is open.

### 2.1 Reply detection has to be trustworthy first

`gmail.get_latest_reply_with_history` inspects only `messages[-1]`. Two silent
drop paths, both in TODOS.md: a manual Gmail reply buries the prospect's earlier
one, and a reply from a different address is dropped every poll forever.

Do not notify on top of a lossy detector — users start trusting it and then miss
warm replies. Scan since the last outgoing message, or keep a per-thread
`historyId` cursor (which also removes the ~900 Gmail calls/hour polling waste).

### 2.2 Background watcher, all accounts

`watch_replies.main()` takes one account from `sys.argv`. It needs to walk every
account with a live token, attribute usage per account (`usage.run_as` exists for
this), isolate per-account failures, and stagger to respect Gmail quota.

### 2.3 Reply notification

`notify.py` exists but sends from the customer's own Gmail to their notify
address — which spends the send quota your safety story is built on, and makes
the app appear to email them from themselves. Decide that before scaling it.

The notification: subject names the person, body carries the first line of what
they actually wrote, one link into the drafted reply. Sent on detection, never
on a schedule, never saying "come back."

**Phase 2 done when:** a reply arrives at 11pm and the user knows by 11:05
without anything being open.

---

## Phase 3 — Make acting fast

Your differentiator is the approval step, so your differentiator is also your
friction, and queue fatigue is how this churns.

- Keyboard-first: `j`/`k` navigate, `a` approve, `e` edit, `x` dismiss, `⌘↵`
  send. Twenty-five drafts in ninety seconds.
- Batch approve with a filter.
- Show what differs from the pattern rather than the full email once trust
  builds.
- Decide first whether this lands in `static/outreach.html` or completes its
  migration into `app/`. Do not fork it.

Then the morning digest — one email, only when there is something to act on.

---

## Phase 4 — Stop shipping claims that aren't true

### 4.1 Verification

The site says "Verified emails only" and "Unverified leads never go out."
`leads.py` has a model guess `first.last@domain`; `config.py` records that no
verification gate exists. Free checks that need no budget: MX lookup (reuse the
DNS-over-HTTPS pattern in `dns_check.py`), syntax, disposable-domain blocklist,
role-account detection. Layer free API tiers behind those. If you can't make the
claim true, change the copy.

### 4.2 Model quality

`gpt-oss-20b:free` is why `agent.py` needs a regex rejecting non-Latin script.
`providers.py` and `eval_models.py` already exist — finish the swap. Groq free
tier is a far larger model at the same price, and OpenRouter's unfunded 50
requests/day is a ceiling one active user hits alone.

Keep prospect reply text off free endpoints whose terms permit training, or
disclose it. `privacy.html` currently names OpenRouter specifically.

---

## Phase 5 — Compounding value

- **Edit diffs train the drafts.** Every edited draft is a labelled pair of what
  the model wrote versus what this person says. Capture it; after enough, offer a
  visible, reversible change to `custom_instructions`.
- **Churn metric.** `last_queue_opened_at` per account. Someone who stopped
  opening the queue has already left.

---

## Deferred, on purpose

Stripe wiring (written, inert, waiting). Follow-up sequences. Frontend
consolidation — four Vite projects is embarrassing, not urgent. Redis-backed rate
limiting and durable jobs (~50 users). RLS policies (know that isolation is
currently query discipline, not a control).

---

## What "done" looks like

A stranger finds the site, signs up with Google without you touching a console,
sources leads, approves a batch in ninety seconds, gets a reply at midnight, is
told about it, replies from their phone by morning, and can delete everything
whenever they want — on a server that doesn't care whether your laptop is open.

None of that requires taking a payment. All of it is required before taking one
would be honest.
