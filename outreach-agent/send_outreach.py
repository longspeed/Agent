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
import drafts_db
import gmail
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


def mark_row_sent(account, row_index, email, thread_id, body):
    """Marks a sheet row "Sent" right after the irreversible Gmail send, with a
    bounded retry. The ordering (send, THEN mark, immediately) is the crash-
    safety invariant: a transient Sheets 429/500 on this one write must not
    convert a successful send into a future duplicate email, and the mark must
    never be deferred to batch end (learning: batch-end-write-defeats-send-lock).
    Raises only after exhausting the retries, and says plainly that the email
    DID go out so the operator fixes the row instead of re-sending."""
    sent_at = datetime.now(timezone.utc).isoformat()
    last_err = None
    for attempt in range(_MARK_RETRIES):
        try:
            sheets.update_row(
                account, row_index, status="Sent", thread_id=thread_id,
                sent_at=sent_at, email_body=body, expect_email=email,
            )
            return sent_at
        except Exception as e:
            last_err = e
            if attempt < _MARK_RETRIES - 1:
                time.sleep(_MARK_RETRY_DELAY * (attempt + 1))
    raise RuntimeError(
        f"Email was sent to {email}, but marking the sheet row failed after "
        f"{_MARK_RETRIES} attempts ({last_err}). Set that row's Status to "
        f"'Sent' manually or it will be re-emailed next batch."
    ) from last_err


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
    return {"row_index": row_index, "email": email, "draft_id": draft_id, "subject": subject}


def prepare_drafts(account):
    """Generate a batch of drafts for every eligible contact and queue them for
    review. Eligible = approved (blank Status), has an email, not suppressed,
    within today's remaining send allowance."""
    # Fail fast if the sheet can't be marked Sent later, before spending LLM
    # calls generating drafts that could never be sent (same consent gate the
    # actual send relies on).
    sheets.require_full_header(account)
    suppressed = suppressions_db.list_suppressed_emails(account["id"])
    readiness = sheets.campaign_readiness(account, suppressed_emails=suppressed)
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
        }

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

    summary = f"Prepared {len(prepared)} of {len(pending)} outreach drafts. Review and send them on the Outreach page."
    if failed:
        summary += "\n\nCouldn't draft:\n" + "\n".join(f"- {email}: {err}" for email, err in failed)
    notify(account, "Outreach drafts ready for review", summary)
    print(summary)

    return {
        "prepared": len(prepared),
        "total": len(pending),
        "failed": [{"email": email, "error": err} for email, err in failed],
        "daily_limit": readiness["daily_limit"],
        "remaining_today": readiness["remaining_today"],
    }


def send_prepared_draft(account, draft, subject=None, body=None):
    """Sends one approved draft and marks its sheet row. `subject`/`body`
    override the stored copy when the operator edited it in the UI. The one-
    click opt-out link and List-Unsubscribe headers are (re)applied here, so a
    legally-required unsubscribe is present even if the operator edited it out.
    Returns (thread_id, sent_body)."""
    email = draft["email"]
    subject = draft["subject"] if subject is None else subject
    body = draft["body"] if body is None else body

    unsub_url = auth.unsubscribe_url(account["id"], email)
    if unsub_url not in body:
        body = f"{body.rstrip()}\n\n{agent._opt_out_line(unsub_url)}"

    thread_id = gmail.send_email(account, email, subject, body, unsubscribe_url=unsub_url)
    mark_row_sent(account, draft["row_index"], email, thread_id, body)
    return thread_id, body


def send_all_prepared(account):
    """Send every pending draft, one at a time, human-paced. The suppression
    list is re-checked before EACH send (someone can opt out mid-batch); the
    daily cap is read ONCE up front and decremented locally per send rather
    than re-fetching the whole sheet every iteration (that was N sheet reads
    for N drafts). Drips with a randomized gap so the batch doesn't leave in
    one burst. Sheet edits/deletions are tolerated per-draft: one bad row must
    not stop the rest."""
    drafts = drafts_db.list_pending_drafts(account["id"])
    sent = []
    skipped = []
    failed = []

    # One sheet read for the whole batch. The lock in the /send-all endpoint
    # keeps a second batch from running for this account concurrently, so a
    # locally-tracked counter stays accurate: every send below is exactly one
    # against the cap, and anything sent before this batch is already reflected.
    remaining = sheets.campaign_readiness(account)["remaining_today"]

    for i, draft in enumerate(drafts):
        email = draft["email"]
        if remaining <= 0:
            skipped.append((email, "daily limit reached"))
            continue
        if suppressions_db.is_suppressed(account["id"], email):
            drafts_db.discard(account["id"], draft["id"])
            skipped.append((email, "recipient unsubscribed"))
            continue
        try:
            _, sent_body = send_prepared_draft(account, draft)
            drafts_db.mark_sent(account["id"], draft["id"], draft["subject"], sent_body)
            sent.append(email)
            remaining -= 1
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
    notify(account, "Outreach batch sent", summary)
    print(summary)
    return {
        "sent": len(sent),
        "skipped": [{"email": e, "reason": r} for e, r in skipped],
        "failed": [{"email": e, "error": err} for e, err in failed],
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
