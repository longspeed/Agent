# /outreach — Reply & Follow-up Desk: End-to-End User Workflow Spec

**Audience:** the end user of the queue — the founder or agency rep who reviews, edits, and sends mail from Sendkeep. Not a developer spec.
**Source of truth:** `static/outreach.html`, `server.py` (`/api/outreach/*`).
**Status:** Spec, matched to current implementation.

---

## 1. What /outreach is

`/outreach` is the **Reply & follow-up desk**. It watches the Gmail threads you already send from, and turns four things into one short queue you can sweep:

1. Drafts of a first message (optional, capped, manual by default)
2. Detected promises ("I'll send the deck Friday") you made in replies
3. Replies that arrived and need a response draft
4. Scheduled follow-ups that are now due

It is a **conversation view, not a CRM replacement**. The record stays attached to Gmail; /outreach just shows you the next move.

### The three-rule operating model
- **Nothing sends by itself** that matters. Replies and follow-ups **always** wait for individual review. First-touch drafts are separate, capped, and re-guarded by the server at send time.
- **The worker refreshes ~every 30 minutes.** An empty queue means "nothing to do right now," not "dead end." "Check for replies now" forces an on-demand check.
- **Your click is the final authorization.** The server re-checks eligibility, opt-outs, duplicates, daily cap, and bounce pause at the moment you send — never just at draft time.

---

## 2. The end-to-end journey

### Phase 0 — Connect (once)
1. In **Settings**, connect the Gmail inbox you send from. Reads recent Sent thread metadata + message text so the desk can find warm conversations. A contact sheet is **optional** — discovery works off Gmail alone.
2. Optional: set your sender name, call-to-action link, and meeting link so drafts are better grounded.

### Phase 1 — Discover (first visits)
The discovery guide at the top of the page shows your *next* step, not a checklist:

- **01 — Connect Gmail** → `/settings` *(required once)*
- **02 — Let the worker discover recent Sent threads** → ~30 min cadence, bounded recent window (never the whole mailbox; respectful of Gmail limits)
- **03 — Review the reply or promise that needs you** → links down to the queues

When there is nothing new, the page says so explicitly — "All caught up" / "Nothing needs review right now" — so an empty state never reads as a bug.

The **operating loop** tab strip (Discover → Decide → Close the loop) is the same loop in words: watch the warm threads, review what changed, then let the outcome stay in Gmail.

### Phase 2 — Decide (the daily sweep, ~one screen)
Everything actionable lives in three queues, top to bottom:

| Queue | `#` badge | What's in it |
|---|---|---|
| **Drafts awaiting approval** | gold | First-touch drafts, capped, manual by default |
| **Promises to track** | gold | Detected promises you made, awaiting confirm or dismiss |
| **Replies and follow-ups to review** | accent | Reply drafts, due follow-ups, uncertain sends, sender checks |

Plus two read-only panels for context: **Monitored Gmail threads** (the full table of what the worker watches) and the **conversation pipeline / thread-to-deal view** (derived stage summaries).

The queue snapshot at the top gives the six live counts: **Follow-up due · Pending · Awaiting approval · Awaiting reply · Replied · Replies to review.**

### Phase 3 — Close the loop
Every card ends in one state: **sent, dismissed, or recorded as already handled.** Nothing lingers. After you act, the card fades out, the count updates, and the record resets to "attached to Gmail."

---

## 3. Review card states (the state machine operators see)

Each card in **Replies and follow-ups to review** is exactly one of the following. The badge *is* the state.

### A. `pending` — badge **Needs review** (accent)
A reply draft grounded in the newest Gmail message + thread history + your sender settings.

- Shows: **Their reply** (the live message they sent) above **Your reply** (editable textarea, prefilled)
- Promised dates/actions are tracked separately under Promises — not buried in the draft
- **Actions:** edit draft · rewrite (Shorter / More casual / More formal / free-text + Ask AI) · **Send reply** · **Dismiss**
- **Guards:** Send is disabled when the body is blank; a blank-body POST is rejected (422). Rewrites read the draft exactly as you last left it, then mark the card `rewritten` so the edit is attributed correctly.
- **Warnings shown when present:** validator problems ("Check before sending: …") and degraded classification ("Sender verification was incomplete — double-check before sending").

### B. `flagged` — badge **Confirm sender** (gold)
The reply came from a **different address than your sheet contact**.

- The draft is hidden — it is only generated *after* you confirm.
- **Actions:** **"Confirm it's them"** (generates the draft, returns the card to sendable) · **Dismiss**
- **Why the button exists:** the no-blank-body guard would otherwise make confirmation impossible.
- After confirmation the card becomes a normal sendable pending card using the observed sender's address.

### C. `answered_elsewhere` — badge **Already answered** (steel)
You already replied from Gmail (or another tool) — the detector saw it.

- Shows the reply plus a note: "You already replied to this from Gmail — nothing to send."
- **Action:** **Dismiss** to clear it from the queue. There is no send button by design.

### D. follow-up due — badge **Follow-up due** (accent)
`kind = follow_up`, still scheduled. No reply arrived before the follow-up date.

- Shows the **Drafted follow-up** only (their reply panel is hidden).
- **Actions:** **Send follow-up** · **Dismiss** (cancels it — it will *not* be scheduled again for this contact)
- **Guards:** this is the **only allowed follow-up per contact**; rewrite controls are hidden; a note states exactly that. Deleting/extracting its promised items is still tracked under Promises.

### E. follow-up send uncertain — badge **Send uncertain** (gold)
`follow_up_status = sending`. Gmail did **not confirm** the last send attempt. The zero-true contract: we don't know whether it went out.

- Draft is **read-only**. The card asks you to open the Gmail thread and record what actually happened.
- **Actions:**
  - **"I found it in Gmail"** → records it as sent, removes from queue (with a confirmation prompt)
  - **"Not sent — retry"** → returns it to the queue where it can be sent again
- **Guards:** the two outcomes are exclusive and reconciled server-side (claim/cancel semantics — only one operator's decision wins, which is what prevents duplicate follow-ups).

### F. follow-up cleanup window — badge **Send uncertain** (gold), no retry
`follow_up_status = sent` but the review card never closed (cleanup failed on an earlier action).

- **Action:** **"I found it in Gmail"** only — finishes cleanup. It **cannot be sent again**; no dismiss/retry shown.

### Empty state
"All caught up — Nothing needs review right now. Replies and due follow-ups will appear here."

### Boundary of each card's data
- The basis note is truthful per type: reply drafts are "grounded in the newest Gmail reply, prior thread history, and your sender settings"; follow-ups state they are "the only follow-up allowed for this contact."

---

## 4. Promise lifecycle (Promises to track)

Detected from reply text. Two stages, then closed.

| State | Badge | Actions |
|---|---|---|
| `detected` | **Needs confirmation** (gold) | **Confirm promise** (accepts the extracted action + due date, moves to due-to-check) · **Dismiss** |
| confirmed | **Due to check** (accent) | **Dismiss** when handled |

Each card shows: the promised action, the evidence quote ("I'll send the deck Friday"), the contact, and the due date (`Due <date>` or `No date detected`). Confirming/Dismissing removes the card and updates the pipeline count immediately.

---

## 5. Draft lifecycle (Drafts awaiting approval) — optional first touch

- Capped **separately** from replies (configurable daily cap, default 25/24h). Contacts over the cap wait for the next batch — never dropped.
- Approved drafts send **from your own Gmail** as normal messages with a **one-click unsubscribe link + List-Unsubscribe headers**, and the row is marked **immediately** on Google's accept (written + retried 3× — that is what stops a double email).
- **Actions per card:** edit subject · edit body · rewrite presets / Ask AI · **Send email** · **Discard**
- **Send all** (appears when drafts exist): sends each per-email after re-checking cap + opt-outs, **paced apart**, one at a time — not a bulk burst.
- Drafts are validated at generation time: no placeholders, no spam tells, CTA link present when configured. Nothing is written until the sheet's header row is intact (a full 9-column contract) so it can never overwrite unlabeled columns you keep.

---

## 6. Guardrails that re-check at send time (the approval contract)

Your click authorizes, then the server verifies **at the moment of send**, not at draft time:

1. **Recipient still eligible** (has an address, blank Status, not already Sent/Replied/Bounced/Unsubscribed)
2. **Opt-out / unsubscribe** not present since drafting
3. **No duplicate send** already recorded (thread ID + timestamp written on Google's accept)
4. **Daily cap** still available for this send
5. **Bounce pause** not active for the destination
6. **Body not blank** (reply lane); confirmation gate for off-sheet senders

What approval **cannot** protect (told to you on the page): a human approving the wrong recipient/copy, a delayed Gmail or worker (check monitoring state before trusting a stale queue), messages sent directly in Gmail or by another tool, and any warmup/inbox-placement guarantee.

---

## 7. Cross-cutting displays (read-only context)

- **Monitored Gmail threads** — table of Name / Email / Company / Status / Sent at / Replied? for everything the worker watches.
- **Conversation pipeline** — four tiles: **Needs your reply · Promises to handle · Awaiting reply · Conversation active**; each tile *links to the queue that resolves it*.
- **Thread-to-deal view** — derived stage per thread: *first touch not sent · needs your reply · promise to handle · awaiting reply · conversation active · meeting booked · deal won · closed lost · worker needs attention*. Derived from Gmail activity only; meeting/won/lost come from operator-entered outcomes, never inferred.
- **Review signal / evidence** — aggregate counts this inbox actually recorded: human edits captured, approvals recorded, replies after follow-up. Meeting attribution, deals won, and reply rate are shown as **Not tracked / Not enough data** until real evidence exists. Style signals stay scoped to this inbox — contact texts are never a shared corpus. "Copy proof summary" copies the summary to the clipboard.
- **Founder / agency dogfood log** — daily form (date, label, reviewed / sent / replies / follow-up replies / meetings / deals / human edits / minutes saved, next-inbox note, safety-event dropdown). Aggregate counts only: **no prospect addresses, bodies, or thread IDs stored**. **Prefill observed counts** pulls the counts the system already recorded. Safety events: duplicate/misrouted, bounce not reflected, opt-out not reflected, stale worker/connection, uncertain send, wrong recipient, other.
- **How it works** modal — one-click walkthrough of the same loop (prepare → approve → watch → reply) for first-time visitors.

---

## 8. Glossary

| Term | Meaning |
|---|---|
| Worker | Background refresher (~30 min) that re-checks recent Sent threads |
| Bounded window | Discovery looks at a limited recent Sent window, not the whole mailbox |
| Pending | Reviewed-thread reply awaiting your send/dismiss |
| Flagged | Reply from a different sender address — confirm before a draft is made |
| Answered elsewhere | Detector saw you already replied from Gmail — clear-dismiss only |
| Follow-up due | No reply by the scheduled date; one-shot send or dismiss |
| Send uncertain | Gmail never confirmed the send — record what you see in Gmail |
| Awaiting approval | First-touch draft queue (capped, manual) |
| Awaiting reply / Replied | Monitored-thread status: waiting on them / they answered |
| Promise | Extracted "I will…" action needing confirm → due-check → dismiss |