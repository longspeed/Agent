"""Polls Gmail Sent threads and tracked conversations across every connected
account in one process. It extracts commitments, schedules one disciplined
follow-up, and retains optional reply drafts as a secondary lane. This worker
never sends a reply itself; operational failures and due commitments may still
produce transactional alerts for the account owner.

Run once to check every account immediately:  python watch_replies.py --once
Run continuously (every 5 min, all accounts): python watch_replies.py
Debug one account only, no heartbeat written: python watch_replies.py <account-email> [--once]
"""
import argparse
import os
import re
import sys
import time


CLI_VERSION = "dev"


def build_cli_parser():
    parser = argparse.ArgumentParser(
        prog="watch_replies.py",
        description=(
            "Poll Gmail Sent threads across every connected Sendkeep account. "
            "Promises and one follow-up are tracked; this worker never sends replies."
        ),
        epilog=(
            "Examples:\n"
            "  python watch_replies.py --once\n"
            "  python watch_replies.py you@company.com --once\n"
            "  python watch_replies.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "account_email",
        nargs="?",
        help="only inspect this account (debug mode; no worker heartbeat is written)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit instead of watching continuously",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {CLI_VERSION}",
    )
    return parser


# Parse before importing the runtime modules below. This makes --help,
# --version, and invalid-option errors useful even when a developer has not
# configured Supabase, Google, or the LLM providers yet.
_CLI_ARGS = None
if __name__ == "__main__":
    _CLI_ARGS = build_cli_parser().parse_args()

import schedule

import accounts_db
import agent
import auth
import bounces
import commitments
import commitments_db
import config
import drafts_db
import gmail
import send_capacity_db
import plans
import reviews_db
import sheets
import suppressions_db
import tracked_threads
import usage
import notify as notifications
import worker_db

CHECK_INTERVAL_MINUTES = 5

# Gap between accounts within one cycle. "Stagger accounts, don't fan out":
# Gmail's per-account quota doesn't need concurrency, and a burst of
# simultaneous getProfile/sendAs.list/thread-read calls across every account
# is exactly the failure mode a delay this small avoids. Costs nothing
# against the 5-minute interval at today's account count.
INTER_ACCOUNT_DELAY_SECONDS = 2
_LAST_GMAIL_DISCOVERY_AT: dict[str, float] = {}


def _gmail_discovery_is_due(account_id):
    last = _LAST_GMAIL_DISCOVERY_AT.get(account_id)
    if last is not None and time.monotonic() - last < config.GMAIL_THREAD_DISCOVERY_INTERVAL_MINUTES * 60:
        return False
    return True


def _normalize_for_echo(text):
    """Lowercase, unquote, and collapse whitespace so a message that merely
    re-wraps or re-quotes what we sent still compares equal to it."""
    collapsed = re.sub(r"\s+", " ", gmail.strip_quoted(text or "")).strip().lower()
    return collapsed


def _process_due_commitments(account, row_errors):
    """Move confirmed promises due now into the visible due queue once."""
    try:
        due = commitments_db.list_due(account["id"])
    except Exception:
        row_errors.append("Due commitment processing is temporarily unavailable.")
        return []

    transitioned = []
    for commitment in due:
        try:
            if not commitments_db.mark_due(account["id"], commitment["id"]):
                continue
            item = dict(commitment)
            item["status"] = "due"
            transitioned.append(item)
        except Exception:
            row_errors.append(
                "A due commitment could not be updated. It will be retried."
            )
    return transitioned


def _reconcile_follow_up_sends(account, row_errors):
    """Close ambiguous follow-up sends from Gmail's Sent thread.

    The web request can lose the response after Gmail accepts a message. Those
    rows stay fenced in ``sending`` so they cannot be retried into a duplicate.
    On the next worker cycle, an exact Sent-message match records the send and
    closes the review. A source already marked ``sent`` only needs the secondary
    review write retried. No automatic retry is ever performed.
    """
    try:
        pending = drafts_db.list_follow_ups_needing_reconciliation(account["id"])
    except Exception:
        row_errors.append("Follow-up send reconciliation is temporarily unavailable.")
        return

    for source in pending:
        review = None
        try:
            review = reviews_db.find_follow_up(account["id"], source["id"])
            if not review or review.get("status") != "pending":
                continue

            if source.get("follow_up_status") == "sent":
                reviews_db.mark_sent(
                    account["id"], review["id"], review.get("draft_reply") or ""
                )
                continue

            thread = gmail.get_thread(account, source["thread_id"])
            sent = gmail.find_sent_reply(
                account,
                source["thread_id"],
                source.get("email") or review.get("email"),
                review.get("draft_reply") or "",
                thread=thread,
            )
            if not sent:
                # Keep the duplicate-send fence in place. Gmail can take a
                # short time to expose an accepted message through a thread
                # read, so the next five-minute cycle owns verification.
                continue
            if drafts_db.mark_follow_up_sent(account["id"], source["id"]):
                reviews_db.mark_sent(
                    account["id"], review["id"], review.get("draft_reply") or ""
                )
                print(
                    f"Reconciled follow-up {source['id']} from Gmail Sent for "
                    f"{source.get('email') or review.get('email')}."
                )
        except Exception:
            label = (source.get("email") or (review or {}).get("email") or "follow-up")
            row_errors.append(f"{label}: follow-up send verification failed. Nothing was resent.")


def _reconcile_first_touch_sends(account, row_errors):
    """Resolve first-touch requests that lost Gmail's response without retrying."""
    try:
        stuck = drafts_db.list_sends_needing_reconciliation(account["id"])
    except Exception:
        row_errors.append("First-touch send verification is temporarily unavailable.")
        return

    for draft in stuck:
        label = draft.get("email") or f"draft {draft['id']}"
        operation_key = f"draft:{draft['id']}"
        try:
            message = gmail.find_sent_email(
                account,
                draft.get("email"),
                draft.get("subject"),
                draft.get("body"),
                gmail.deterministic_message_id(account["id"], operation_key),
            )
            if message:
                if drafts_db.mark_sent(
                    account["id"], draft["id"], draft.get("subject") or "",
                    draft.get("body") or "", thread_id=message.get("threadId") or "",
                    follow_up_delay_days=account.get("follow_up_delay_days") or 3,
                ):
                    try:
                        send_capacity_db.complete(account["id"], operation_key)
                    except Exception:
                        pass  # conservative reservation expiry; never a resend
                    print(f"Reconciled first-touch draft {draft['id']} from Gmail Sent for {label}.")
                continue
            if drafts_db.mark_send_uncertain(account["id"], draft["id"]):
                print(f"First-touch draft {draft['id']} for {label} had no verifiable send result; surfaced as send_uncertain.")
        except Exception:
            row_errors.append(f"{label}: first-touch send verification failed.")


def _reconcile_review_sends(account, row_errors):
    """Close ambiguous normal-reply sends from Gmail's Sent thread.

    A reply claimed for sending (status ``sending``) whose web request died
    before mark_sent/mark_send_uncertain ran was previously invisible and
    unsendable forever -- a delivered reply recorded nowhere. The same
    recovery primitive the follow-up lane uses applies here: an exact
    Sent-message match records the send; no proof flips the row to the
    visible ``send_uncertain`` card, keeping the duplicate fence while ending
    the limbo. No automatic retry is ever performed."""
    try:
        stuck = reviews_db.list_reviews_stuck_in_send(account["id"])
    except Exception:
        row_errors.append("Reply send reconciliation is temporarily unavailable.")
        return

    for review in stuck:
        label = review.get("email") or f"review {review['id']}"
        try:
            thread = gmail.get_thread(account, review["thread_id"])
            sent = gmail.find_sent_reply(
                account,
                review["thread_id"],
                review.get("email"),
                review.get("draft_reply") or "",
                thread=thread,
            )
            if sent:
                reviews_db.mark_sent(
                    account["id"], review["id"], review.get("draft_reply") or ""
                )
                print(f"Reconciled reply {review['id']} from Gmail Sent for {label}.")
                continue
            # No proof either way: the outcome is already fixed in Gmail by
            # now (the claim is minutes old at minimum), so surfacing the
            # truth the operator can verify costs nothing and hides nothing.
            if reviews_db.mark_stuck_send_visible(account["id"], review["id"]):
                print(f"Reply {review['id']} for {label} had no verifiable "
                      "send result; surfaced as send_uncertain.")
        except Exception:
            row_errors.append(f"{label}: reply send verification failed. Nothing was resent.")


def check_for_replies(account):
    """Checks every eligible row and returns {"reviews": [...], "row_errors": [...]}.
    Rows are isolated: one row failing (deleted Gmail thread, transient API
    error) must not block the rows after it, or a single stale ThreadID would
    silently stop reply detection for the rest of the sheet on every run."""
    new_reviews = []
    new_commitments = []
    row_errors = []
    new_bounces = []
    due_commitments = _process_due_commitments(account, row_errors)
    _reconcile_first_touch_sends(account, row_errors)
    _reconcile_follow_up_sends(account, row_errors)
    _reconcile_review_sends(account, row_errors)

    if (account.get("google_sheet_id") or "").strip():
        try:
            sent_rows = sheets.get_reply_check_rows(account)
        except Exception:
            sent_rows = []
            row_errors.append(
                "Google Sheet is temporarily unavailable; using durable follow-up state only."
            )
    else:
        # A tracked Gmail thread is deliberately allowed to outlive its Sheet
        # row, including an account that no longer uses Sheets at all. Do not
        # turn that supported configuration into a permanent degraded health
        # signal merely because there is no Sheet to read.
        sent_rows = []
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

    tracking_available = True
    try:
        tracked_rows = tracked_threads.list_active(account["id"])
    except Exception:
        tracking_available = False
        tracked_rows = []
        row_errors.append("Durable Gmail thread tracking is temporarily unavailable.")

    own_addresses = None
    degraded = False
    if (
        tracking_available
        and tracked_threads.enabled()
        and config.GMAIL_THREAD_DISCOVERY_ENABLED
        and _gmail_discovery_is_due(account["id"])
    ):
        try:
            # Reuse this profile/Send-As lookup for the actual reply scan below
            # so discovery never treats the operator's own aliases as contacts.
            own_addresses, degraded = gmail.get_own_addresses(account)
            discovered = gmail.list_recent_sent_threads(
                account,
                own_addresses=own_addresses,
                days=config.GMAIL_THREAD_DISCOVERY_DAYS,
                max_threads=config.GMAIL_THREAD_DISCOVERY_MAX_THREADS,
            )
            for item in discovered:
                tracked_threads.upsert_thread(
                    account["id"], item["thread_id"],
                    email=item.get("email", ""),
                    name=item.get("name", ""),
                    source="gmail_discovery",
                )
            if discovered:
                tracked_rows = tracked_threads.list_active(account["id"])
                print(f"Discovered {len(discovered)} recent Gmail thread(s) for {account.get('email', account['id'])}.")
            _LAST_GMAIL_DISCOVERY_AT[account["id"]] = time.monotonic()
        except Exception:
            row_errors.append("Gmail sent-thread discovery failed. It will retry next cycle.")

    if not sent_rows and not active_follow_ups and not tracked_rows:
        print("No sent rows, tracked threads, or active follow-ups awaiting replies.")
        return {
            "reviews": [],
            "commitments": due_commitments,
            "row_errors": row_errors,
            "bounces": [],
        }

    # Only sheet-backed work writes a sheet status, so only that path needs the
    # positional-header consent gate. A durable database schedule must keep
    # working when its sheet row was deleted or the earlier best-effort sheet
    # update failed.
    if sent_rows:
        try:
            sheets.require_full_header(account)
        except Exception:
            row_errors.append(
                "Google Sheet header is unsafe to write; using durable follow-up state only."
            )
            sent_rows = []

    # Once per account per cycle, not per row -- it's a Gmail profile call
    # (plus a Send-As listing call) reused by every row below.
    if own_addresses is None:
        own_addresses, degraded = gmail.get_own_addresses(account)
    if not own_addresses:
        # Fail closed. With no verified list of this account's own addresses,
        # _classify_message answers "is this ours?" with no for every message
        # -- including the operator's OWN outgoing mail, which then surfaces
        # as a "customer reply" and gets an AI draft in answer to itself.
        # That failure was observed live as a four-turn self-conversation.
        # Skipping detection for one cycle (the next poll retries the lookup)
        # is strictly better than drafting to ourselves even once.
        row_errors.append(
            f"{account.get('email', account['id'])}: could not determine this "
            "account's own email addresses; skipping reply detection this cycle "
            "so its own outgoing mail can never be mistaken for a customer reply."
        )
        print(f"Own-address lookup empty for {account.get('email', account['id'])}; "
              "reply detection skipped this cycle.")
        return {
            "reviews": [],
            "commitments": due_commitments,
            "row_errors": row_errors,
            "bounces": [],
        }

    # Sheets are a user-visible mirror, not the scheduler. Start with every
    # sheet row so ordinary reply detection is unchanged, then append active
    # database schedules whose thread is absent from the sheet. Synthetic rows
    # carry the same nine positional fields to keep the mature per-row logic
    # below shared; failed sheet writes remain best-effort row errors.
    work_items = []
    sheet_threads = set()
    tracked_ids = {row.get("thread_id") for row in tracked_rows}
    sheet_row_indices = set()
    # A tracked thread may outlive the Sheet row that originally pointed to it.
    # Row numbers are reusable, so an old tracked record must never write into a
    # newer conversation merely because both once occupied row 2.
    sheet_thread_by_row = {}
    for row_index, row in sent_rows:
        thread_id = row[sheets.COL_THREAD_ID].strip()
        if thread_id:
            sheet_threads.add(thread_id)
            sheet_row_indices.add(row_index)
            sheet_thread_by_row[row_index] = thread_id
            if tracking_available and thread_id not in tracked_ids:
                try:
                    tracked_threads.upsert_thread(
                        account["id"], thread_id,
                        email=row[sheets.COL_EMAIL],
                        name=row[sheets.COL_NAME],
                        company=row[sheets.COL_COMPANY],
                        source="sheet_backfill",
                        row_index=row_index,
                    )
                except Exception:
                    row_errors.append(
                        f"{row[sheets.COL_EMAIL].strip() or thread_id}: could not persist "
                        "Gmail thread tracking. It will retry."
                    )
        work_items.append((row_index, row, active_follow_ups.get(thread_id)))
    for thread_id, follow_up in active_follow_ups.items():
        if thread_id in sheet_threads:
            continue
        row = [""] * len(sheets.EXPECTED_HEADER)
        row[sheets.COL_NAME] = follow_up.get("name") or ""
        row[sheets.COL_EMAIL] = follow_up.get("email") or ""
        row[sheets.COL_COMPANY] = follow_up.get("company") or ""
        row[sheets.COL_STATUS] = "Sent"
        row[sheets.COL_THREAD_ID] = thread_id
        row[sheets.COL_EMAIL_BODY] = follow_up.get("body") or ""
        work_items.append((follow_up.get("row_index"), row, follow_up))
    known_work_threads = set(sheet_threads)
    known_work_threads.update(
        item[1][sheets.COL_THREAD_ID].strip()
        for item in work_items if item[1][sheets.COL_THREAD_ID].strip()
    )
    for tracked in tracked_rows:
        thread_id = (tracked.get("thread_id") or "").strip()
        if not thread_id or thread_id in known_work_threads:
            continue
        row = [""] * len(sheets.EXPECTED_HEADER)
        row[sheets.COL_NAME] = tracked.get("name") or ""
        row[sheets.COL_EMAIL] = tracked.get("email") or ""
        row[sheets.COL_COMPANY] = tracked.get("company") or ""
        row[sheets.COL_STATUS] = "Sent"
        row[sheets.COL_THREAD_ID] = thread_id
        tracked_row_index = tracked.get("row_index")
        if sheet_thread_by_row.get(tracked_row_index) != thread_id:
            tracked_row_index = None
        work_items.append((tracked_row_index, row, active_follow_ups.get(thread_id)))
        known_work_threads.add(thread_id)

    def cancel_follow_up(follow_up, reason, *, replied=False):
        """Best-effort cleanup that can never hide an incoming reply.

        The send endpoint repeats these checks immediately before Gmail, so a
        temporary database failure here remains fail-closed without disabling
        the optional reply-help lane.
        """
        if not follow_up:
            return
        try:
            if replied:
                transitioned = drafts_db.mark_follow_up_replied(
                    account["id"], follow_up["id"]
                )
            else:
                transitioned = drafts_db.cancel_follow_up(
                    account["id"], follow_up["id"], reason
                )
            if transitioned is False:
                row_errors.append(
                    f"{follow_up.get('email', 'contact')}: follow-up state changed "
                    "while cancellation was being recorded; its current owner will finish it."
                )
                return
            reviews_db.dismiss_follow_up_for_source(account["id"], follow_up["id"])
        except Exception:
            row_errors.append(
                f"{follow_up.get('email', 'contact')}: could not record follow-up "
                "cancellation; the send-time guard will check it again."
            )

    def record_commitments(thread_id, candidate, reply_body):
        if candidate.get("kind") not in {"on_sheet", "off_sheet", "operator"} or not candidate.get("message_id"):
            return
        try:
            candidates = commitments.extract_commitments(
                reply_body,
                reference_at=candidate.get("timestamp"),
                actor="operator" if candidate.get("kind") == "operator" else "contact",
            )
            if not candidates:
                return
            saved = commitments_db.add_candidates(
                account["id"], thread_id, candidate["message_id"], candidates,
            )
            new_commitments.extend(saved)
            print(f"{len(saved)} commitment candidate(s) found for {email}.")
        except Exception as e:
            # Commitment extraction is additive. A missing migration or an LLM
            # provider issue must never hide the reply review that triggered it.
            row_errors.append(
                f"{name or email}: reply queued, but commitment detection is temporarily unavailable."
            )
            print(f"Commitment detection failed for a reply ({type(e).__name__}).")

    for row_index, row, follow_up in work_items:
        thread_id = row[sheets.COL_THREAD_ID].strip()
        if not thread_id:
            continue

        name = row[sheets.COL_NAME].strip()
        email = row[sheets.COL_EMAIL].strip()
        tracked_row = next(
            (item for item in tracked_rows if item.get("thread_id") == thread_id),
            None,
        )
        try:
            # One read for both checks below. They ask different questions of the
            # same messages, and Gmail charges per fetch.
            thread = gmail.get_thread(account, thread_id)
            # Scan every message once. Choose the oldest unreviewed inbound
            # candidate so multiple replies between worker cycles are drained
            # one-by-one instead of losing everything except the newest.
            reply_candidates = gmail.get_reply_candidates_with_history(
                account, thread_id, email, own_addresses, thread=thread
            )
            if not reply_candidates:
                # Compatibility for isolated tests and older Gmail adapters
                # that expose only the legacy newest-candidate method.
                legacy = gmail.get_latest_reply_with_history(
                    account, thread_id, email, own_addresses, thread=thread
                )
                reply_candidates = [legacy] if legacy else []
            latest_candidate = reply_candidates[-1] if reply_candidates else None
            candidate = next(
                (
                    item for item in reply_candidates
                    if reviews_db.find_review_id(
                        account["id"], thread_id,
                        item.get("message_id"), item.get("text"),
                    ) is None
                ),
                None,
            )
            tracked_threads.mark_checked(
                account["id"], thread_id,
                latest_candidate.get("message_id") if latest_candidate else None,
            )
            # Outbound promises are the product's differentiator. Extract them
            # from Sent messages using the Gmail message timestamp for relative
            # dates, even when there is no inbound reply yet.
            for operator_message in gmail.get_operator_messages(thread, own_addresses):
                record_commitments(
                    thread_id, operator_message, operator_message.get("text") or ""
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
                        # Follow-ups are deliberately unmetered during the
                        # validation cohort. They are not first-touch outreach
                        # drafts, and the pricing decision has not been made;
                        # do not gate them against UNIT_DRAFT_EMAIL without a
                        # corresponding customer-facing contract.
                        draft = agent.draft_follow_up(
                            account, name, row[sheets.COL_COMPANY].strip(),
                            follow_up.get("body") or row[sheets.COL_EMAIL_BODY].strip(),
                            unsubscribe_url=unsubscribe_url,
                        )
                    except Exception:
                        draft = ""
                        row_errors.append(
                            f"{name or email}: follow-up is due, but drafting failed. "
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
                        transitioned = drafts_db.mark_follow_up_queued(
                            account["id"], follow_up["id"]
                        )
                    except Exception:
                        # The update may have committed even if its response was
                        # lost. Inspect the authoritative row before hiding the
                        # review; otherwise a queued source plus dismissed card
                        # is permanently invisible.
                        current = drafts_db.get_draft(account["id"], follow_up["id"])
                        if not current or current.get("follow_up_status") != "queued":
                            reviews_db.dismiss_follow_up_for_source(
                                account["id"], follow_up["id"]
                            )
                            raise
                        transitioned = True
                    if not transitioned:
                        current = drafts_db.get_draft(account["id"], follow_up["id"])
                        if not current or current.get("follow_up_status") != "queued":
                            reviews_db.dismiss_follow_up_for_source(
                                account["id"], follow_up["id"]
                            )
                            continue
                    review = reviews_db.get_review(account["id"], review_id)
                    new_reviews.append(review)
                    print(f"Follow-up due for {name or email}; queued for manual review.")
                elif tracked_row and tracked_row.get("source") == "gmail_discovery":
                    # Discovery refresh reactivates recent Sent threads. Do not
                    # spend every five-minute cycle re-reading permanently
                    # quiet historical threads.
                    tracked_threads.mark_quiescent(account["id"], thread_id)
                continue

            # Echo guard FIRST, before anything acts on this candidate. If the
            # "newest message" is verbatim something this account already sent
            # on this thread, it is our own words coming back -- an alias that
            # dodged own-address classification, or a contact echoing us (or
            # an auto-responder quoting us whole). Acting on it as a reply
            # cancelled pending follow-ups and drafted answers to ourselves;
            # that is how one inbox produced a four-turn AI conversation with
            # itself, each turn signed by a different invented person.
            reply_body = gmail.strip_quoted(candidate["text"])
            sent_bodies = reviews_db.list_sent_bodies_for_thread(account["id"], thread_id)
            normalized_reply = _normalize_for_echo(reply_body)
            if normalized_reply and any(
                normalized_reply == _normalize_for_echo(body) for body in sent_bodies
            ):
                print(f"{name or email}: newest message echoes what we already "
                      "sent on this thread; skipping as an echo, not a reply.")
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
            # Trim the quoted history off before this is stored or shown. A
            # reply on a long thread carries the entire conversation back at us,
            # and an operator scanning a queue at a few seconds per item cannot
            # find the two new sentences inside 20KB of '>' lines. strip_quoted
            # keeps inline replies whole -- it trims the tail, never cuts at the
            # first marker -- so an answer woven between quoted lines survives.
            # (reply_body was already trimmed above for the echo guard.)
            record_commitments(thread_id, candidate, reply_body)

            if candidate["answered_elsewhere"]:
                # The operator already replied from Gmail directly, before
                # this poll ran -- nothing to draft or send. Queued for
                # visibility only: worth knowing, not worth notifying on (no
                # notification path exists yet regardless -- that's Phase 2).
                review_id, created = reviews_db.add_review(
                    account["id"], row_index, name, email, thread_id, reply_body, "",
                    gmail_message_id=candidate["message_id"], status="answered_elsewhere",
                    degraded_classification=degraded,
                    return_created=True,
                )
                new_reviews.append(reviews_db.get_review(account["id"], review_id))
                print(f"{name or email} already answered elsewhere; queued for visibility only.")
                if row_index in sheet_row_indices:
                    try:
                        sheets.update_row(account, row_index, status="Replied", expect_email=email)
                    except Exception:
                        row_errors.append(
                            f"{name or email}: already-answered reply noted, but marking "
                            "the sheet 'Replied' failed."
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
                review_id, created = reviews_db.add_review(
                    account["id"], row_index, name, candidate["from_addr"], thread_id,
                    reply_body, "", gmail_message_id=candidate["message_id"],
                    status="flagged", degraded_classification=degraded,
                    return_created=True,
                )
                review = reviews_db.get_review(account["id"], review_id)
                new_reviews.append(review)
                print(f"Off-sheet reply from {candidate['from_addr']} on {name or email}'s "
                      "thread, flagged for sender confirmation.")
                if row_index in sheet_row_indices:
                    try:
                        sheets.update_row(account, row_index, status="Replied", expect_email=email)
                    except Exception:
                        row_errors.append(
                            f"{name or email}: off-sheet reply flagged for review, but "
                            "marking the sheet 'Replied' failed."
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
                    "response failed. Write the reply yourself in the app."
                )
                print(f"Reply draft failed; queued undrafted ({type(e).__name__}).")

            # Review FIRST, sheet status second. The review is the operator
            # surface and the dedupe key: once it exists, a failing status
            # write costs one visible row_error -- never a re-draft per poll.
            # (Reversed, a persistently failing write would re-burn an LLM
            # call every 100s because add_review never runs.)
            review_id, created = reviews_db.add_review(
                account["id"], row_index, name, email, thread_id, reply_body, draft,
                gmail_message_id=candidate["message_id"], degraded_classification=degraded,
                # draft_reply hands back its last attempt even when validation
                # still fails, so record what was still wrong with it. The
                # operator gets told why a draft is suspect instead of having to
                # notice. Does not affect the lane -- a reply is full-text
                # whatever this says.
                validator_problems=agent.reply_problems(account, draft),
                return_created=True,
            )
            review = reviews_db.get_review(account["id"], review_id)
            new_reviews.append(review)
            print(f"Reply detected from {name}, queued for review in the app.")
            if row_index in sheet_row_indices:
                try:
                    sheets.update_row(account, row_index, status="Replied", expect_email=email)
                except Exception:
                    row_errors.append(
                        f"{name or email}: reply queued for review, but marking the "
                        "sheet 'Replied' failed."
                    )
        except Exception as e:
            label = name or email or f"row {row_index}"
            row_errors.append(
                f"{label}: this conversation could not be checked for replies. It will retry."
            )
            try:
                tracked_threads.mark_error(
                    account["id"], thread_id, f"reply_check_failed:{type(e).__name__}"
                )
            except Exception:
                row_errors.append(f"{label}: the retry status could not be saved.")
            print(f"Reply check failed ({type(e).__name__}).")

    return {
        "reviews": new_reviews,
        "commitments": new_commitments + due_commitments,
        "row_errors": row_errors,
        "bounces": new_bounces,
    }


def run_all_accounts(accounts=None, write_heartbeat=True):
    """Walks every eligible account once, sequentially -- never concurrently.
    A ThreadPoolExecutor across accounts (the pattern send_outreach.py uses
    for outreach sends) would be exactly the fan-out this is meant to avoid.

    accounts defaults to every account with a configured sheet or an active
    durable Gmail thread (accounts_db.list_accounts_for_worker -- deliberately
    not filtered on google_token; see its docstring for why a token-based
    exclusion would freeze the very observability this loop is supposed to
    provide). Pass an explicit list to restrict a run to
    specific accounts, as main()'s single-account debug mode does.

    write_heartbeat is False for that debug mode: a one-off manual run
    against one account must not overwrite worker_heartbeat_at and forge
    the "the continuous worker is alive for this account" signal.

    Returns a summary dict; also prints one line per account plus a
    cycle-level summary, so a glance at the log answers "did the last cycle
    run" without correlating per-account lines by hand."""
    started = time.monotonic()
    run_id = worker_db.start_run() if write_heartbeat else None
    if accounts is None:
        try:
            accounts = accounts_db.list_accounts_for_worker()
        except Exception as e:
            print(f"Could not list accounts for this reply-watch cycle: {e}")
            summary = {"checked": 0, "skipped_no_token": 0, "failed": 0, "elapsed": 0.0}
            if run_id is not None:
                worker_db.record_event(run_id, None, "cycle", "failed", {"error": str(e)})
                worker_db.finish_run(run_id, summary, "failed", str(e))
            return summary

    checked = skipped_no_token = failed = degraded_accounts = 0
    degraded_reasons = {}
    cycle_degraded = False
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
            degraded_accounts += 1
            degraded_reasons["google_disconnected"] = (
                degraded_reasons.get("google_disconnected", 0) + 1
            )
            cycle_degraded = True
            error = "Google disconnected — reconnect in Settings"
            if run_id is not None:
                worker_db.record_event(
                    run_id, account_id, "account_check", "needs_reconnect",
                    {"error": error},
                )
            if write_heartbeat:
                accounts_db.set_worker_heartbeat(account_id, error=error)
            print(f"{label}: no Google connection, skipped this cycle.")
        else:
            try:
                result = usage.run_as(account_id, check_for_replies, account)
                checked += 1
                row_errors = result.get("row_errors") or []
                account_error = row_errors[0] if row_errors else None
                if account_error:
                    cycle_degraded = True
                    degraded_accounts += 1
                    degraded_reasons["row_errors"] = (
                        degraded_reasons.get("row_errors", 0) + 1
                    )
                if run_id is not None:
                    worker_db.record_event(
                        run_id, account_id, "account_check",
                        "degraded" if account_error else "success",
                        {
                            "reviews": len(result.get("reviews") or []),
                            "row_errors": len(row_errors),
                            "bounces": len(result.get("bounces") or []),
                            **({"error": account_error} if account_error else {}),
                        },
                    )
                if write_heartbeat:
                    accounts_db.set_worker_heartbeat(account_id, error=account_error)
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
                degraded_accounts += 1
                degraded_reasons["account_failure"] = (
                    degraded_reasons.get("account_failure", 0) + 1
                )
                cycle_degraded = True
                if run_id is not None:
                    worker_db.record_event(
                        run_id, account_id, "account_check", "failed", {"error": str(e)}
                    )
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
    summary = {
        "checked": checked,
        "skipped_no_token": skipped_no_token,
        "failed": failed,
        "degraded": degraded_accounts,
        "degraded_reasons": degraded_reasons,
        "elapsed": elapsed,
    }
    if run_id is not None:
        status = "success" if not cycle_degraded else "degraded"
        worker_db.finish_run(run_id, summary, status)
    # Alert delivery is independent of Gmail and is intentionally last: a
    # provider outage must never block reply discovery or heartbeat writes.
    if (os.environ.get("TRANSACTIONAL_EMAIL_API_KEY", "").strip()
            and os.environ.get("TRANSACTIONAL_EMAIL_FROM", "").strip()):
        notifications.dispatch_pending(limit=50)
    return summary


def main(argv=None):
    args = argv if isinstance(argv, argparse.Namespace) else build_cli_parser().parse_args(argv)

    if args.account_email:
        try:
            account = accounts_db.get_account_by_email(args.account_email)
        except Exception as exc:
            raise SystemExit(
                f"Could not load account {args.account_email}: {type(exc).__name__}: {exc}. "
                "Check SUPABASE_URL and SUPABASE_SECRET_KEY."
            ) from exc
        if not account:
            sys.exit(f"No account found for {args.account_email}")
        run = lambda: run_all_accounts(accounts=[account], write_heartbeat=False)
        label = f"{args.account_email} only (debug mode -- no heartbeat written)"
    else:
        run = run_all_accounts
        label = "every connected account"

    if args.once:
        run()
        return

    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run)
    print(f"Watching {label} every {CHECK_INTERVAL_MINUTES} minutes. Ctrl+C to stop.")
    run()
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main(_CLI_ARGS)
