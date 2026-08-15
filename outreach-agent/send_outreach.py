"""Two-phase outreach.

Phase 1 -- prepare: read the account's approved-but-unsent sheet rows, generate
and validate a personalized email for each, and queue it in `outreach_drafts`
for human approval. Nothing is emailed here and the sheet is not touched.

Phase 2 -- send: a human approves a queued draft on the /outreach page, and the
server calls send_prepared_draft, which is the only place an outreach email
actually leaves Gmail. The sheet row is marked "Sent" immediately after, with
the same crash-safe retry the old one-shot path used.

Prepare a batch manually:  python send_outreach.py <account-email>
"""
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import accounts_db
import agent
import auth
import bounces
import drafts_db
import gmail
import plans
import sheets
import suppressions_db
import usage
from datetime import datetime, timezone
from notify import notify

# Draft generation runs concurrently per contact. Kept modest: OpenRouter's
# free-tier model is rate-limited to ~20 req/min, so higher concurrency mostly
# means more 429-retry waiting, not more speed.
MAX_WORKERS = 4

# Post-send sheet marking: attempts and base backoff (seconds). The delay is a
# module constant so tests can zero it.
_MARK_RETRIES = 3
_MARK_RETRY_DELAY = 2

# Spacing between sends in the "send all" convenience path, so an approved batch
# trickles out like a person clicking Send rather than leaving in one burst --
# bursts are a classic spam signal. Per-draft manual approval needs no such
# pacing; only the bulk path does.
_SEND_ALL_MIN_GAP = 3
_SEND_ALL_MAX_GAP = 12


def _with_retries(action, describe):
    """Runs action with a bounded backoff. Returns its result, or raises the last
    error with `describe` prefixed."""
    last_err = None
    for attempt in range(_MARK_RETRIES):
        try:
            return action()
        except Exception as e:
            last_err = e
            if attempt < _MARK_RETRIES - 1:
                time.sleep(_MARK_RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"{describe} after {_MARK_RETRIES} attempts ({last_err})") from last_err


def mark_row_sent(account, row_index, email, thread_id, body):
    """Marks a sheet row "Sent" right after the irreversible Gmail send, with a
    bounded retry. The mark must never be deferred to batch end (learning:
    batch-end-write-defeats-send-lock).

    This is the LEAST reliable of the two records a send produces, which is why
    it is written last and why send_prepared_draft treats a failure here as a
    repair task rather than a failed send. sheets.update_row re-verifies the row
    by address first, and that check raises permanently -- not transiently --
    when the operator has deleted or edited the row, so every retry fails
    identically. Sitting where it used to, ahead of the draft-queue update, that
    turned an ordinary sheet edit into an unbounded re-send loop."""
    sent_at = datetime.now(timezone.utc).isoformat()
    _with_retries(
        lambda: sheets.update_row(
            account, row_index, status="Sent", thread_id=thread_id,
            sent_at=sent_at, email_body=body, expect_email=email,
        ),
        f"Email was sent to {email}, but marking the sheet row failed",
    )
    return sent_at


def _prepare_one(account, row_index, name, email, company, lead_reason):
    """Generates + validates one email and queues it as a pending draft. No
    Gmail send, no sheet write -- the row stays Pending until a human approves
    the draft. A generation failure raises and surfaces in the batch summary,
    leaving the row untouched and safe to re-prepare."""
    unsub_url = auth.unsubscribe_url(account["id"], email)
    subject, body = agent.generate_outreach_email(
        account, name, company, lead_reason=lead_reason, unsubscribe_url=unsub_url
    )
    draft_id = drafts_db.add_draft(
        account["id"], row_index, name, email, company, subject, body
    )
    # Counted here, after the draft exists, because this is the unit the plan
    # sells -- a draft in the review queue. Not counted on generation: a
    # generation that failed validation and was retried cost us two LLM calls
    # (already metered as cost) but produced one draft, and billing the customer
    # for our own retry is indefensible. add_draft returning None means the row
    # was already queued, which is not a new unit either.
    if draft_id is not None:
        usage.record_unit(usage.UNIT_DRAFT_EMAIL, 1, email)
    return {"row_index": row_index, "email": email, "draft_id": draft_id, "subject": subject}


def prepare_drafts(account, auto_send=False):
    """Generate a batch of drafts for every eligible contact and queue them for
    review. Eligible = approved (blank Status), has an email, not suppressed,
    within today's remaining send allowance."""
    # Fail fast if the sheet can't be marked Sent later, before spending LLM
    # calls generating drafts that could never be sent (same consent gate the
    # actual send relies on).
    sheets.require_full_header(account)
    # Independent of the API's blocker check: this module is also driven from
    # the CLI, and an account bouncing this hard should not be able to spend
    # LLM calls drafting mail it must not send.
    bounces.assert_sendable(account)
    suppressed = suppressions_db.list_suppressed_emails(account["id"])
    readiness = sheets.campaign_readiness(
        account,
        sent_today=drafts_db.count_sent_last_24_hours(account["id"]),
        suppressed_emails=suppressed,
    )
    pending = [
        (row_index,
         row[sheets.COL_NAME].strip(),
         row[sheets.COL_EMAIL].strip(),
         row[sheets.COL_COMPANY].strip(),
         row[sheets.COL_LEAD_REASON].strip())
        for row_index, row in readiness["eligible"]
        # Skip rows already sitting in the review queue so re-running prepare
        # doesn't double-draft the same person (also enforced in drafts_db).
        if not drafts_db.has_pending_for_row(account["id"], row_index)
    ]

    if not pending:
        return {
            "prepared": 0, "total": 0, "failed": [],
            "daily_limit": readiness["daily_limit"],
            "remaining_today": readiness["remaining_today"],
            "draft_ids": [],
        }

    # Monthly plan quota, checked after the daily cap and on the same principle:
    # spend nothing before we know the work is allowed. An account with no
    # allowance left is refused outright (raises), rather than silently
    # preparing zero drafts and reporting success -- "prepared 0 of 12" with no
    # reason is the failure mode that generates a support ticket instead of an
    # upgrade. An account with *some* allowance takes what is left, so a batch
    # that straddles the boundary delivers up to the line rather than failing
    # whole.
    _, allowance, _ = plans.headroom(account, usage.UNIT_DRAFT_EMAIL)
    quota_trimmed = 0
    if allowance is not None and allowance <= 0:
        # Re-checked rather than raising from here so the refusal message is
        # built in one place. Costs a second query only on the path that is
        # about to do no work at all.
        plans.check(account, usage.UNIT_DRAFT_EMAIL)
    if allowance is not None and allowance < len(pending):
        quota_trimmed = len(pending) - allowance
        pending = pending[:allowance]

    prepared = []
    failed = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(usage.run_as, account["id"], _prepare_one, account, idx, name, email, company, reason): email
            for idx, name, email, company, reason in pending
        }
        for future in as_completed(futures):
            email = futures[future]
            try:
                result = future.result()
                if result["draft_id"] is not None:
                    prepared.append(result)
                    print(f"Drafted email to {email}")
            except Exception as e:
                failed.append((email, str(e)))
                print(f"Failed to draft for {email}: {e}")

    if auto_send:
        summary = f"Prepared {len(prepared)} of {len(pending)} outreach drafts. Newly prepared drafts are sending automatically."
    else:
        summary = f"Prepared {len(prepared)} of {len(pending)} outreach drafts. Review and send them on the Outreach page."
    if quota_trimmed:
        summary += (
            f"\n\n{quota_trimmed} more were left undrafted: that is all your plan "
            "includes this month."
        )
    if failed:
        summary += "\n\nCouldn't draft:\n" + "\n".join(f"- {email}: {err}" for email, err in failed)
    subject = "Outreach drafts are sending automatically" if auto_send else "Outreach drafts ready for review"
    notify(account, subject, summary)
    print(summary)

    return {
        "prepared": len(prepared),
        "total": len(pending),
        "failed": [{"email": email, "error": err} for email, err in failed],
        "daily_limit": readiness["daily_limit"],
        "remaining_today": readiness["remaining_today"],
        "quota_trimmed": quota_trimmed,
        "draft_ids": [draft["draft_id"] for draft in prepared],
    }


def send_prepared_draft(account, draft, subject=None, body=None, rewritten=False):
    """Sends one approved draft, then records it. `subject`/`body` override the
    stored copy when the operator edited it in the UI. The one-click opt-out link
    and List-Unsubscribe headers are (re)applied here, so a legally-required
    unsubscribe is present even if the operator edited it out.

    rewritten says whether an AI rewrite touched this draft before it was
    sent (see agent.rewrite_outreach_email / drafts_db.mark_rewritten) -- for
    Phase 6's future edit-diff corpus to exclude or label these pairs rather
    than learn from the model's own rewrite as if it were the operator's
    voice. Called from exactly two places: the single-draft send endpoint
    (which passes whatever the request said) and send_all_prepared's batch
    loop (which passes nothing, so this stays False). That's correct by
    construction, not luck -- a server-side batch send has no browser-side
    rewritten state to pass -- but it reads as obvious right up until someone
    adds a parameter near it later, hence writing it down here.

    Returns {"thread_id", "body", "sheet_error"}, where sheet_error is a message
    when the email went out but its sheet row could not be updated. That is a
    repair task, not a failed send, and the distinction is the whole point of the
    ordering below.

    The write order is load-bearing. The Gmail send is irreversible and has to go
    first -- nothing else can produce the thread id, and writing "Sent" before
    sending would mark mail that never left. So the moment it succeeds, the very
    next thing is the durable record that stops it ever being sent again: the
    draft-queue update, one row in our own database with no re-verification to
    fail. The sheet, which is a spreadsheet the operator edits underneath us,
    comes last and is allowed to fail.

    Getting this backwards is what a deleted sheet row used to cost: the email
    left, mark_row_sent raised permanently on the missing row, drafts_db.mark_sent
    never ran, the draft stayed pending, and the next batch sent the same email to
    the same prospect -- every batch, without limit."""
    # Before anything irreversible: confirm the send can be recorded at all. An
    # unrecordable send is a duplicate waiting to happen, so it must not start.
    drafts_db.require_send_log()

    email = draft["email"]
    subject = draft["subject"] if subject is None else subject
    body = draft["body"] if body is None else body

    unsub_url = auth.unsubscribe_url(account["id"], email)
    if unsub_url not in body:
        body = f"{body.rstrip()}\n\n{agent._opt_out_line(unsub_url)}"

    thread_id = gmail.send_email(account, email, subject, body, unsubscribe_url=unsub_url)

    # Irreversible act done. Record it before anything else is attempted, and
    # retry it, because this is now the record that prevents a duplicate.
    _with_retries(
        lambda: drafts_db.mark_sent(
            account["id"], draft["id"], subject, body,
            thread_id=thread_id,
            follow_up_delay_days=account.get("follow_up_delay_days") or 3,
        ),
        f"Email was sent to {email}, but recording it in the draft queue failed",
    )
    # Separate, best-effort -- never part of the critical write above. See
    # drafts_db.mark_rewritten for why this must never be able to make that
    # write (or this send) fail.
    if rewritten:
        drafts_db.mark_rewritten(account["id"], draft["id"])

    sheet_error = None
    try:
        mark_row_sent(account, draft["row_index"], email, thread_id, body)
    except Exception as e:
        # The email is out and recorded, so this cannot cause a re-send. It only
        # means the customer's sheet does not show what happened.
        sheet_error = (
            f"{email} was emailed successfully, but its sheet row was not updated "
            f"({e}). Set that row's Status to 'Sent' so your records match; it will "
            "not be emailed again either way."
        )
        print(sheet_error)
    return {"thread_id": thread_id, "body": body, "sheet_error": sheet_error}


def send_all_prepared(account, draft_ids=None):
    """Send every pending draft, one at a time, human-paced. The suppression
    list and EmailConfidence are re-checked before EACH send (someone can opt
    out or a sourced address can be unverified mid-batch); the
    daily cap is read ONCE up front and decremented locally per send rather
    than re-fetching the whole sheet every iteration (that was N sheet reads
    for N drafts). Drips with a randomized gap so the batch doesn't leave in
    one burst. Sheet edits/deletions are tolerated per-draft: one bad row must
    not stop the rest."""
    # Derive the safety policy from the durable account setting here, at the
    # only batch-send implementation. Callers cannot opt out accidentally by
    # forgetting a flag or explicitly passing the wrong one.
    unattended = (account.get("outreach_send_mode") or "manual") == "auto"

    # Drafts prepared before the rate crossed the line must not go out either --
    # the check belongs on the send, not just on the drafting.
    bounces.assert_sendable(account)
    drafts = drafts_db.list_pending_drafts(account["id"])
    if draft_ids is not None:
        selected_ids = set(draft_ids)
        drafts = [draft for draft in drafts if draft["id"] in selected_ids]
    sent = []
    skipped = []
    failed = []
    needs_review = []
    sheet_warnings = []

    # One sheet read for the whole batch, serving both the cap and the
    # already-emailed guard below. The lock in the /send-all endpoint keeps a
    # second batch from running for this account concurrently, so a locally
    # tracked counter stays accurate: every send below is exactly one against the
    # cap, and anything sent before this batch is already reflected.
    rows = sheets.get_all_rows(account)
    remaining = sheets.campaign_readiness(
        account, sent_today=drafts_db.count_sent_last_24_hours(account["id"]), rows=rows
    )["remaining_today"]
    already_emailed = sheets.already_emailed_rows(rows)

    for i, draft in enumerate(drafts):
        email = draft["email"]
        if remaining <= 0:
            skipped.append((email, "daily limit reached"))
            continue
        if suppressions_db.is_suppressed(account["id"], email):
            drafts_db.discard(account["id"], draft["id"])
            skipped.append((email, "recipient unsubscribed"))
            continue
        if sheets.email_confidence(account, email, rows=rows).lower() == "unverified":
            # A draft may have existed before the verification gate was added,
            # or an operator may have changed the sheet after it was drafted.
            # Retire it rather than leave a permanently blocked pending draft;
            # the row remains for the operator to verify and prepare again.
            drafts_db.discard(account["id"], draft["id"])
            skipped.append((email, "address is still marked unverified in the sheet"))
            continue
        if draft["row_index"] in already_emailed:
            # The sheet says this row already went out while the draft queue still
            # says pending. The only way to reach that is a send whose queue
            # update failed, so the email is out: retire the draft instead of
            # sending a second copy. Free -- it reads the sheet fetched above.
            drafts_db.discard(account["id"], draft["id"])
            skipped.append((email, "already marked Sent in the sheet"))
            continue
        if unattended:
            # Auto mode has no human between this durable row and Gmail. Apply
            # the validator to the exact copy about to leave, not merely the
            # model output that originally created the row. A rejected draft
            # stays pending so the operator can repair and send it manually.
            unsubscribe_url = auth.unsubscribe_url(account["id"], email)
            problems = agent.outreach_problems(
                account, draft.get("subject"), draft.get("body"), unsubscribe_url
            )
            if problems:
                needs_review.append({"email": email, "problems": problems})
                continue
        try:
            result = send_prepared_draft(account, draft)
            # Counted against the cap on the strength of the send, not of the
            # sheet write. The cap is a domain-reputation control, so it has to
            # count emails that actually left; deferring the decrement to a
            # successful sheet write let it under-count and over-send.
            sent.append(email)
            remaining -= 1
            if result["sheet_error"]:
                sheet_warnings.append(result["sheet_error"])
            print(f"Sent to {email}")
        except Exception as e:
            failed.append((email, str(e)))
            print(f"Failed to send to {email}: {e}")

        if i < len(drafts) - 1:
            time.sleep(random.uniform(_SEND_ALL_MIN_GAP, _SEND_ALL_MAX_GAP))

    summary = f"Sent {len(sent)} of {len(drafts)} approved drafts."
    if skipped:
        summary += "\n\nSkipped:\n" + "\n".join(f"- {email}: {why}" for email, why in skipped)
    if failed:
        summary += "\n\nFailed:\n" + "\n".join(f"- {email}: {err}" for email, err in failed)
    if needs_review:
        summary += "\n\nHeld for manual review:\n" + "\n".join(
            f"- {item['email']}: {'; '.join(item['problems'])}"
            for item in needs_review
        )
    if sheet_warnings:
        # Kept apart from `failed` on purpose: these were delivered. Filing them
        # as failures is what invited an operator to send them again.
        summary += "\n\nSent, but your sheet needs a manual fix:\n" + "\n".join(
            f"- {w}" for w in sheet_warnings
        )
    notify(account, "Outreach batch sent", summary)
    print(summary)
    return {
        "sent": len(sent),
        "skipped": [{"email": e, "reason": r} for e, r in skipped],
        "failed": [{"email": e, "error": err} for e, err in failed],
        "needs_review": needs_review,
        "sheet_warnings": sheet_warnings,
    }


def main(account):
    """CLI entry point: prepare a batch of drafts for review."""
    return prepare_drafts(account)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python send_outreach.py <account-email>")
    acct = accounts_db.get_account_by_email(sys.argv[1])
    if not acct:
        sys.exit(f"No account found for {sys.argv[1]}")
    usage.set_account(acct["id"])
    main(acct)
