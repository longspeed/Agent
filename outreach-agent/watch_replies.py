"""Polls Gmail for replies to previously-sent outreach emails for one account.
When a reply is found, drafts a response with the LLM and queues it in the
app's review list -- this script never sends the reply itself, and never
emails the account owner about it (the /outreach page is the review surface).

Run once to check immediately:      python watch_replies.py <account-email> --once
Run continuously (checks every 5 min): python watch_replies.py <account-email>
"""
import sys
import time

import schedule

import accounts_db
import agent
import bounces
import gmail
import reviews_db
import sheets
import usage

CHECK_INTERVAL_MINUTES = 5


def check_for_replies(account):
    """Checks every eligible row and returns {"reviews": [...], "row_errors": [...]}.
    Rows are isolated: one row failing (deleted Gmail thread, transient API
    error) must not block the rows after it, or a single stale ThreadID would
    silently stop reply detection for the rest of the sheet on every run."""
    sent_rows = sheets.get_reply_check_rows(account)
    if not sent_rows:
        print("No sent rows awaiting replies.")
        return {"reviews": [], "row_errors": [], "bounces": []}
    # This run writes "Replied" statuses -- same full-header consent gate as
    # the send path (see sheets.require_full_header).
    sheets.require_full_header(account)

    new_reviews = []
    row_errors = []
    new_bounces = []
    for row_index, row in sent_rows:
        thread_id = row[sheets.COL_THREAD_ID].strip()
        if not thread_id:
            continue

        name = row[sheets.COL_NAME].strip()
        email = row[sheets.COL_EMAIL].strip()

        try:
            # One read for both checks below. They ask different questions of the
            # same messages, and Gmail charges per fetch.
            thread = gmail.get_thread(account, thread_id)
            reply_text, history, message_id = gmail.get_latest_reply_with_history(
                account, thread_id, email, thread=thread
            )
            if not reply_text:
                # No new message from them -- but the thread may be carrying a
                # bounce, which get_latest_reply_with_history() filters out
                # (it only surfaces messages sent by the contact themselves).
                bounce = gmail.find_bounce(account, thread_id, email, thread=thread)
                if bounce:
                    bounces.record(account, row_index, email, bounce)
                    new_bounces.append({
                        "row": row_index, "email": email,
                        "code": bounce["code"], "permanent": bounce["permanent"],
                    })
                    outcome = (
                        "suppressed" if bounce["permanent"]
                        else "marked Delayed; still sendable and still watched for a reply"
                    )
                    print(f"Bounce for {email} ({bounce['code']}) -- {outcome}.")
                continue

            # A reply that already has a review -- pending, sent, or dismissed --
            # was handled in an earlier check. Skip before drafting: the auto-poll
            # runs every 100s, and drafting first would burn an LLM call per poll
            # per unanswered reply (and resurface dismissed reviews as "new").
            #
            # Deliberately handed the RAW body, not the trimmed one below. The
            # message id is the real key; the body is only consulted as a legacy
            # fallback for rows written before that column existed, and those
            # rows stored the untrimmed text. Passing the trimmed version would
            # fail to match them and queue one duplicate review per already-
            # reviewed thread -- which on a fast queue is one duplicate reply to
            # a prospect.
            if reviews_db.find_review_id(
                account["id"], thread_id, message_id, reply_text
            ) is not None:
                continue

            # Trim the quoted history off before this is stored or shown. A
            # reply on a long thread carries the entire conversation back at us,
            # and an operator scanning a queue at a few seconds per item cannot
            # find the two new sentences inside 20KB of '>' lines. strip_quoted
            # keeps inline replies whole -- it trims the tail, never cuts at the
            # first marker -- so an answer woven between quoted lines survives.
            reply_body = gmail.strip_quoted(reply_text)

            company = row[sheets.COL_COMPANY].strip()
            draft = agent.draft_reply(account, name, company, reply_body, history)

            # Review FIRST, sheet status second. The review is the operator
            # surface and the dedupe key: once it exists, a failing status
            # write costs one visible row_error -- never a re-draft per poll.
            # (Reversed, a persistently failing write would re-burn an LLM
            # call every 100s because add_review never runs.)
            review_id = reviews_db.add_review(
                account["id"], row_index, name, email, thread_id, reply_body, draft,
                gmail_message_id=message_id,
            )
            new_reviews.append(reviews_db.get_review(account["id"], review_id))
            print(f"Reply detected from {name}, queued for review in the app.")
            try:
                sheets.update_row(account, row_index, status="Replied", expect_email=email)
            except Exception as e:
                row_errors.append(
                    f"{name or email}: reply queued for review, but marking the "
                    f"sheet 'Replied' failed: {e}"
                )
        except Exception as e:
            label = name or email or f"row {row_index}"
            row_errors.append(f"{label}: {e}")
            print(f"Reply check failed for {label} (row {row_index}): {e}")

    return {"reviews": new_reviews, "row_errors": row_errors, "bounces": new_bounces}


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
