# Agent Hub — What Should Be Upgraded

Ranked by what blocks revenue, not by what's most fun to build. Every item is
tied to a specific file so you can verify it.

---

## P0 — Fix before a single stranger uses this

### 1. You are emailing guessed addresses with no verification

This is the most serious problem in the codebase.

`leads.py` asks the model to derive the address:

> "infer a likely contact email ONLY if you can reasonably derive the company's
> domain from the URL (e.g. first.last@domain, or an address literally present in
> the content). If you can't derive a plausible domain, set email_guess to null —
> do not invent a domain." — `leads.py:31-34`

*(Corrected 2026-07-26: an earlier draft of this audit truncated the quote before
the null clause, which made the prompt look more reckless than it is. The domain
is constrained. The local-part pattern — `first.last` — is still a guess, and
that guess is sent without verification. The risk stands; the prompt is not the
cause of it.)*

Every one of those rows is written as `"unverified"` (`leads.py:116`), and
`config.py:53-57` confirms there is no gate: *"any approved row with an email can
send."* Nothing ever checks whether the address exists.

**What happens:** guessed `first.last@domain` patterns bounce at roughly 20–40%
in the wild. Gmail treats sustained bounce rates above ~2–3% as a spam signal.
You will burn your customers' sending domains — the precise harm your landing
page promises to prevent.

**Upgrade:**

- Verify at insert time in `leads.py`, before the row ever reaches the sheet
  (ZeroBounce, NeverBounce, Bouncer — roughly $0.003–0.008/email)
- Write the real result into `EmailConfidence` instead of the literal string `"unverified"`
- Hard-gate `send_outreach.py` on it — refuse to queue an unverified address
- Meter it and charge for it (~$0.02/verification against ~$0.005 cost)

This one change fixes the false advertising, protects deliverability, and adds a
revenue line. Do it first.

### 2. ~~Close the bounce loop~~ — SHIPPED 2026-07-26

The audit said "no bounce handling." The real diagnosis was worse: bounces were
**silently swallowed**. `gmail.py:162` matched the newest message's `From`
against the contact, so a `mailer-daemon@` bounce returned `(None, [])`. The row
stayed `Sent` forever, the customer was never told, and the dead address stayed
eligible for the next campaign.

Now implemented:

- **Detect** — `gmail.find_bounce()` walks the thread for delivery-status
  reports, via a new `_all_text()` helper (`gmail.py:179`); `_extract_body()`
  only returned the first `text/plain` part, and the status code lives in a
  sibling `message/delivery-status`.
- **Classify** — RFC 3463: `5.x.x` permanent, `4.x.x` transient. Only hard
  bounces suppress. Auto-suppressing a 4.x.x would permanently block a reachable
  prospect over a temporarily full mailbox.
- **Attribute** — confirms the failed address is the contact's via
  `X-Failed-Recipients` (`gmail.py:223`), falling back to the report body.
  Unparseable daemon messages are skipped: a missed bounce costs one stale row,
  a misattributed one costs a real prospect.
- **Suppress + mark** — hard bounces enter `suppressions_db` with
  `source="hard_bounce"`; the row is marked `Bounced`, which drops it from
  reply-check scope.
- **Auto-pause** — new `bounces.py` pauses above 5%, but only past 20 sends.
  One bounce in three is 33% and means nothing.

Surfaced through the existing `blockers` list in `_campaign_preview`, so
`server.py:572` already returns 409 and the UI already renders it — no frontend
work. Gated at both `prepare_drafts` and `send_all_prepared`, since the CLI
bypasses the API and drafts queued before the rate crossed the line must not go
out. 9 tests added.

### 3. Upgrade the model — you're compensating for it with regex

`config.py:22` defaults to `openai/gpt-oss-20b:free`.

Look at what that forced you to build in `agent.py`: a `_NON_LATIN` regex that
rejects any non-Latin character, with this comment —

> "A weak model sometimes injects a token from another script mid-sentence
> ('Reaching out დღის ...' — real output from the free model)" — `agent.py:275-278`

Plus 28 banned phrases, 17 filler phrases, a placeholder detector, a markdown
stripper, and a two-attempt regeneration loop. That validator is genuinely good
engineering — but you built a scaffolding rig to hold up a model that can't stand
on its own. Every failed validation is a second API call, so the free model isn't
even free.

**Upgrade:** move to a mid-tier model (Haiku 4.5, GPT-5-mini class, or similar),
measure first-attempt validation pass rate before and after, and keep the
validator as a safety net rather than a crutch. Reply rate is the metric that
decides whether this business works, and it's downstream of exactly this.

---

## P1 — Missing features that cap retention and pricing

### 4. No follow-up sequences

There is no follow-up capability anywhere — only first-touch and reply drafting.
Roughly 60–70% of cold email replies come from touches 2–4. You are leaving the
majority of your own product's results on the table, and every competitor has this.

**Upgrade:** a 2–3 step sequence with configurable delays, auto-cancelled on
reply or unsubscribe, each step still passing through the approval queue. This is
your single biggest retention lever *and* the clearest justification for a $79+
price point.

### 5. Lead sourcing is too thin to support the plans you sell

`leads.py:8-10` caps a run at 3 queries × 5 results = **15 raw results**, with at
most 10 leads out. To hit "500 leads/month" a customer runs the search 50+ times,
each time hitting the same Tavily result pool for their ICP — so they'll see
heavy repetition after a handful of runs.

**Upgrade:** paginate Tavily, raise the query count, track which queries a given
ICP has already exhausted, and add a real B2B data provider as a fallback source
instead of deriving domains from URLs.

### 6. No domain authentication check

No warmup and no SPF/DKIM/DMARC check, alongside a flat daily cap
(`DAILY_SEND_LIMIT = 25`).

*(Corrected 2026-07-26: this item originally also claimed "no send-time jitter."
That was wrong — `send_outreach.py:219` already sleeps a random interval between
sends. Only the DNS gap is real.)*

**Upgrade:** a pre-flight DNS check at Gmail-connect time that tells the user if
their domain isn't authenticated. A few hours of work, and it prevents a large
share of deliverability complaints you'd otherwise be blamed for.

---

## P2 — Infrastructure that breaks the moment you get traction

### 7. Rate limiting and background jobs are in-process memory

`ratelimit.py:1-6` is explicit: *"Single-process only, matching the current
one-uvicorn-process deployment."* Jobs run through `_run_job`/`start_job` in
`server.py:478-505` with the same constraint.

**Consequences:** you cannot run a second worker without limits silently
doubling, and every deploy or restart kills in-flight campaign preparation with
no recovery.

**Upgrade:** move rate-limit state to Redis or a Supabase table, and move jobs to
a durable queue with retry. Needed before you can scale past one box — which is
roughly the 50–100 customer mark.

### 8. Reply watching is poll-based

**Upgrade:** Gmail push notifications via Pub/Sub `watch`. Cheaper on quota,
near-instant reply drafts. "Drafted a reply 30 seconds after they wrote back" is
a demoable feature; "we check every 15 minutes" isn't.

---

## P3 — Codebase debt that's slowing you down

### 9. Four parallel frontends and a half-finished migration

`landing/`, `site/`, `web/`, and `app/` are **four** separate Vite+React projects
with duplicated assets (each has its own `vite.config.ts`). Only `app/` is
actually built into what the server serves. Meanwhile `server.py` serves a mix: hand-written
`static/index.html`, `static/outreach.html`, `static/leads.html`, plus React
builds at `static/app/`. And `server.py:216-218` admits
`static/settings.html` is dead code kept "as a rollback/diff reference."

**Upgrade:** keep `app/`, delete `landing/`, `site/`, and `web/` once you've
confirmed which one produced the live `static/landing/` build. Finish migrating
`outreach.html` and `leads.html` into `app/`, delete `static/settings.html`.

### 10. No linter, no test runner

Per your own `CLAUDE.md`: no `ruff`, no `pytest`, and typecheck is
`py_compile` (syntax errors only).

*(Corrected 2026-07-26: this item said "tests exist for the outreach agent only."
Understated. `tests/test_outreach_agent.py` covers server endpoints, auth,
unsubscribe, and sheets; `outreach-agent/tests/` adds ratelimit, leads, and usage
suites. Coverage is decent — it's the **tooling** that's missing, not the tests.)*

**Upgrade:** add `ruff` and `pytest` config so the existing suites run in CI
rather than by hand.

---

## Suggested order

| Week | Work | Why now |
|---|---|---|
| ~~1~~ | ~~Bounce loop (#2)~~ | **Done 2026-07-26** |
| 1 | Email verification (#1) | Makes the marketing true; prevents domain damage |
| 1 | Model upgrade + measure pass rate (#3) | Everything downstream depends on draft quality |
| 2–3 | Follow-up sequences (#4) | Biggest retention and pricing lever |
| 3 | DNS pre-flight check (#6) | Hours of work, large complaint reduction |
| 4 | Lead sourcing depth (#5) | Needed before you sell 500+/mo plans honestly |
| 5–6 | Consolidate frontends, ruff + pytest (#9, #10) | Pays for itself in velocity |
| Later | Redis/durable jobs, Gmail push (#7, #8) | At ~50 customers, not before |

**The one-sentence version:** verify the emails, kill the free model, and add
follow-ups — those three turn a demo into something people will pay $79/month for.

---

## What is genuinely good and should not be touched

Worth saying, because it's unusual: the prompt engineering in `agent.py` is the
strongest part of this codebase. The truth rules ("never invent anything about
the recipient"), the reply-classification branches, the show-the-model-its-own-
rejection retry, appending the unsubscribe line in code rather than trusting
generation, and the custom-instruction fencing that can't override the hard rules
— that's careful work, and it's the actual product. Fix the model underneath it,
don't rewrite it.
