# Agent Hub — Live smoke test & UX review

**Date:** 2026-08-04 · **Build:** localhost:8000, logged in as
`applepenprovnlong@gmail.com` · **Method:** Chrome extension, real account,
real data. **Nothing was sent.**

**Overall: 6/10.** The engineering underneath is strong and the Guide is
genuinely good. What's wrong is almost entirely *copy and state* — the product
says three different things about what it does, and the one path a returning
user takes ends in a circle.

---

## BLOCKERS — the product contradicts itself

### S-1 · The logged-in home page sells autopilot. The product is approval-first.

The first thing a signed-in user reads:

> **"Outreach that runs itself."** · badge: **"Autonomous agents"**
> Step 1 It writes the email → Step 2 **It sends & listens** → Step 3 You just approve

That describes a product that sends on its own and asks you to approve the
*reply*. The actual product refuses to send anything without approval, and
that refusal is the entire differentiator — it's what `/security`, the FAQ, and
the drafts panel ("Nothing goes out until you do") all promise.

So the home page is simultaneously the strongest anti-selling point (it promises
the thing your buyer is afraid of) and factually wrong about the software.

**Fix:** headline to the actual product — *"Cold outreach that waits for your
yes."* Step 2 becomes "It queues the email for you," step 3 "You approve, it
sends and watches for the reply." Drop "Autonomous agents."

### S-2 · "Verified emails" is still live in the product, and the product denies it two clicks away

Settings → Pilot plan: *"up to **25 verified emails** per inbox every 24 hours."*

Lead Agent → *"it searches the web, tries to identify named people, and
**infers their email** where it can."*

Both are in the app. The second one is true. Users will read the first, hit
bounces, and find the second — which is exactly the sequence the Apollo
complaint you showed me described. Delete the word "verified" everywhere until
verification exists.

---

## STUCK POINTS — where a user stops

### S-3 · The main flow dead-ends in a circle *(highest-value fix)*

Current state of this account: 5 sheet rows, all with a status, so none are
eligible. What the page says, in order down the screen:

1. **"Prepare drafts"** — prominent, orange, enabled
2. *"0 recipients can be drafted today · 0/25 sent in the last 24 hours"*
3. Click it → *"No pending contacts are available to draft today."*
4. Empty state below: *"No drafts waiting — **Press Prepare drafts above** to
   generate an email for each eligible contact."*

Step 4 tells you to do the thing that just failed. Nothing anywhere says *how to
get eligible contacts* — that you add rows to the Sheet with a blank Status, or
run the Lead Agent. A returning user with a finished campaign has no next
action, and the interface's only advice is a loop.

**Fix:** when eligible = 0, the empty state should route out, not repeat:
*"Every contact in your sheet has been emailed. Add rows with a blank Status, or
[find new leads] →."* Disable **Prepare drafts** with that reason as its tooltip.

### S-4 · "Sent" means two different numbers on two pages

- Home: **"5 emails sent · 3 replies received · 60% reply rate"**
- Outreach: **2 Sent · 3 Replied**

Both are right — Outreach's "Sent" means *sent and not yet replied* — but the
same word carries a different meaning one click apart, with no explanation.
A user who sent 5 and sees "2 Sent" will reasonably conclude 3 failed.

**Fix:** label it "Awaiting reply" on the Outreach page, or make both 5.

### S-5 · The meeting-purpose guard only catches one exact string

This live account has: **"To have a meeting with our team."**

The helper text under that field says the generic default *"is rejected at send
time because it produces empty-sounding emails."* But `account_send_blockers`
tests equality against `DEFAULT_MEETING_PURPOSE` only — so any equally
content-free sentence that isn't byte-identical passes. This account's purpose
names no offer, no audience, and no outcome, which is precisely what the guard
exists to prevent, and it sailed through.

**Fix:** the guard can't judge quality, so stop implying it does. Either check
something cheap and real (length, or that it isn't only stopwords), or change
the copy to "we can't check this for you — vague purposes produce vague emails,"
and show an example of good vs bad inline.

---

## SMALLER, BUT USER-VISIBLE

| # | Finding | Where |
|---|---|---|
| S-6 | **"83.198 LLM tokens"** — decimal separator used for thousands. Should read 83,198. | Settings → Usage |
| S-7 | **"22:00 18 thg 7"** — Vietnamese month abbreviation (`tháng 7`) in an otherwise English UI. | Outreach → Campaigns |
| S-8 | Rows show Status **Replied** while the **Replied?** column shows **—**. Two columns about the same fact disagree. | Outreach → Campaigns |
| S-9 | Settings renders **"Loading…"** for ~3 seconds on *localhost*. On a real host with real latency this is the first impression. | /settings |
| S-10 | **"Or paste a Sheet ID/URL manually"** is an interactive button styled as grey caption text. Nothing signals it's clickable. | Settings → Lead sheet |
| S-11 | Usage shows lifetime tokens and searches — not quota headroom. No "X of Y drafts this month," so the plan limits are invisible until they bite. `plans.describe()` doesn't return headroom. | Settings → Usage |
| S-12 | **BUG-1 confirmed live:** "Sender first name" (required — blocks all sending) and "Company name" (optional) look identical, while "Custom instructions" *is* marked `(optional)`. The convention implies company is required and says nothing about the field that actually is. | /settings |
| S-13 | Plan card says "Pilot plan · $0/month" while `plans.py` defines Pilot at $49. Honest placeholder text explains it, but the tier name and price disagree with the code. | Settings → Plan |

---

## WHAT'S GOOD — don't lose these in a redesign

- **The Getting Started guide is the best asset in the product.** Table of
  contents, honest scope, and a call-out saying *"You do not need the lead
  sourcing agent"* — telling users they can skip a feature is rare and it builds
  trust.
- **The Lead Agent's copy is the most honest text in the app**: "infers their
  email where it can," "most searches turn up a handful of leads, not a full
  list." It sets expectations the marketing site breaks.
- **404 page** is well-designed with two escape routes.
- **Settings warns about Google's "unverified app" screen before you hit it** —
  good pre-emptive support.
- **Zero console errors** across every page loaded.
- **"Nothing goes out until you do"** on the drafts panel — the right message,
  in the right place. It just needs to be on the home page too.

---

## NOT TESTED — and why that matters

The rewrite feature, the batch-send confirmation dialog, `flagged` review
rendering, the rate limit, and the opt-out-line check were **all unreachable**,
because the account has no eligible contacts and no pending drafts.

That is itself the finding: **to exercise the product at all, you must first go
edit a Google Sheet by hand.** Every feature shipped in the last several
sessions sits behind a state the UI gives you no way to reach.

To finish this pass: add 2–3 rows with a blank Status to the sheet, then I can
test prepare → rewrite presets → hand-edit-then-rewrite → opt-out line appears
once → batch dialog wording, all without sending.

---

## Fix order

1. **S-1** home page copy — one paragraph, removes a direct contradiction of your differentiator
2. **S-2** delete "verified" — one word, removes the claim the market has learned to distrust
3. **S-3** empty-state routing — the only place a returning user gets stuck with no way forward
4. **S-12 / S-5** required markers and the purpose guard's honesty — both first-run
5. **S-4, S-6, S-7, S-8** — an hour of polish that removes four "is this broken?" moments
