"""Reads pending rows from the account's Google Sheet, sends a personalized
outreach email to each from the account's Gmail, updates the sheet, and sends
a summary notification.

Run manually per batch:  python send_outreach.py <account-email>
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import accounts_db
import agent
import gmail
import sheets
import usage
from notify import notify

# Generation + send happen concurrently per contact instead of one at a time.
# Kept modest: OpenRouter's free-tier model is rate-limited to ~20 req/min,
# so higher concurrency mostly means more 429-retry waiting, not more speed.
MAX_WORKERS = 4


# Post-send sheet marking: attempts and base backoff (seconds). The delay is
# a module constant so tests can zero it.
_MARK_RETRIES = 3
_MARK_RETRY_DELAY = 2


# Per-contact pipeline -- the ordering IS the crash-safety invariant:
#
#   draft (LLM) ──▶ gmail.send ──▶ mark row "Sent" (retry x3, backoff)
#      │               │               │
#      ▼               ▼               ▼
#   fail: row       fail: row      still failing: LABELED error
#   stays Pending,  stays Pending  ("email WAS sent") -- operator must
#   safe to retry   safe to retry  mark the row or it re-emails next batch
#
# The mark happens HERE, immediately after the irreversible side effect,
# never at batch end: a crash mid-batch must not leave sent rows Pending
# (learning: batch-end-write-defeats-send-lock). The retry exists because a
# transient Sheets 429/500 on this one write would otherwise convert a
# SUCCESSFUL send into a future duplicate email.
def _send_one(account, row_index, name, email, company):
    subject, body = agent.generate_outreach_email(account, name, company)
    thread_id = gmail.send_email(account, email, subject, body)
    sent_at = datetime.now(timezone.utc).isoformat()
    last_err = None
    for attempt in range(_MARK_RETRIES):
        try:
            sheets.update_row(
                account, row_index, status="Sent", thread_id=thread_id,
                sent_at=sent_at, email_body=body, expect_email=email,
            )
            break
        except Exception as e:
            last_err = e
            if attempt < _MARK_RETRIES - 1:
                time.sleep(_MARK_RETRY_DELAY * (attempt + 1))
    else:
        # The email DID go out -- surface that loudly instead of letting this
        # look like a failed send, and tell the operator what to fix.
        raise RuntimeError(
            f"Email was sent to {email}, but marking the sheet row failed after "
            f"{_MARK_RETRIES} attempts ({last_err}). Set that row's Status to "
            f"'Sent' manually or it will be re-emailed next batch."
        ) from last_err
    return {
        "row_index": row_index,
        "email": email,
        "thread_id": thread_id,
        "sent_at": sent_at,
        "email_body": body,
    }


def main(account):
    # Writes go into columns D-I; refuse the whole batch unless row 1
    # explicitly labels them (see sheets.require_full_header).
    sheets.require_full_header(account)
    readiness = sheets.campaign_readiness(account)
    pending = [
        (row_index, row[sheets.COL_NAME].strip(), row[sheets.COL_EMAIL].strip(), row[sheets.COL_COMPANY].strip())
        for row_index, row in readiness["eligible"]
    ]

    if not pending:
        return {
            "sent": 0, "total": 0, "failed": [],
            "daily_limit": readiness["daily_limit"],
            "remaining_today": readiness["remaining_today"],
        }

    sent = []
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(usage.run_as, account["id"], _send_one, account, *p): p[2] for p in pending}
        for future in as_completed(futures):
            email = futures[future]
            try:
                result = future.result()
                sent.append(result)
                print(f"Sent to {result['email']}")
            except Exception as e:
                failed.append((email, str(e)))
                print(f"Failed to send to {email}: {e}")

    summary = f"Sent {len(sent)} of {len(pending)} outreach emails."
    if failed:
        summary += "\n\nFailed:\n" + "\n".join(f"- {email}: {err}" for email, err in failed)

    notify(account, "Outreach batch complete", summary)
    print(summary)

    return {
        "sent": len(sent),
        "total": len(pending),
        "failed": [{"email": email, "error": err} for email, err in failed],
        "daily_limit": readiness["daily_limit"],
        "remaining_today": max(0, readiness["remaining_today"] - len(sent)),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python send_outreach.py <account-email>")
    acct = accounts_db.get_account_by_email(sys.argv[1])
    if not acct:
        sys.exit(f"No account found for {sys.argv[1]}")
    usage.set_account(acct["id"])
    main(acct)
