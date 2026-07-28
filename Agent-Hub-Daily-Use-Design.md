# Agent Hub — Daily use and honest conversion

How to make this a product people open every day, and pay for, without building
anything you'd have to hide from the person using it.

---

## First, an honest correction to the goal

Cold outreach is not a daily activity for most founders. It is daily *during an
active campaign* and roughly weekly otherwise. A product that manufactures daily
engagement for a weekly job produces resentment and then churn — the user feels
nagged, the notifications get muted, and the mute is permanent.

So the target isn't "daily forever." It's:

- **Daily while a campaign is running** — which, with a 25/day send cap, is
  structurally most days a customer is active.
- **Within-the-hour when a prospect replies** — the only genuinely time-critical
  event in the whole system.
- **Weekly otherwise**, via a digest that's worth reading on its own.

Design for that shape and the daily usage is real. Force it and you get a muted
email channel.

---

## The blocker: your highest-value event is undetectable

`TODOS.md` has this as a P2 under "Background reply watcher." It is actually the
single most important thing in this document.

> "The homepage says the agent 'watches your inbox' — today that's only true
> while a tab is open. A prospect replying at night sits undetected."

A warm reply is the moment your product is worth money. Response speed is the
difference between a booked meeting and a cold trail. Right now:

- Detection requires a browser tab to be open on a laptop that must stay awake.
- There is no notification of any kind — no email, no push, nothing.
- So the user has no reason to open the app, because opening the app is the only
  way to find out anything happened.

**You cannot build a daily-use product on top of a system that cannot tell the
user something happened.** Every other item below depends on this one.

Fix: run `watch_replies.py` as a real scheduled process on a real host (this is
also the $5 VPS that gets your laptop out of the critical path), then send an
email the moment a reply is detected and drafted.

---

## The loop

Trigger → action → reward → investment. The standard habit model, with the
distinction that every element here has to be *true* or it stops working within a
week.

### 1. Triggers — three, all carrying real information

**A. A prospect replied.** Immediate email: who, what they said (first line), and
"your reply is drafted — review it." This is the killer notification. It is
time-sensitive, high-value, and entirely honest.

**B. Today's queue is ready.** One morning email: "14 drafts ready for review ·
3 replies waiting · 11 sends left today." Send it only when there is something to
act on. A digest that sometimes says "nothing to do" is a digest people keep
open; one that arrives daily regardless gets filtered.

**C. Something needs a decision.** Bounce rate climbing, domain auth failing,
quota nearly spent, Google connection expiring in 2 days (you have a hard 7-day
expiry — warning about it is both a trigger and a support-load reduction).

Never send a notification whose content is "come back." Every one carries a fact
and a specific action.

### 2. Action — the queue must be fast, not gamified

This is where the retention actually lives, and it's unglamorous. Your
differentiator is the approval step, which means your differentiator is also
friction. The fix is not to reduce the control — it's to make exercising it
cost seconds.

- **Keyboard-first.** `j`/`k` to move, `a` approve, `e` edit, `x` dismiss,
  `⌘↵` send. Approving 25 drafts should take 90 seconds, not 15 minutes.
- **Batch approve with a filter** — "approve all 12 with no edits flagged."
- **Show the diff, not the whole email**, on drafts after the first few. Once a
  user trusts the pattern they're scanning for anomalies, not reading prose.
- **Mobile review.** Replies arrive when people aren't at a desk. A reply draft
  you can approve from a phone in 20 seconds is the difference between same-hour
  and next-day.

Queue fatigue is your churn mechanism. Someone who stops opening the queue has
already churned; they just haven't cancelled yet. **Track days-since-last-queue-
open as your leading churn indicator** — it will tell you 30 days before the
subscription lapses.

### 3. Reward — replies, which are already variable

You don't need to manufacture a variable-reward schedule. Cold outreach already
is one: you don't know which email gets a response. That's the real thing, and
it's why email is compulsive without anyone designing it to be.

What you should do is *surface* it well:
- Reply notifications that show the actual first line of what they wrote.
- A running "meetings booked" count — the outcome that matters, not opens or
  sends. Vanity metrics train people to value the wrong thing and then feel
  cheated when the wrong thing doesn't produce revenue.

### 4. Investment — make edits train the product

This is the strongest and most underused mechanic you have, and it's entirely
honest: the product gets better the more you use it, so switching costs rise
because leaving actually costs something real.

Right now `custom_instructions` is a static text field the user fills once. But
every time someone edits a draft before sending, you have a labelled example of
"what the model wrote" versus "what this person actually says." You're throwing
that away.

- Capture the diff on every edited draft.
- After N edits, offer: *"You've been cutting the second paragraph and shortening
  the ask. Want me to write that way by default?"* — user-approved, visible,
  reversible.
- Now month three is measurably better than week one, and that's the honest
  version of lock-in.

`agent._validate_outreach` already gives you structured rejection reasons. Edit
diffs give you the other half.

---

## Conversion, honestly

The strongest conversion event for this product is **a reply the user got.** Not
a timer, not a discount that expires tonight.

- **Trial ends with results in hand.** "You sent 43 emails, got 4 replies, booked
  1 meeting. Pilot continues from here." The value proposition proves itself; you
  just have to be present at the moment it does.
- **Quota-triggered upgrade, at the real boundary.** "You've used 45 of your 50
  trial leads." True, useful, arrives exactly when the constraint is felt.
- **Annual discount that's real** — you already offer two months free. Say it
  plainly, once, and don't dress it as expiring.
- **Ask for the upgrade after a reply, not after a send.** Sending is effort;
  replying is reward. Attach the ask to the reward.
- **Make cancellation genuinely one click,** as your FAQ already promises, and
  say so *on the pricing page*. For a product selling control to burned buyers,
  a visible easy exit is a conversion feature. It removes the exact fear that
  stops them signing up.

### Not building

Fake urgency or countdown timers, fabricated activity ("3 people are viewing
this"), confirmshaming opt-outs, hidden or multi-step cancellation, trials that
convert to paid without clear advance notice, or pre-checked upsells. All of them
are regulatory exposure in the US and EU, and all of them are specifically
suicidal for a product whose entire pitch is *"nothing happens without you."*
The contradiction would be visible to your buyer in about four seconds.

---

## Build order

| # | Work | Why here |
|---|---|---|
| 1 | Background reply watcher on a real host | Nothing else works without it. Also kills the "keep the laptop awake" problem. |
| 2 | Reply notification email | Your highest-value event becomes knowable. This alone creates the daily open. |
| 3 | Keyboard-first approval queue | Turns the differentiator from friction into speed. Biggest retention lever. |
| 4 | Morning digest, only when actionable | The second daily trigger. Suppress on empty. |
| 5 | Days-since-queue-open as churn metric | You can't fix retention you can't see. |
| 6 | Edit-diff capture → suggested instruction updates | Honest compounding value. |
| 7 | Mobile reply review | Same-hour responses from anywhere. |
| 8 | Result-anchored trial-end and quota prompts | Conversion at demonstrated value. |

Items 1 and 2 are most of the effect. If you only do those, the product goes from
"a thing you remember to check" to "a thing that tells you when it matters."

---

## Build-order corrections from the 2026-07-27 CEO review

The table above is superseded on three points. Reasoning is in the review report
at the end of this file; the corrections themselves are load-bearing.

1. **Edit-diff *capture* moves from #6 to the front.** `drafts_db.mark_sent` and
   `reviews_db.mark_sent` do not discard the model's original, they **overwrite
   it in place** at send time. It is the only item in the plan that is lossy to
   defer: every day the other phases run without it destroys corpus permanently.
   It is also two columns and two write sites. The *suggestion* half (#6b) is
   deferred to TODOS — its gate cannot be met until an account has 30+ pairs.
   Item #3's "show the diff, not the whole email" also reads this data, so #3
   depends on the capture landing first.
2. **The Google-connection expiry warning moves from #4 into #1.** Testing-mode
   refresh tokens die after 7 days, so "accounts with a live token" is the
   shrinking set the watcher iterates. Without the warning the watcher spends
   most of its life logging skips. `accounts.token_granted_at` does not exist
   yet, so the warning is uncomputable until that column is added.
3. **Reply detection is dedupe-driven, not boundary-driven.** "Scan all messages
   after our last outgoing message" fails the exact case it was written for: when
   the thing that landed on the thread is the operator's own manual Gmail reply,
   the contact's reply is still behind the boundary. The rule is "the newest
   message from the contact that has no review yet", with daemon and
   auto-responder senders classified out before candidacy (or bounce detection
   dies as a side effect) and older unreviewed messages marked superseded (or
   they get re-queued and answered after the fact).

Two prerequisites the build order does not name: split `APP_SECRET_KEY` before
the VPS holds it, and pin a permanent HTTPS hostname before Web Push subscriptions
depend on the origin.

## Corrections from the 2026-07-28 eng review

Four structures collapse, and two defects emerged only from combining decisions
that were each fine on their own.

1. **Dedupe keys on the Gmail message id, not the reply body.**
   `reviews_db.find_review_id` matches exact `customer_reply` text. Two "sounds
   good" messages on one thread collide and the second never surfaces; a
   multi-KB body in a PostgREST `.eq()` is a GET parameter that eventually 414s;
   and btree's ~2704-byte limit means the race can never be closed with a unique
   constraint. The message id is stable, short, and already in the payload —
   `unique (account_id, thread_id, gmail_message_id)` then makes double-insert
   impossible no matter how many processes race.
2. **A contact message our own message postdates is `answered_elsewhere`.**
   The dedupe rule as written double-replies a prospect the operator already
   answered from the Gmail app — visible, no draft, no notification is the fix.
3. **Visibility is separate from workflow state.** A review must be visible on
   detection, not on successful drafting, or a validator rejection or provider
   outage strands it and the user is never told a prospect replied.
   `list_pending_reviews` and the send/dismiss endpoints take an explicit status
   allowlist.
4. **One `scheduled_jobs(account_id, job, next_due_at, last_run_at, last_error,
   consecutive_failures)` table** replaces the `schedule` dependency, the global
   heartbeat row and the job registry. Per-account heartbeat, so one account
   wedged behind a dead token is visible instead of hidden behind a healthy
   global tick. `schedule` is deleted, not replaced — its import and the
   requirements change land in the same commit or server boot breaks.
5. **The key split mints a new session key and leaves the token key alone.**
   Re-keying stored tokens is a forced re-consent delivered as an unhandled 500.
6. **`draft_reply` gets a validator with its own constants.** Reusing
   `_validate_outreach` would reject a reply mirroring "thanks for reaching out"
   and force a 120-character answer to "not interested." Phase 3 does not start
   until that validator and its eval are green, because 3.6 seconds per draft is
   what turns human approval from a real control into a nominal one.

**Accepted and untracked:** unattended detection is capped at 7 days per account
until OAuth verification and CASA land, and reply drafting has no volume limit
and no unit meter. Both were surfaced and both were deliberately left unfiled.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | ISSUES_OPEN | mode: HOLD_SCOPE, 3 critical gaps, 8 deferred to TODOS |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES_OPEN | 18 issues, 2 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** not installed. The outside voice ran twice as a Claude subagent,
  once per review, returning 14 and 15 findings. Both passes verified every
  factual claim the reviews made and then found real defects on top. The eng
  pass produced the two highest-value catches in the whole cycle: the dedupe
  rule double-replies a prospect answered from Gmail, and status-as-visibility
  makes notification conditional on the LLM being up.
- **CROSS-MODEL:** six substantive tensions across both reviews, all resolved by
  the owner. Notable: Web Push kept but paired with `notify.py` for the closed-
  laptop case push structurally cannot serve; the "N sends left today" digest
  line dropped as manufactured urgency; OAuth verification deferred with the
  7-day cap accepted as a known limitation.
- **VERDICT:** CEO + ENG reviewed — 39 decisions taken across both, 0 unresolved.
  72 implementation tasks emitted (39 CEO + 33 eng, 48 P1). Tests 228/228 before
  and after both reviews; no code changed by either. Eng review status is
  `issues_open` rather than `clean`, so `/ship` will not treat this as CLEARED
  until the P1 tasks land.

NO UNRESOLVED DECISIONS
