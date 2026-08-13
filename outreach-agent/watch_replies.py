"""Polls Gmail for replies to previously-sent outreach emails, across every
connected account in one process. When a reply is found, drafts a response
with the LLM and queues it in the app's review list -- this script never
sends the reply itself, and never emails the account owner about it (the
/outreach page is the review surface).

Run once to check every account immediately:  python watch_replies.py --once
Run continuously (every 5 min, all accounts): python watch_replies.py
Debug one account only, no heartbeat written: python watch_replies.py <account-email> [--once]
"""
import sys
import time

import schedule

import accounts_db
import agent
import auth
import bounces
import drafts_db
import gmail
import plans
import reviews_db
import sheets
import suppressions_db
import usage

CHECK_INTERVAL_MINUTES = 5

# Gap between accounts within one cycle. "Stagger accounts, don't fan out":
# Gmail's per-account quota doesn't need concurrency, and a burst of
# simultaneous getProfile/sendAs.list/thread-read calls across every account
# is exactly the failure mode a delay this small avoids. Costs nothing
# against the 5-minute interval at today's account count.
INTER_ACCOUNT_DELAY_SECONDS = 2


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

    # Once per account per cycle, not per row -- it's a Gmail profile call
    # (plus a Send-As listing call) reused by every row below. See
    # gmail.get_own_addresses for what `degraded` means and why it has to
    # travel onto every review created while it's true.
    own_addresses, degraded = gmail.get_own_addresses(account)
    try:
        active_follow_ups = {
            draft.get("thread_id"): draft
            for draft in drafts_db.list_active_follow_ups(account["id"])
            if draft.get("thread_id")
        }
    except Exception as e:
        # Follow-up state is additive. A missing migration or temporary
        # Supabase failure must never switch off the older, load-bearing reply
        # and bounce detection loop.
        active_follow_ups = {}
        print(f"Follow-up scheduling unavailable this cycle: {e}")

    new_reviews = []
    row_errors = []
    new_bounces = []

    def cancel_follow_up(follow_up, reason, *, replied=False):
        """Best-effort cleanup that can never hide an incoming reply.

        The send endpoint repeats these checks immediately before Gmail, so a
        temporary database failure here remains fail-closed without disabling
        the older reply queue.
        """
        if not follow_up:
            return
        try:
            if replied:
                drafts_db.mark_follow_up_replied(account["id"], follow_up["id"])
            else:
                drafts_db.cancel_follow_up(account["id"], follow_up["id"], reason)
            reviews_db.dismiss_follow_up_for_source(account["id"], follow_up["id"])
        except Exception as e:
            row_errors.append(
                f"{follow_up.get('email', 'contact')}: could not record follow-up "
                f"cancellation ({e}); the send-time guard will check it again."
            )
    for row_index, row in sent_rows:
        thread_id = row[sheets.COL_THREAD_ID].strip()
        if not thread_id:
            continue

        name = row[sheets.COL_NAME].strip()
        email = row[sheets.COL_EMAIL].strip()
        follow_up = active_follow_ups.get(thread_id)

        try:
            # One read for both checks below. They ask different questions of the
            # same messages, and Gmail charges per fetch.
            thread = gmail.get_thread(account, thread_id)
            candidate = gmail.get_latest_reply_with_history(
                account, thread_id, email, own_addresses, thread=thread
            )
            if candidate is None:
                # No new message from them -- but the thread may be carrying a
                # bounce, which get_latest_reply_with_history() filters out
                # (it only surfaces messages sent by the contact themselves).
                bounce = gmail.find_bounce(account, thread_id, email, thread=thread)
                if bounce:
                    bounces.record(account, row_index, email, bounce)
                    cancel_follow_up(follow_up, "bounce")
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

                if follow_up and drafts_db.is_follow_up_due(follow_up):
                    if suppressions_db.is_suppressed(account["id"], email):
                        cancel_follow_up(follow_up, "opt_out")
                        continue
                    unsubscribe_url = auth.unsubscribe_url(account["id"], email)
                    try:
                        plans.check(account, usage.UNIT_DRAFT_EMAIL)
                        draft = agent.draft_follow_up(
                            account, name, row[sheets.COL_COMPANY].strip(),
                            follow_up.get("body") or row[sheets.COL_EMAIL_BODY].strip(),
                            unsubscribe_url=unsubscribe_url,
                        )
                    except Exception as e:
                        draft = ""
                        row_errors.append(
                            f"{name or email}: follow-up is due, but drafting failed ({e}). "
                            "Write it yourself in the review queue."
                        )
                    review_id = reviews_db.add_follow_up(
                        account["id"], follow_up["id"], row_index, name, email,
                        thread_id, draft,
                        validator_problems=agent.follow_up_problems(
                            account, draft, unsubscribe_url=unsubscribe_url
                        ),
                    )
                    try:
                        drafts_db.mark_follow_up_queued(account["id"], follow_up["id"])
                    except Exception:
                        # Leave the source in waiting so the next watcher cycle
                        # can retry instead of exposing an unsendable orphan.
                        reviews_db.dismiss_follow_up_for_source(account["id"], follow_up["id"])
                        raise
                    new_reviews.append(reviews_db.get_review(account["id"], review_id))
                    print(f"Follow-up due for {name or email}; queued for manual review.")
                continue

            cancel_follow_up(follow_up, "reply", replied=True)

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
                account["id"], thread_id, candidate["message_id"], candidate["text"]
            ) is not None:
                continue

            # Trim the quoted history off before this is stored or shown. A
            # reply on a long thread carries the entire conversation back at us,
            # and an operator scanning a queue at a few seconds per item cannot
            # find the two new sentences inside 20KB of '>' lines. strip_quoted
            # keeps inline replies whole -- it trims the tail, never cuts at the
            # first marker -- so an answer woven between quoted lines survives.
            reply_body = gmail.strip_quoted(candidate["text"])

            if candidate["answered_elsewhere"]:
                # The operator already replied from Gmail directly, before
                # this poll ran -- nothing to draft or send. Queued for
                # visibility only: worth knowing, not worth notifying on (no
                # notification path exists yet regardless -- that's Phase 2).
                review_id = reviews_db.add_review(
                    account["id"], row_index, name, email, thread_id, reply_body, "",
                    gmail_message_id=candidate["message_id"], status="answered_elsewhere",
                    degraded_classification=degraded,
                )
                new_reviews.append(reviews_db.get_review(account["id"], review_id))
                print(f"{name or email} already answered elsewhere; queued for visibility only.")
                try:
                    sheets.update_row(account, row_index, status="Replied", expect_email=email)
                except Exception as e:
                    row_errors.append(
                        f"{name or email}: already-answered reply noted, but marking "
                        f"the sheet 'Replied' failed: {e}"
                    )
                continue

            if candidate["kind"] == "off_sheet":
                # Not drafted here. This is the least-trusted input the system
                # sees -- nobody vetted this address the way a sheet contact
                # was vetted -- so it waits for a human to confirm the sender
                # before an LLM ever sees it (drafting happens in the
                # confirm-sender endpoint, server.py). email is the *observed*
                # sender, not the sheet's contact address, since that's who a
                # reply actually has to reach.
                review_id = reviews_db.add_review(
                    account["id"], row_index, name, candidate["from_addr"], thread_id,
                    reply_body, "", gmail_message_id=candidate["message_id"],
                    status="flagged", degraded_classification=degraded,
                )
                new_reviews.append(reviews_db.get_review(account["id"], review_id))
                print(f"Off-sheet reply from {candidate['from_addr']} on {name or email}'s "
                      "thread, flagged for sender confirmation.")
                try:
                    sheets.update_row(account, row_index, status="Replied", expect_email=email)
                except Exception as e:
                    row_errors.append(
                        f"{name or email}: off-sheet reply flagged for review, but "
                        f"marking the sheet 'Replied' failed: {e}"
                    )
                continue

            company = row[sheets.COL_COMPANY].strip()
            # A failed draft must not cost the notification. Every provider
            # being down, an exhausted free-tier daily quota, or this
            # account's own reply-draft plan quota, is a bad afternoon for the
            # drafting feature and nothing at all to do with whether a
            # prospect wrote back -- but if the exception escapes here,
            # add_review never runs, no review row exists, and the operator is
            # never told. The reply is the product; the draft is a
            # convenience. Queue it empty and let them write their own.
            try:
                plans.check(account, usage.UNIT_DRAFT_REPLY)
                draft = agent.draft_reply(account, name, company, reply_body, candidate["history"])
            except plans.QuotaExceeded as e:
                # Its own branch, not the generic one below: str(e) is the
                # rich, plan-specific upgrade-path message ("a quota refusal
                # is a sales moment" -- plans.py), and folding it into the
                # generic "drafting a response failed" wrapper would bury it.
                draft = ""
                row_errors.append(f"{name or email}: {e}")
                print(f"Reply-draft quota hit for {name or email}, queueing the reply undrafted: {e}")
            except Exception as e:
                draft = ""
                row_errors.append(
                    f"{name or email}: reply detected and queued, but drafting a "
                    f"response failed ({e}). Write the reply yourself in the app."
                )
                print(f"Draft failed for {name or email}, queueing the reply undrafted: {e}")

            # Review FIRST, sheet status second. The review is the operator
            # surface and the dedupe key: once it exists, a failing status
            # write costs one visible row_error -- never a re-draft per poll.
            # (Reversed, a persistently failing write would re-burn an LLM
            # call every 100s because add_review never runs.)
            review_id = reviews_db.add_review(
                account["id"], row_index, name, email, thread_id, reply_body, draft,
                gmail_message_id=candidate["message_id"], degraded_classification=degraded,
                # draft_reply hands back its last attempt even when validation
                # still fails, so record what was still wrong with it. The
                # operator gets told why a draft is suspect instead of having to
                # notice. Does not affect the lane -- a reply is full-text
                # whatever this says.
                validator_problems=agent.reply_problems(account, draft),
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


def run_all_accounts(accounts=None, write_heartbeat=True):
    """Walks every eligible account once, sequentially -- never concurrently.
    A ThreadPoolExecutor across accounts (the pattern send_outreach.py uses
    for outreach sends) would be exactly the fan-out this is meant to avoid.

    accounts defaults to every account with a configured sheet
    (accounts_db.list_accounts_with_a_sheet -- deliberately not also
    filtered on google_token; see that function's docstring for why a
    token-based exclusion would freeze the very observability this loop is
    supposed to provide). Pass an explicit list to restrict a run to
    specific accounts, as main()'s single-account debug mode does.

    write_heartbeat is False for that debug mode: a one-off manual run
    against one account must not overwrite worker_heartbeat_at and forge
    the "the continuous worker is alive for this account" signal.

    Returns a summary dict; also prints one line per account plus a
    cycle-level summary, so a glance at the log answers "did the last cycle
    run" without correlating per-account lines by hand."""
    started = time.monotonic()
    if accounts is None:
        try:
            accounts = accounts_db.list_accounts_with_a_sheet()
        except Exception as e:
            print(f"Could not list accounts for this reply-watch cycle: {e}")
            return {"checked": 0, "skipped_no_token": 0, "failed": 0, "elapsed": 0.0}

    checked = skipped_no_token = failed = 0
    for i, account in enumerate(accounts):
        account_id = account["id"]
        label = account.get("email") or account_id

        if not account.get("google_token"):
            # Skip the Gmail work, not the account: an account stays in
            # rotation with a fresh heartbeat and a specific, actionable
            # last_error, rather than silently dropping out of the list the
            # moment its token dies (Testing-mode tokens do, on their own,
            # every 7 days -- the normal lifecycle of every account today).
            skipped_no_token += 1
            if write_heartbeat:
                accounts_db.set_worker_heartbeat(
                    account_id, error="Google disconnected — reconnect in Settings"
                )
            print(f"{label}: no Google connection, skipped this cycle.")
        else:
            try:
                result = usage.run_as(account_id, check_for_replies, account)
                checked += 1
                if write_heartbeat:
                    accounts_db.set_worker_heartbeat(account_id, error=None)
                print(
                    f"{label}: {len(result['reviews'])} review(s), "
                    f"{len(result['row_errors'])} row error(s), "
                    f"{len(result['bounces'])} bounce(s)."
                )
            except Exception as e:
                # One account's failure must not stop the loop -- the whole
                # point of this phase. check_for_replies already isolates
                # per row; this is the same discipline one level up, for
                # whatever can still fail before or around that loop
                # (a revoked token discovered mid-cycle, a deleted sheet).
                failed += 1
                if write_heartbeat:
                    accounts_db.set_worker_heartbeat(account_id, error=str(e))
                print(f"{label}: reply check failed for the whole account: {e}")

        if i < len(accounts) - 1:
            time.sleep(INTER_ACCOUNT_DELAY_SECONDS)

    elapsed = time.monotonic() - started
    print(
        f"Cycle done: {checked} checked, {skipped_no_token} skipped (no Google "
        f"connection), {failed} failed, {elapsed:.1f}s elapsed."
    )
    if elapsed > CHECK_INTERVAL_MINUTES * 60:
        # schedule doesn't overlap runs, it just starts the next one late --
        # without this warning, detection latency quietly grows with the
        # only symptom being "replies take longer to show up," discovered by
        # a customer instead of a log line. And the fix at that point is not
        # a shorter INTER_ACCOUNT_DELAY_SECONDS: if a cycle is already this
        # slow anywhere near today's account count, the sequential
        # single-process model itself is wrong for that count, and the real
        # fix is sharding accounts across more than one worker or
        # reintroducing concurrency with real per-account rate limiting.
        print(
            f"WARNING: this cycle took {elapsed:.1f}s, longer than the "
            f"{CHECK_INTERVAL_MINUTES}-minute interval between cycles."
        )
    return {"checked": checked, "skipped_no_token": skipped_no_token, "failed": failed, "elapsed": elapsed}


def main():
    args = [a for a in sys.argv[1:] if a != "--once"]
    once = "--once" in sys.argv

    if args:
        account = accounts_db.get_account_by_email(args[0])
        if not account:
            sys.exit(f"No account found for {args[0]}")
        run = lambda: run_all_accounts(accounts=[account], write_heartbeat=False)
        label = f"{args[0]} only (debug mode -- no heartbeat written)"
    else:
        run = run_all_accounts
        label = "every connected account"

    if once:
        run()
        return

    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run)
    print(f"Watching {label} every {CHECK_INTERVAL_MINUTES} minutes. Ctrl+C to stop.")
    run()
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
