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
    return {
        "row_index": row_index,
        "email": email,
        "thread_id": thread_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "email_body": body,
    }


def main(account):
    pending = [
        (row_index, row[sheets.COL_NAME].strip(), row[sheets.COL_EMAIL].strip(), row[sheets.COL_COMPANY].strip())
        for row_index, row in sheets.get_pending_rows(account)
    ]
    pending = [p for p in pending if p[2]]  # p[2] = email

    if not pending:
        print("No pending rows to send.")
        return {"sent": 0, "total": 0, "failed": []}

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

    # One round-trip to the sheet for the whole batch instead of one per row.
    sheets.batch_update_rows(account, [
        {
            "row_index": r["row_index"], "status": "Sent", "thread_id": r["thread_id"],
            "sent_at": r["sent_at"], "email_body": r["email_body"],
        }
        for r in sent
    ])

    summary = f"Sent {len(sent)} of {len(pending)} outreach emails."
    if failed:
        summary += "\n\nFailed:\n" + "\n".join(f"- {email}: {err}" for email, err in failed)

    notify(account, "Outreach batch complete", summary)
    print(summary)

    return {
        "sent": len(sent),
        "total": len(pending),
        "failed": [{"email": email, "error": err} for email, err in failed],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python send_outreach.py <account-email>")
    acct = accounts_db.get_account_by_email(sys.argv[1])
    if not acct:
        sys.exit(f"No account found for {sys.argv[1]}")
    usage.set_account(acct["id"])
    main(acct)
