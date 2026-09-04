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
import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

CLI_VERSION = "dev"


def build_cli_parser():
    parser = argparse.ArgumentParser(
        prog="send_outreach.py",
        description=(
            "Prepare personalized first-touch drafts for one Sendkeep account. "
            "Preparation queues drafts; it does not send email."
        ),
        epilog=(
            "Example:\n"
            "  python send_outreach.py you@company.com"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "account_email",
        help="account to prepare drafts for",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {CLI_VERSION}",
    )
    return parser


# Parse before importing the runtime modules below. --help, --version, and
# invalid-option errors should work on a fresh checkout before any secrets or
# external services have been configured.
_CLI_ARGS = None
if __name__ == "__main__":
    _CLI_ARGS = build_cli_parser().parse_args()


import accounts_db
import agent
import auth
import bounces
import config
import dns_check
import drafts_db
import gmail
import plans
import sheets
import suppressions_db
import send_capacity_db
import tracked_threads
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


class SendFailure(RuntimeError):
    """Stable first-touch failure returned to both API and batch callers."""

    def __init__(self, detail, code="send_blocked", retryable=False):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.retryable = bool(retryable)


class DeliveryUncertain(SendFailure):
    def __init__(self, detail):
        super().__init__(detail, code="send_uncertain", retryable=False)


class CapacityUnavailable(SendFailure):
    def __init__(self, detail):
        super().__init__(detail, code="capacity_unavailable", retryable=True)


def domain_safety(account):
    """Return the concrete sender-domain safety state for this account.

    This check is intentionally fail-closed for optional first-touch sends:
    DNS trouble is not evidence that a domain is healthy. Gmail-only reply
    monitoring remains usable when the check is unavailable because monitoring
    does not send mail.
    """
    if not account.get("google_token"):
        return {
            "status": "not_connected",
            "address": "",
            "domain": "",
            "managed": False,
            "findings": [],
            "summary": "Connect Gmail first — there is no sending domain to check yet.",
            "send_blockers": [],
        }
    try:
        # The send gate checks the domain mail leaves from; that read must
        # demand the SEND capability, not monitor -- a gmail.send-only grant
        # has no readonly scope, and demanding monitor here 409-blocked every
        # first-touch send from such accounts.
        address = gmail.get_sending_address(account, "send")
        _, separator, domain = address.partition("@")
        if not separator or not domain.strip():
            raise RuntimeError("Gmail did not return a sending address")
        report = dns_check.check_domain(domain)
    except Exception:
        # Do not return provider/DNS exception text to the browser. It can
        # contain account-specific details, and the useful customer action is
        # simply to retry or reconnect before sending.
        return {
            "status": "unknown",
            "address": "",
            "domain": "",
            "managed": False,
            "findings": [],
            "summary": "Could not verify the sending domain. Optional first-touch sending is paused until the check succeeds.",
            "send_blockers": ["Could not verify the sending domain before sending."],
        }

    blockers = dns_check.safety_blockers(report)
    unknown = any(finding.get("status") == "unknown" for finding in report.get("findings") or [])
    if unknown:
        blockers = ["Could not complete the SPF, DKIM, and DMARC checks before sending."]
    return {
        "status": "blocked" if blockers else "ready",
        "address": address,
        "send_blockers": blockers,
        **report,
    }


def assert_domain_safe(account):
    """Refuse an optional first-touch send when domain authentication is unsafe."""
    state = domain_safety(account)
    if state["send_blockers"]:
        raise RuntimeError(
            "Sending is paused by the domain-safety gate. "
            + " ".join(state["send_blockers"])
        )


def _block_first_touch(account_id, draft_id, detail):
    """Retire permanently stale work, then expose a stable non-retryable error."""
    try:
        drafts_db.discard(account_id, draft_id)
    except Exception as exc:
        # Failure to retire cannot make the send eligible. The next attempt
        # repeats the same fail-closed preflight.
        print(f"Could not discard blocked first-touch draft {draft_id}: {exc}")
    raise SendFailure(detail, code="send_blocked", retryable=False)


def first_touch_preflight(account, draft):
    """Fresh, shared guard for every first-touch send path.

    This deliberately performs external reads immediately before the durable
    claim. Preview and draft-time checks are useful UX, but they are not safety
    controls because the Sheet, suppression list, bounce state, and DNS can all
    change while a draft waits for approval.
    """
    account_id = account["id"]
    draft_id = draft["id"]
    email = (draft.get("email") or "").strip().lower()

    try:
        drafts_db.require_send_log()
    except Exception as exc:
        raise SendFailure(
            str(exc) or "Sending is temporarily paused because the durable send log could not be verified.",
            code="send_temporarily_blocked",
            retryable=True,
        ) from exc

    try:
        bounces.assert_sendable(account)
        assert_domain_safe(account)
    except Exception as exc:
        raise SendFailure(
            str(exc) or "Sending is temporarily paused by a sender-safety check.",
            code="send_temporarily_blocked",
            retryable=True,
        ) from exc

    try:
        rows = sheets.get_all_rows(account)
    except Exception as exc:
        raise SendFailure(
            "The connected Sheet could not be read, so nothing was sent. Try again when it is available.",
            code="send_temporarily_blocked",
            retryable=True,
        ) from exc

    exact = next((row for index, row in rows if index == draft.get("row_index")), None)
    if exact is None:
        _block_first_touch(
            account_id, draft_id,
            "This draft's exact Sheet row no longer exists. The stale draft was discarded.",
        )
    row_email = sheets.cell(exact, sheets.COL_EMAIL).strip().lower()
    if not email or row_email != email:
        _block_first_touch(
            account_id, draft_id,
            "The recipient on this Sheet row changed after drafting. The stale draft was discarded.",
        )
    if sheets.cell(exact, sheets.COL_STATUS).strip():
        _block_first_touch(
            account_id, draft_id,
            "This Sheet row is already handled. The stale draft was discarded.",
        )
    if sheets.cell(exact, sheets.COL_EMAIL_CONFIDENCE).strip().lower() == "unverified":
        _block_first_touch(
            account_id, draft_id,
            "This address is marked unverified. Confirm it in the Sheet and prepare a new draft.",
        )

    try:
        if suppressions_db.is_suppressed(account_id, email):
            _block_first_touch(
                account_id, draft_id,
                "This recipient opted out. The draft was discarded and nothing was sent.",
            )
        if drafts_db.has_first_touch_conflict(account_id, email, exclude_draft_id=draft_id):
            _block_first_touch(
                account_id, draft_id,
                "A first-touch email for this recipient already exists. Continue from the Gmail thread instead.",
            )
    except SendFailure:
        raise
    except Exception as exc:
        raise SendFailure(
            "Recipient safety state could not be verified, so nothing was sent. Try again.",
            code="send_temporarily_blocked",
            retryable=True,
        ) from exc

    completed = {status.lower() for status in sheets.SENT_STATUSES}
    for row_index, row in rows:
        if row_index == draft.get("row_index"):
            continue
        if (
            sheets.cell(row, sheets.COL_EMAIL).strip().lower() == email
            and sheets.cell(row, sheets.COL_STATUS).strip().lower() in completed
        ):
            _block_first_touch(
                account_id, draft_id,
                "Another Sheet row shows this recipient was already contacted. Continue from Gmail instead.",
            )
    return rows


def _with_retries(action, describe):
    """Runs action with a bounded backoff. Returns its result, or raises the last
    error with `describe` prefixed."""
    last_err = None
    for attempt in range(_MARK_RETRIES):
        try:
            return action()
        except Exception as e:
            last_err = e
            if attempt < _MARK_RETRIES - 1:
                time.sleep(_MARK_RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f"{describe} after {_MARK_RETRIES} attempts ({last_err})") from last_err


def mark_row_sent(account, row_index, email, thread_id, body):
    """Marks a sheet row "Sent" right after the irreversible Gmail send, with a
    bounded retry. The mark must never be deferred to batch end (learning:
    batch-end-write-defeats-send-lock).

    This is the LEAST reliable of the two records a send produces, which is why
    it is written last and why send_prepared_draft treats a failure here as a
    repair task rather than a failed send. sheets.update_row re-verifies the row
    by address first, and that check raises permanently -- not transiently --
    when the operator has deleted or edited the row, so every retry fails
    identically. Sitting where it used to, ahead of the draft-queue update, that
    turned an ordinary sheet edit into an unbounded re-send loop."""
    sent_at = datetime.now(timezone.utc).isoformat()
    _with_retries(
        lambda: sheets.update_row(
            account, row_index, status="Sent", thread_id=thread_id,
            sent_at=sent_at, email_body=body, expect_email=email,
        ),
        f"Email was sent to {email}, but marking the sheet row failed",
    )
    return sent_at


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
    # Counted here, after the draft exists, because this is the unit the plan
    # sells -- a draft in the review queue. Not counted on generation: a
    # generation that failed validation and was retried cost us two LLM calls
    # (already metered as cost) but produced one draft, and billing the customer
    # for our own retry is indefensible. add_draft returning None means the row
    # was already queued, which is not a new unit either.
    if draft_id is not None:
        usage.record_unit(usage.UNIT_DRAFT_EMAIL, 1, email)
    return {"row_index": row_index, "email": email, "draft_id": draft_id, "subject": subject}


def prepare_draft_for_contact(account, row_index, expected_email):
    """Prepare or reopen one first-touch draft from the Threads table.

    The browser supplies both the last-seen Sheet row and email. The email is
    the stable identity: if the owner sorted the Sheet after rendering Threads,
    follow the contact to its current row instead of drafting for whoever moved
    into the stale row number.
    """
    wanted = (expected_email or "").strip().lower()
    if not wanted:
        raise RuntimeError("This contact has no email address to draft for.")

    rows = sheets.get_all_rows(account)
    selected = next(
        ((idx, row) for idx, row in rows if row[sheets.COL_EMAIL].strip().lower() == wanted),
        None,
    )
    if selected is None:
        raise RuntimeError("This contact is no longer present in the connected Sheet. Refresh Threads.")
    current_row_index, row = selected
    if row[sheets.COL_STATUS].strip():
        raise RuntimeError("This contact is no longer waiting for a first email. Refresh Threads.")

    existing = drafts_db.get_pending_for_row(account["id"], current_row_index)
    if existing:
        return {
            "draft_id": existing["id"],
            "row_index": current_row_index,
            "name": existing.get("name") or row[sheets.COL_NAME].strip(),
            "email": existing.get("email") or row[sheets.COL_EMAIL].strip(),
            "company": existing.get("company") or row[sheets.COL_COMPANY].strip(),
            "subject": existing.get("subject") or "",
            "body": existing.get("body") or "",
            "existing": True,
        }

    sheets.require_full_header(account)
    blockers = agent.account_send_blockers(account)
    if blockers:
        raise RuntimeError(" ".join(blockers))
    bounces.assert_sendable(account)
    assert_domain_safe(account)
    if suppressions_db.is_suppressed(account["id"], wanted):
        raise RuntimeError("This contact has opted out and cannot be emailed.")
    if sheets.cell(row, sheets.COL_EMAIL_CONFIDENCE).strip().lower() == "unverified":
        raise RuntimeError(
            "This address is marked unverified. Confirm it in the Sheet before drafting."
        )

    readiness = sheets.campaign_readiness(
        account,
        sent_today=drafts_db.count_sent_last_24_hours(account["id"]),
        suppressed_emails=suppressions_db.list_suppressed_emails(account["id"]),
        rows=rows,
    )
    eligible_rows = {idx for idx, _ in readiness["eligible"]}
    if current_row_index not in eligible_rows:
        if readiness["remaining_today"] <= 0:
            raise RuntimeError("Daily send limit reached. Try again after it resets.")
        raise RuntimeError("This contact is not eligible for a first-touch draft. Refresh Threads.")

    plans.check(account, usage.UNIT_DRAFT_EMAIL)
    prepared = _prepare_one(
        account,
        current_row_index,
        row[sheets.COL_NAME].strip(),
        row[sheets.COL_EMAIL].strip(),
        row[sheets.COL_COMPANY].strip(),
        sheets.cell(row, sheets.COL_LEAD_REASON).strip(),
    )
    draft = drafts_db.get_draft(account["id"], prepared.get("draft_id"))
    if not draft:
        # A concurrent request can win the unique pending-row insert after the
        # model call. Reopen that durable draft rather than returning an empty
        # composer or generating a second send candidate.
        draft = drafts_db.get_pending_for_row(account["id"], current_row_index)
    if not draft:
        raise RuntimeError("The draft could not be saved. Try again.")
    return {
        "draft_id": draft["id"],
        "row_index": current_row_index,
        "name": draft.get("name") or row[sheets.COL_NAME].strip(),
        "email": draft.get("email") or row[sheets.COL_EMAIL].strip(),
        "company": draft.get("company") or row[sheets.COL_COMPANY].strip(),
        "subject": draft.get("subject") or "",
        "body": draft.get("body") or "",
        "existing": False,
    }


def prepare_drafts(account, auto_send=False):
    """Generate a batch of drafts for every eligible contact and queue them for
    review. Eligible = approved (blank Status), has an email, not suppressed,
    within today's remaining send allowance."""
    if auto_send and not config.AUTO_SEND_ENABLED:
        raise RuntimeError(
            "Auto-send is disabled in safety-first mode. Use manual review, "
            "or enable AUTO_SEND_ENABLED only for a controlled deployment."
        )
    # Fail fast if the sheet can't be marked Sent later, before spending LLM
    # calls generating drafts that could never be sent (same consent gate the
    # actual send relies on).
    sheets.require_full_header(account)
    # Independent of the API's blocker check: this module is also driven from
    # the CLI, and an account bouncing this hard should not be able to spend
    # LLM calls drafting mail it must not send.
    bounces.assert_sendable(account)
    # The address authentication check belongs on the operation as well as the
    # preview endpoint so a CLI or future caller cannot bypass it.
    assert_domain_safe(account)
    suppressed = suppressions_db.list_suppressed_emails(account["id"])
    readiness = sheets.campaign_readiness(
        account,
        sent_today=drafts_db.count_sent_last_24_hours(account["id"]),
        suppressed_emails=suppressed,
    )
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
            "draft_ids": [],
        }

    # Monthly plan quota, checked after the daily cap and on the same principle:
    # spend nothing before we know the work is allowed. An account with no
    # allowance left is refused outright (raises), rather than silently
    # preparing zero drafts and reporting success -- "prepared 0 of 12" with no
    # reason is the failure mode that generates a support ticket instead of an
    # upgrade. An account with *some* allowance takes what is left, so a batch
    # that straddles the boundary delivers up to the line rather than failing
    # whole.
    _, allowance, _ = plans.headroom(account, usage.UNIT_DRAFT_EMAIL)
    quota_trimmed = 0
    if allowance is not None and allowance <= 0:
        # Re-checked rather than raising from here so the refusal message is
        # built in one place. Costs a second query only on the path that is
        # about to do no work at all.
        plans.check(account, usage.UNIT_DRAFT_EMAIL)
    if allowance is not None and allowance < len(pending):
        quota_trimmed = len(pending) - allowance
        pending = pending[:allowance]

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

    if auto_send:
        summary = f"Prepared {len(prepared)} of {len(pending)} outreach drafts. Newly prepared drafts are sending automatically."
    else:
        summary = f"Prepared {len(prepared)} of {len(pending)} outreach drafts. Review and send them on the Outreach page."
    if quota_trimmed:
        summary += (
            f"\n\n{quota_trimmed} more were left undrafted: that is all your plan "
            "includes this month."
        )
    if failed:
        summary += "\n\nCouldn't draft:\n" + "\n".join(f"- {email}: {err}" for email, err in failed)
    subject = "Outreach drafts are sending automatically" if auto_send else "Outreach drafts ready for review"
    notify(account, subject, summary)
    print(summary)

    return {
        "prepared": len(prepared),
        "total": len(pending),
        "failed": [{"email": email, "error": err} for email, err in failed],
        "daily_limit": readiness["daily_limit"],
        "remaining_today": readiness["remaining_today"],
        "quota_trimmed": quota_trimmed,
        "draft_ids": [draft["draft_id"] for draft in prepared],
    }


def send_prepared_draft(account, draft, subject=None, body=None, rewritten=False):
    """Sends one approved draft, then records it. `subject`/`body` override the
    stored copy when the operator edited it in the UI. The one-click opt-out link
    and List-Unsubscribe headers are (re)applied here, so a legally-required
    unsubscribe is present even if the operator edited it out.

    rewritten says whether an AI rewrite touched this draft before it was
    sent (see agent.rewrite_outreach_email / drafts_db.mark_rewritten) -- for
    Phase 6's future edit-diff corpus to exclude or label these pairs rather
    than learn from the model's own rewrite as if it were the operator's
    voice. Called from exactly two places: the single-draft send endpoint
    (which passes whatever the request said) and send_all_prepared's batch
    loop (which passes nothing, so this stays False). That's correct by
    construction, not luck -- a server-side batch send has no browser-side
    rewritten state to pass -- but it reads as obvious right up until someone
    adds a parameter near it later, hence writing it down here.

    Returns {"thread_id", "body", "sheet_error", "tracking_error"}. A tracking
    or sheet error means the email went out and was durably logged, but one of
    the mirrors needs repair. It is never reported as a failed send, because
    retrying it would risk a duplicate.

    The write order is load-bearing. The Gmail send is irreversible and has to go
    first -- nothing else can produce the thread id, and writing "Sent" before
    sending would mark mail that never left. So the moment it succeeds, the very
    next thing is the durable record that stops it ever being sent again: the
    draft-queue update, one row in our own database with no re-verification to
    fail. The sheet, which is a spreadsheet the operator edits underneath us,
    comes last and is allowed to fail.

    Getting this backwards is what a deleted sheet row used to cost: the email
    left, mark_row_sent raised permanently on the missing row, drafts_db.mark_sent
    never ran, the draft stayed pending, and the next batch sent the same email to
    the same prospect -- every batch, without limit."""
    email = draft["email"]
    subject = draft["subject"] if subject is None else subject
    body = draft["body"] if body is None else body

    # This is the sole first-touch safety gate. It runs for the single endpoint
    # and the batch path, against fresh state, immediately before ownership is
    # claimed. Gmail is unreachable when any invariant fails.
    first_touch_preflight(account, draft)

    unsub_url = auth.unsubscribe_url(account["id"], email)
    if unsub_url not in body:
        body = f"{body.rstrip()}\n\n{agent._opt_out_line(unsub_url)}"

    # Freeze the exact approved copy before Gmail. Only the CAS winner may
    # reserve capacity or dispatch, so two tabs cannot both cross this line.
    if not drafts_db.claim_send(account["id"], draft["id"], subject, body):
        raise SendFailure(
            "This draft is already being sent or was handled elsewhere.",
            code="send_blocked",
            retryable=False,
        )

    operation_key = f"draft:{draft['id']}"
    try:
        reserved = send_capacity_db.reserve(
            account["id"], operation_key, plans.daily_send_limit_for(account)
        )
    except Exception as exc:
        drafts_db.release_send_claim(account["id"], draft["id"])
        raise CapacityUnavailable(
            "Send capacity could not be reserved, so Gmail was not called. Try again."
        ) from exc
    if not reserved:
        drafts_db.release_send_claim(account["id"], draft["id"])
        raise send_capacity_db.SendCapacityExceeded(
            "This inbox has reached its rolling 24-hour safety cap, or this send is already in progress."
        )

    message_id = gmail.deterministic_message_id(account["id"], operation_key)
    try:
        thread_id = gmail.send_email(
            account, email, subject, body, unsubscribe_url=unsub_url,
            message_id=message_id,
        )
    except Exception as exc:
        # Gmail may have accepted the message before the response was lost.
        # Keep both fences: `sending` is non-sendable and the worker owns the
        # exact Gmail reconciliation before surfacing a final uncertain state.
        raise DeliveryUncertain(
            "Gmail did not return a final result. Sendkeep locked this draft and the worker will verify Gmail Sent; do not resend it."
        ) from exc

    # Irreversible act done. Record it before anything else is attempted, and
    # retry it, because this is now the record that prevents a duplicate.
    try:
        _with_retries(
            lambda: drafts_db.mark_sent(
                account["id"], draft["id"], subject, body, thread_id=thread_id,
                follow_up_delay_days=account.get("follow_up_delay_days") or 3,
            ) or (_ for _ in ()).throw(RuntimeError("the draft send claim was lost")),
            f"Email was sent to {email}, but recording it in the draft queue failed",
        )
    except Exception as exc:
        raise DeliveryUncertain(
            "Gmail accepted the email, but Sendkeep could not record delivery. The draft remains locked; verify Gmail Sent before taking another action."
        ) from exc
    try:
        _with_retries(
            lambda: send_capacity_db.complete(account["id"], operation_key)
            or (_ for _ in ()).throw(RuntimeError("reservation was not active")),
            f"Email was sent to {email}, but closing its capacity reservation failed",
        )
    except Exception as e:
        # Conservative only: the durable sent row already prevents a resend;
        # an open reservation can temporarily reduce capacity but cannot exceed it.
        print(e)
    # Separate, best-effort -- never part of the critical write above. See
    # drafts_db.mark_rewritten for why this must never be able to make that
    # write (or this send) fail.
    if rewritten:
        drafts_db.mark_rewritten(account["id"], draft["id"])

    tracking_error = None
    try:
        tracked_threads.upsert_thread(
            account["id"], thread_id,
            email=email,
            name=draft.get("name"),
            company=draft.get("company"),
            source="sendkeep_send",
            row_index=draft.get("row_index"),
        )
    except Exception as e:
        # The send log is already authoritative, so this cannot make the send
        # retryable. Surface it as a repair task instead of silently returning
        # to Sheet-only reply coverage.
        tracking_error = (
            f"{email} was emailed successfully, but its Gmail thread was not "
            f"added to Sendkeep tracking ({e})."
        )
        print(tracking_error)

    sheet_error = None
    try:
        mark_row_sent(account, draft["row_index"], email, thread_id, body)
    except Exception as e:
        # The email is out and recorded, so this cannot cause a re-send. It only
        # means the customer's sheet does not show what happened.
        sheet_error = (
            f"{email} was emailed successfully, but its sheet row was not updated "
            f"({e}). Set that row's Status to 'Sent' so your records match; it will "
            "not be emailed again either way."
        )
        print(sheet_error)
    return {
        "thread_id": thread_id,
        "body": body,
        "sheet_error": sheet_error,
        "tracking_error": tracking_error,
    }


def send_all_prepared(account, draft_ids=None):
    """Send every pending draft, one at a time, human-paced. The suppression
    list and EmailConfidence are re-checked before EACH send (someone can opt
    out or a sourced address can be unverified mid-batch). The daily cap is
    reserved atomically immediately before Gmail for every item. Drips with a randomized gap so the batch doesn't leave in
    one burst. Sheet edits/deletions are tolerated per-draft: one bad row must
    not stop the rest."""
    # Derive the safety policy from the durable account setting here, at the
    # only batch-send implementation. Callers cannot opt out accidentally by
    # forgetting a flag or explicitly passing the wrong one.
    unattended = (account.get("outreach_send_mode") or "manual") == "auto"

    drafts = drafts_db.list_pending_drafts(account["id"])
    if draft_ids is not None:
        selected_ids = set(draft_ids)
        drafts = [draft for draft in drafts if draft["id"] in selected_ids]
    sent = []
    skipped = []
    failed = []
    needs_review = []
    sheet_warnings = []
    tracking_warnings = []

    for i, draft in enumerate(drafts):
        email = draft["email"]
        if unattended:
            # Auto mode has no human between this durable row and Gmail. Apply
            # the validator to the exact copy about to leave, not merely the
            # model output that originally created the row. A rejected draft
            # stays pending so the operator can repair and send it manually.
            unsubscribe_url = auth.unsubscribe_url(account["id"], email)
            problems = agent.outreach_problems(
                account, draft.get("subject"), draft.get("body"), unsubscribe_url
            )
            if problems:
                needs_review.append({"email": email, "problems": problems})
                continue
        try:
            result = send_prepared_draft(account, draft)
            # Counted against the cap on the strength of the send, not of the
            # sheet write. The cap is a domain-reputation control, so it has to
            # count emails that actually left; deferring the decrement to a
            # successful sheet write let it under-count and over-send.
            sent.append(email)
            if result["sheet_error"]:
                sheet_warnings.append(result["sheet_error"])
            if result.get("tracking_error"):
                tracking_warnings.append(result["tracking_error"])
            print(f"Sent to {email}")
        except send_capacity_db.SendCapacityExceeded as e:
            skipped.append((email, str(e)))
        except SendFailure as e:
            if e.retryable:
                failed.append((email, e.detail))
            else:
                skipped.append((email, e.detail))
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
    if needs_review:
        summary += "\n\nHeld for manual review:\n" + "\n".join(
            f"- {item['email']}: {'; '.join(item['problems'])}"
            for item in needs_review
        )
    if sheet_warnings:
        # Kept apart from `failed` on purpose: these were delivered. Filing them
        # as failures is what invited an operator to send them again.
        summary += "\n\nSent, but your sheet needs a manual fix:\n" + "\n".join(
            f"- {w}" for w in sheet_warnings
        )
    if tracking_warnings:
        summary += "\n\nSent, but Gmail thread tracking needs a manual fix:\n" + "\n".join(
            f"- {w}" for w in tracking_warnings
        )
    notify(account, "Outreach batch sent", summary)
    print(summary)
    return {
        "sent": len(sent),
        "skipped": [{"email": e, "reason": r} for e, r in skipped],
        "failed": [{"email": e, "error": err} for e, err in failed],
        "needs_review": needs_review,
        "sheet_warnings": sheet_warnings,
        "tracking_warnings": tracking_warnings,
    }


def main(account):
    """CLI entry point: prepare a batch of drafts for review."""
    return prepare_drafts(account)


def run_cli(argv=None):
    args = argv if isinstance(argv, argparse.Namespace) else build_cli_parser().parse_args(argv)
    try:
        account = accounts_db.get_account_by_email(args.account_email)
    except Exception as exc:
        raise SystemExit(
            f"Could not load account {args.account_email}: {type(exc).__name__}: {exc}. "
            "Check SUPABASE_URL and SUPABASE_SECRET_KEY."
        ) from exc
    if not account:
        raise SystemExit(f"No account found for {args.account_email}")
    usage.set_account(account["id"])
    return main(account)


if __name__ == "__main__":
    run_cli(_CLI_ARGS)
