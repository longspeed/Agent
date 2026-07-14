"""Reads pending rows from the Google Sheet, sends a personalized outreach
email to each, updates the sheet, and sends a summary notification.

Run manually per batch:  python send_outreach.py
"""
from datetime import datetime, timezone

import agent
import gmail
import sheets
from notify import notify


def main():
    pending = sheets.get_pending_rows()
    if not pending:
        print("No pending rows to send.")
        return {"sent": 0, "total": 0, "failed": []}

    sent_count = 0
    failed = []

    for row_index, row in pending:
        name = row[sheets.COL_NAME].strip()
        email = row[sheets.COL_EMAIL].strip()
        company = row[sheets.COL_COMPANY].strip()

        if not email:
            continue

        try:
            subject, body = agent.generate_outreach_email(name, company)
            thread_id = gmail.send_email(email, subject, body)
            sheets.update_row(
                row_index,
                status="Sent",
                thread_id=thread_id,
                sent_at=datetime.now(timezone.utc).isoformat(),
                email_body=body,
            )
            sent_count += 1
            print(f"Sent to {email}")
        except Exception as e:
            failed.append((email, str(e)))
            print(f"Failed to send to {email}: {e}")

    summary = f"Sent {sent_count} of {len(pending)} outreach emails."
    if failed:
        summary += "\n\nFailed:\n" + "\n".join(f"- {email}: {err}" for email, err in failed)

    notify("Outreach batch complete", summary)
    print(summary)

    return {
        "sent": sent_count,
        "total": len(pending),
        "failed": [{"email": email, "error": err} for email, err in failed],
    }


if __name__ == "__main__":
    main()
