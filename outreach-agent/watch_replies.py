"""Polls Gmail for replies to previously-sent outreach emails for one account.
When a reply is found, drafts a response with the LLM and notifies the account
owner with the draft for manual review/sending -- this script never sends the
reply itself.

Run once to check immediately:      python watch_replies.py <account-email> --once
Run continuously (checks every 5 min): python watch_replies.py <account-email>
"""
import sys
import time

import schedule

import accounts_db
import agent
import gmail
import reviews_db
import sheets
import usage
from notify import notify

CHECK_INTERVAL_MINUTES = 5


def check_for_replies(account):
    """Returns the list of newly created reviews (empty list if none)."""
    sent_rows = sheets.get_sent_rows(account)
    if not sent_rows:
        print("No sent rows awaiting replies.")
        return []

    new_reviews = []
    for row_index, row in sent_rows:
        thread_id = row[sheets.COL_THREAD_ID].strip()
        if not thread_id:
            continue

        reply_text = gmail.get_latest_reply(account, thread_id)
        if not reply_text:
            continue

        name = row[sheets.COL_NAME].strip()
        email = row[sheets.COL_EMAIL].strip()
        original_email = row[sheets.COL_EMAIL_BODY].strip()
        draft = agent.draft_reply(account, original_email, reply_text)

        sheets.update_row(account, row_index, status="Replied")
        review_id = reviews_db.add_review(
            account["id"], row_index, name, email, thread_id, reply_text, draft
        )
        new_reviews.append(reviews_db.get_review(account["id"], review_id))

        notify(
            account,
            f"{name} replied - draft response ready",
            f"{name} ({email}) replied to your outreach. A draft response is "
            f"ready for review in the app: check the Outreach Agent page.",
        )
        print(f"Reply detected from {name}, notification sent.")

    return new_reviews


def main():
    args = [a for a in sys.argv[1:] if a != "--once"]
    if not args:
        sys.exit("Usage: python watch_replies.py <account-email> [--once]")
    account = accounts_db.get_account_by_email(args[0])
    if not account:
        sys.exit(f"No account found for {args[0]}")
    usage.set_account(account["id"])

    if "--once" in sys.argv:
        check_for_replies(account)
        return

    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(check_for_replies, account)
    print(f"Watching for replies every {CHECK_INTERVAL_MINUTES} minutes. Ctrl+C to stop.")
    check_for_replies(account)
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
