# Master prompt — daily-use build

Paste as the first message of a Claude Code session. Read
`Agent-Hub-Daily-Use-Design.md` in the repo root first; this is the build order
for it.

---

You are the technical lead on Agent Hub. The goal of this run is to make the
product tell the user when something matters, and make acting on it fast.

## The situation

A prospect replying is the only time-critical, high-value event in this system.
Today it is undetectable unless a browser tab is open on a laptop that has to
stay awake (`VALIDATION-WEEK.md` documents this literally). There is no
notification of any kind. So the only way to learn a reply arrived is to open the
app, which is why nobody opens the app.

This is plumbing, not psychology. Fix the plumbing first.

## Anti-goals

- **No deceptive patterns.** No countdown timers, no fabricated activity or
  social proof, no confirmshaming, no hidden or multi-step cancellation, no
  pre-checked upsells, no trial auto-converting without clear advance notice.
  This product sells "nothing sends without you" to people burned by tools that
  acted behind their back — a manipulative pattern here is visible in seconds
  and costs the only differentiator there is.
- **No notification whose content is "come back."** Every notification carries a
  fact and one action, or it doesn't send.
- **No gamification of vanity metrics.** Streaks on sends, open counts, and
  activity badges train people to value the wrong number. Meetings booked is the
  outcome; report that.
- **Do not touch the bounce path.** `bounces.py` and `gmail.find_bounce` are
  done.
- **Do not create a fifth frontend.** `landing/`, `site/`, `web/`, `app/` already
  exist plus hand-written `static/*.html`.
- Do not start phase N+1 before phase N meets its definition of done.

## Phase 0 — Reply detection has to be trustworthy before you notify on it

Do not skip this. Notifying on a lossy detector is worse than not notifying: the
user starts trusting it and then silently misses a warm reply.

`gmail.get_latest_reply_with_history` inspects only `messages[-1]`. TODOS.md
records two silent drop paths:

- The operator replies manually from the Gmail UI before a poll runs → the
  contact's earlier reply is buried and never surfaced.
- The contact replies from a different address (assistant, alias) → dropped every
  poll, forever.

Replace newest-only with "scan all messages after our last outgoing message," or
a per-thread `historyId` cursor. The cursor also removes the ~900 Gmail
calls/hour polling waste, which matters once phase 1 runs this continuously.

The unknown-address case is a product decision, not a code one: surface it as a
flagged review ("replied from an address not on your sheet — is this them?")
rather than auto-accepting or silently dropping. Ask before implementing either.

**Done when:** a reply is detected regardless of what else has landed on the
thread since, and an off-sheet reply reaches the user as a flagged item instead
of vanishing.

## Phase 1 — Background watcher, all accounts, on a real host

`watch_replies.main()` takes a single account email from `sys.argv` and loops
with `schedule` at `CHECK_INTERVAL_MINUTES = 5`. That's a per-account manual
script. It needs to become one process that walks every account with a connected
Google token.

- Iterate accounts with a live token; skip and log the rest.
- Attribute usage per account — `usage.run_as` exists for exactly this and worker
  threads don't inherit contextvars.
- One account's failure must not stop the loop. `check_for_replies` already
  isolates per row; apply the same discipline per account.
- Respect Gmail quota: stagger accounts, don't fan out.
- The host is a prerequisite you cannot do alone. A $5 VPS removes the laptop
  from the critical path and is the single highest-leverage line in
  `VALIDATION-WEEK.md`. Stop and tell me when you need it.

**Done when:** replies are detected with no browser open, for every account, and
one bad account doesn't halt the others.

## Phase 2 — Reply notification

`notify.py` exists and sends from the account's own Gmail to their
`notify_email`. It works, and it has two properties worth a decision before you
build on it:

- Notifications consume the customer's own Gmail send quota — on a product whose
  core safety story is per-inbox send caps.
- They land in the customer's Sent folder, so the app appears to be emailing
  them from themselves.

Ask me whether to keep that or add a transactional provider before you scale it.

The notification itself:

- Subject names the person: "John at Acme replied."
- Body carries the actual first line of what they wrote, and one link into the
  drafted reply.
- Send on reply detection, not on a schedule.
- Never send an empty or "just checking in" notification.

**Done when:** a reply lands and the user knows within one poll interval, with
enough context to decide whether to act now.

## Phase 3 — Make the queue fast

Your differentiator is the approval step, so the approval step is also your
friction, and queue fatigue is how this product churns. The fix is speed, never
less control.

- Keyboard-first: `j`/`k` navigate, `a` approve, `e` edit, `x` dismiss, `⌘↵`
  send. Approving 25 drafts should take about 90 seconds.
- Batch approve with a filter ("approve all with no flags").
- After the first few drafts, show what differs from the pattern rather than the
  full email — users are scanning for anomalies by then, not reading prose.
- Decide with me first whether this lands in `static/outreach.html` or completes
  its migration into `app/`. Do not fork it into both.

## Phase 4 — Morning digest, suppressed when empty

One email: "14 drafts ready · 3 replies waiting · 11 sends left today." Send only
when there is something to act on. A digest that sometimes doesn't arrive is one
people keep open; a daily one that's sometimes empty gets filtered, and the
filter is permanent.

Also worth including here: Google connection expiring in 2 days. Testing-mode
tokens die after 7 days and today nobody is warned.

## Phase 5 — Churn metric

Record `last_queue_opened_at` per account and surface days-since. Someone who
stopped opening the queue has already churned and hasn't cancelled yet. This is
the leading indicator, and you can't fix retention you can't see.

## Phase 6 — Edit diffs train the drafts

Every edited draft is a labelled pair: what the model wrote, what this person
actually says. You currently discard it.

- Capture the diff when a draft is edited before sending.
- After enough of them, offer a concrete, user-approved change: "You've been
  cutting the second paragraph and shortening the ask. Write that way by
  default?" Visible, reversible, never silent.
- Feed accepted changes into `custom_instructions`, which
  `agent._custom_instructions_block` already fences safely.

## Deferred

Mobile reply review, and result-anchored conversion prompts (trial-end summary,
quota boundary). Both matter; neither works before phases 1-2 exist.

## How you work

- `python tests/test_outreach_agent.py` and
  `python -m py_compile server.py outreach-agent/*.py` after every change.
  Report counts before and after.
- Before a design change, predict which tests must break and how many. Reconcile
  afterward. A smaller-than-predicted blast radius is a finding, not luck — and
  grep shared helpers for coupling, not just test bodies.
- Tests assert recovery, not repair.
- Any new guard: trace every value in its predicate to a source. In a conjunctive
  guard the weakest term decides. Anything measured against the Google Sheet is
  user-editable; `outreach_drafts` and `suppressions` are authoritative.
- Fix structurally, not textually. When you fix a bug shape, sweep every module
  for it and report clean sweeps too.
- Comments here are the design record. Update them with the code.

## Stop and ask before

Any change to what gets sent, suppressed, charged, or deleted. Any migration. Any
file deletion. Provisioning the host. The unknown-address policy in phase 0. The
notification-transport decision in phase 2. The frontend decision in phase 3.

## Report per phase

What shipped, what it's gated on, test delta, prediction vs actual, what's open.
Terse.
