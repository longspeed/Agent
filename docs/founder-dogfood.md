# Founder dogfood and agency distribution

Sendkeep's first distribution channel is the workflow it already owns: the
founder's Gmail. Use the product to find people who already feel the missed
reply and forgotten follow-up problem, then use the same queue to handle every
conversation that comes back.

This is a validation loop, not permission to turn Sendkeep into a volume-send
product. Keep manual review enabled, respect the account's configured cap, and
send only to contacts the operator has explicitly reviewed and approved.

## Who to contact

Prioritize people who match the product instead of generic email-tool buyers:

- Growth, recruiting, or consulting agencies that manage several client
  inboxes.
- Solo founders and operators who already send roughly 15-50 messages a day
  from Gmail.
- People who say they lose replies, forget promised follow-ups, or search Gmail
  manually before a call.

Do not pitch warmup, domain rotation, inbox placement, or a replacement for a
CRM. The useful sentence is:

> Sendkeep is the Gmail reply and follow-up desk that keeps warm replies and
> promised next actions moving after the send.

For an agency, make the commercial unit explicit: one isolated Sendkeep
account per managed Gmail inbox. Inbox is $29/month; Agency pilot is $49/month per managed
question until real agencies validate workload and evidence requirements. Do not promise a
shared multi-inbox console until that workflow has been validated.

## The seven-day loop

### Day 0: prepare one real inbox

- Confirm the worker is healthy and has completed a successful cycle.
- Connect the founder's Gmail and let the worker discover recent Sent threads.
- Keep the optional Sheet lane disabled unless it is needed for a reviewed,
  approved first-touch batch.
- Add the founder's real offer, audience, and next step in Settings. Never use
  the default meeting purpose.
- Add a booking link only if the founder actually wants meetings as the next
  step.

### Days 1-5: create a bounded conversation sample

Build a small, relevant list of agency and solo-operator prospects. The
operator should review each address and message before sending. A practical
daily target is 20-50 *reviewed opportunities*; actual sends remain bounded by
the configured daily cap and the operator's approval.

For each approved message:

1. State the specific missed-reply or follow-up problem being investigated.
2. Ask for one low-friction response, not a generic demo.
3. Keep the message in the founder's Gmail workflow so the reply can be
   observed in the same thread.
4. Record no result until Gmail or the operator provides one.

When replies arrive, work the queue in this order:

1. Reply drafts waiting for human review.
2. Promised dates or actions that are due or approaching.
3. Conversations with a clear next step or booking link.
4. Quiet threads where the configured follow-up is due.

Edit or dismiss every draft deliberately. Those edits are the private voice
signal for that inbox; AI rewrites are not counted as human preference data.

### Day 6: review the evidence

Use **Copy proof summary** in Outreach and keep the source of every result
clear:

| Signal | What counts | Label |
| --- | --- | --- |
| Reply | A reply observed in a monitored Gmail thread | `organic` |
| Follow-up reply | A reply observed after a Sendkeep follow-up | `organic` |
| Meeting | The operator confirms that a meeting was booked | `operator-confirmed` |
| Deal | The operator confirms won or lost status | `operator-confirmed` |
| Seeded thread | A test reply used only to demonstrate the workflow | `seeded` |
| Voice signal | A human edit or approval recorded on a sent draft | `organic` or `operator-confirmed` |

Never turn a seeded thread into a customer quote or performance claim. Never
infer a meeting or deal from a calendar URL, a reply, or a draft.

### Day 7: ask for the second inbox

The strongest agency signal is not a high send count. It is a request to run
the same queue on another client inbox. Ask:

- What reply or promised action would normally have been missed?
- How much time did the queue save compared with searching Gmail manually?
- Did the operator edit the draft or rewrite it from scratch?
- What per-inbox agency price would be cheaper than the actual missed revenue or labor, and what proof would justify it?
- Which client inbox should be connected next?

Treat a second-inbox request, a paid continuation, or a confirmed meeting as
stronger evidence than a vanity reply-rate screenshot.

## Daily operator log

Use **Founder / agency dogfood log** at the bottom of Outreach to keep one
account-scoped row per day. It stores aggregate counts only; do not copy
prospect bodies, email addresses, or thread IDs into the log or a public case
study. The form accepts internal inbox labels and a fixed safety-event
vocabulary so the distribution record stays operational rather than becoming a
second CRM.

| Field | Meaning |
| --- | --- |
| Date | UTC date of the run |
| Inbox | Internal account label, not the Gmail address |
| Reviewed | First-touch drafts reviewed |
| Sent | Messages actually sent after approval |
| Replies | Replies observed in monitored threads |
| Follow-up replies | Replies after a Sendkeep follow-up |
| Meetings | Operator-confirmed meetings |
| Deals | Operator-confirmed wins or losses |
| Human edits | Sent drafts edited by the operator |
| Minutes saved | Operator's best estimate, with method noted |
| Next inbox | Requested, connected, or blank |
| Safety event | Duplicate, bounce, opt-out, stale worker, or `none` |

Use the account-scoped proof summary for aggregate product evidence. Keep this
operator log separate from public marketing copy until a real participant has
reviewed and approved any quote or result.

Use [founder-dogfood-messages.md](founder-dogfood-messages.md) for reviewed
Gmail-native starting points. Treat each message as a draft to personalize, not
as a sequence to launch blindly.

## Stop conditions

Pause the run immediately if any of these occur:

- Gmail disconnects or worker health becomes stale.
- A message is duplicated, misrouted, or has an uncertain send outcome.
- A bounce, opt-out, or complaint is not reflected in the queue.
- A draft contains an unsupported claim, wrong recipient detail, or invented
  meeting context.
- The operator cannot explain why a message was sent.

Preserve the audit trail, fix the underlying issue, and rerun the health check
before sending again. See [operations.md](operations.md) for deployment and
worker recovery procedures, and [pilot-validation.md](pilot-validation.md) for
the pilot interview and safety checklist.

## What success looks like

At the end of the first week, the desired output is not a fabricated case
study. It is a small, trustworthy dataset showing whether the conversation
layer earns a second inbox:

- A non-founder can connect Gmail and understand the queue.
- A real reply or promised action is handled with less inbox archaeology.
- Human review is faster than writing from scratch.
- A meeting or deal can be confirmed without Sendkeep pretending to infer it.
- An agency asks to repeat the workflow on another managed inbox.

If those signals do not appear, narrow the audience or improve the reply and
follow-up workflow before adding sending volume, warmup, rotation, sequences,
or multi-inbox infrastructure.
