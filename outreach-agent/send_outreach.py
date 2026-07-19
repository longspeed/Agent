"""Reads pending rows from the account's Google Sheet, sends a personalized
outreach email to each from the account's Gmail, updates the sheet, and sends
a summary notification.

Run manually per batch:  python send_outreach.py <account-email>
"""
import sys
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


def _send_one(account, row_index, name, email, company):
    subject, body = agent.generate_outreach_email(account, name, company)
    thread_id = gmail.send_email(account, email, subject, body)
    sent_at = datetime.now(timezone.utc).isoformat()
    # Mark the row Sent immediately, not at batch end: a crash or restart
    # mid-batch must not leave already-emailed rows looking Pending, or the
    # next batch re-emails everyone the interrupted one already reached.
    try:
        sheets.update_row(
            account, row_index, status="Sent", thread_id=thread_id,
            sent_at=sent_at, email_body=body, expect_email=email,
        )
    except Exception as e:
        # The email DID go out -- surface that loudly instead of letting this
        # look like a failed send, and tell the operator what to fix.
        raise RuntimeError(
            f"Email was sent to {email}, but marking the sheet row failed ({e}). "
            f"Set that row's Status to 'Sent' manually or it will be re-emailed next batch."
        ) from e
    return {
        "row_index": row_index,
        "email": email,
        "thread_id": thread_id,
        "sent_at": sent_at,
        "email_body": body,
    }


def main(account):
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
