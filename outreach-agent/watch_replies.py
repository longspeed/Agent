"""Polls Gmail for replies to previously-sent outreach emails. When a reply
is found, drafts a response with Claude and notifies the user (Slack + email)
with the draft for manual review/sending -- this script never sends the
reply itself.

Run once to check immediately:      python watch_replies.py --once
Run continuously (checks every 5 min): python watch_replies.py
"""
import sys
import time

import schedule

import agent
import gmail
import reviews_db
import sheets
from notify import notify

CHECK_INTERVAL_MINUTES = 5


def check_for_replies():
    """Returns the list of newly created reviews (empty list if none)."""
    sent_rows = sheets.get_sent_rows()
    if not sent_rows:
        print("No sent rows awaiting replies.")
        return []

    new_reviews = []
    for row_index, row in sent_rows:
        thread_id = row[sheets.COL_THREAD_ID].strip()
        if not thread_id:
            continue

        reply_text = gmail.get_latest_reply(thread_id)
        if not reply_text:
            continue

        name = row[sheets.COL_NAME].strip()
        email = row[sheets.COL_EMAIL].strip()
        original_email = row[sheets.COL_EMAIL_BODY].strip()
        draft = agent.draft_reply(original_email, reply_text)

        sheets.update_row(row_index, status="Replied")
        review_id = reviews_db.add_review(row_index, name, email, thread_id, reply_text, draft)
        new_reviews.append(reviews_db.get_review(review_id))

        notify(
            f"{name} replied - draft response ready",
            f"Customer's reply:\n{reply_text}\n\n"
            f"--- Suggested response (review before sending) ---\n{draft}",
        )
        print(f"Reply detected from {name}, notification sent.")

    return new_reviews


def main():
    if "--once" in sys.argv:
        check_for_replies()
        return

    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(check_for_replies)
    print(f"Watching for replies every {CHECK_INTERVAL_MINUTES} minutes. Ctrl+C to stop.")
    check_for_replies()
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
