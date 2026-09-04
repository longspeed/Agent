# Follow-Up + Promise Desk — Execution Plan
**Date:** 2026-08-23 · **Source verdict:** Council round 2 — GO, gated by one falsification test · **Confidence:** medium-high
**Product:** Sendkeep `/outreach` pivots to two jobs only: (1) follow-up discipline (exactly one scheduled follow-up per contact), (2) promise tracking (extract → human-confirm → due-check). Reply drafting demoted. Everything else subtracted.

---

## Gate 0 — 48-hour falsification test (NO new product code)

Run before anything else. Failing this converts GO → KILL at a weekend's cost instead of six months of churn curves.

| # | Task | Output |
|---|---|---|
| G0.1 | Run the existing promise extractor over your own last ~90 days of Sent mail | Raw extraction dump |
| G0.2 | Hand-grade every extraction: real commitment vs soft phrase ("let's circle back") vs hallucination | Precision % |
| G0.3 | Query `reviews_db`: % of review cards ending `answered_elsewhere`, plus time-from-reply-to-worker-notice distribution | Reply-lane reality check |
| G0.4 | Put the graded output in front of 5 founders using the near-miss framing ("send pricing by Thursday," discovered 16 days late) | Install-intent count |

**Pass bar (both required):**
- Extraction precision ≥ **80%** (Buyer's churn ceiling was ~20% junk)
- ≥ **2 of 5** founders say they'd install today on the near-miss story

**On pass** → proceed to Phase 1. **On fail** → stop; write up why; revisit only if extraction quality changes materially.

---

## Phase 1 — Correctness before features (Week 1)

The two invariant holes the council found. Nothing ships until these are closed and tested.

### 1.1 Close the schedule→send reply window
Any inbound reply must cancel a queued follow-up instantly — following up on someone who just wrote you is maximal embarrassment for minimal code.
- Audit the current path (`mark_claimed_follow_up_replied`, `claim_follow_up_send`, worker refresh) for gaps where a reply lands after queueing but before the operator presses send.
- Add the missing cancellation trigger on reply detection; add regression tests for every interleaving (reply-before-claim, reply-during-claim, reply-between-queue-and-open-page).

### 1.2 Fail-safe send semantics
Unconfirmed send = **not sent**, always. Retry is the promoted default action; "I found it in Gmail" requires explicit confirmation (this mostly exists via the `sending` state + `/reconcile` — verify every branch, including the cleanup-only `sent` variant).
- Acceptance: provable zero double-follow-up paths AND zero follow-up-after-reply paths across the state machine. Write the truth-table test.

---

## Phase 2 — The three non-negotiable edits (Weeks 1–2)

### 2.1 Counts, never inferred dollars
Headline metric becomes: **"7 promises overdue — oldest 12 days, Sarah @ Acme, 'send security docs'."**
- Specific, verifiable, slightly shaming — beats any invented number (Buyer: deal values live in your head; Contrarian: "$14k past due" is built on vapor).
- Dollar figures appear ONLY when the operator typed them (optional field). Confirmed promises only — unconfirmed/stale items never feed aggregates. Display age on unconfirmed items; age out stale ones.

### 2.2 Confirm queue that can't rot
Cap the visible confirm queue (~10 items). Past that, batch-confirm degrades the ledger silently while still printing authoritative counts — trust dies once, not gradually. Show age ("unconfirmed, 9 days old") so decay is honest and structural, not a hope about review habits.

### 2.3 Daily digest — the retention spine
One morning email: due follow-ups · overdue promises (count + names + days) · yesterday's rescues. A desk nobody opens doesn't alarm; the digest makes the tool reach the operator even when the operator forgets the tool — fixing the exact disease it cures (Contrarian's month-two cliff).

---

## Phase 3 — Subtraction (Week 2)

1. **Demote reply drafting:** keep the lane behind a settings toggle; drop it from default page order and nav emphasis.
2. **Collapse vanity surfaces:** pipeline tiles, evidence stats, dogfood log move below the fold / into settings. The top of the page answers exactly one question: *"What needs me right now?"*
3. **Scoped OAuth messaging:** plain-English data page — what's read (recent Sent window, not all history), how long it's kept, one-click delete, personal-mail exclusion posture. The Buyer approved Slack/Calendly scopes without losing sleep; vague privacy pages are what he refuses.

---

## Phase 4 — Proof, price, position (Weeks 3+)

- **Rescue log:** instrument recovered threads (follow-up sent → reply arrived → thread reopened). This is the renewal story and the asset competitors can't fake.
- **Price:** $15–19/mo solo. Never compete on features HeyWren matches at $5 — compete on outbound-native (promises to *prospects* = deal currency) + proof-of-recovery.
- **Agency tier:** defer until 10 paying solos. Then per-client-inbox SLA tracking ("we reply within 48h").
- **Positioning line:** *recover the revenue your inbox is losing.*

---

## Metrics that decide the next step

| Metric | Week 1 | Day 30 |
|---|---|---|
| Extraction precision (live) | ≥80% | ≥85% |
| Digest open rate | — | ≥40% |
| D30 retention | — | ≥60% |
| Documented rescues | — | ≥1 per user |
| Double-send / follow-up-after-reply incidents | **0** | **0** |

## Kill switches
- Live precision <70% after tuning → pull promise lane, ship follow-ups-only.
- D30 <40% despite digest working → the pain isn't chronic enough at solo volume; evaluate agency-first pivot.
