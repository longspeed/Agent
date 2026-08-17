"""Outreach drafts awaiting human approval, in Supabase, scoped per account.

The outreach path is deliberately two-phase: "prepare" generates and validates
an email per eligible contact and parks it here as `pending`; a human then edits
and approves each one on the /outreach page, at which point it is actually sent.
Nothing in this table has been emailed. Mirrors reviews_db.py (the reply-review
queue) closely on purpose -- same per-account scoping, same RLS backing.
"""
from datetime import datetime, timedelta, timezone

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "outreach_drafts"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


_send_log_verified = False

# Every column written by mark_sent's authoritative, post-Gmail update. The
# preflight must probe the whole write shape: checking only sent_at allowed a
# partially migrated database to accept the Gmail send and then reject the
# durable duplicate-send fence on a newer follow-up column.
_AUTHORITATIVE_SEND_COLUMNS = (
    "status",
    "subject",
    "body",
    "sent_at",
    "thread_id",
    "follow_up_due_at",
    "follow_up_status",
    "follow_up_cancel_reason",
)


# Outreach drafts are generated from the sender's own settings and a row in the
# sheet they own. No third-party text reaches the prompt, so a fast approval
# path is coherent here in a way it is not for replies -- see reviews_db, where
# the same constant is False and explains why.
#
# Eligible is not the same as fast: a draft the validator complained about still
# routes to full text (see lane), because "the model produced something the
# rules rejected" is a reason to read it whatever its provenance.
FAST_LANE_ELIGIBLE = True


def lane(draft):
    """Which approval surface an outreach draft belongs on. Always the fast one.

    Two independent reasons, and it is worth being explicit that the second one
    is not doing any work:

    1. Provenance. No third-party text reaches the prompt (see FAST_LANE_ELIGIBLE
       above), which is what makes a few-seconds-per-item glance coherent here.
    2. Validation. generate_outreach_email RAISES after two failed attempts
       rather than returning the bad text, so a draft that reached this queue
       passed the validator by construction. There is no such thing as a queued
       outreach draft carrying unresolved problems.

    An earlier version of this function checked a validator_problems column on
    the draft. That column can never be populated for exactly the reason in (2),
    so the check would have read like a safety gate and been a permanent no-op --
    the same shape as the fence that could not block. Replies are the path where
    unresolved problems are real, and they are handled by provenance in
    reviews_db, not by inspecting output here."""
    return "fast"


def require_send_log():
    """Refuses to let a send start if the send log cannot record it.

    mark_sent is the durable record that stops a sent email being sent again.
    Probe every column in that update, not just sent_at: a partially migrated
    database can have sent_at while lacking the follow-up columns, and would
    otherwise fail only after Gmail has accepted the message. This is checked
    BEFORE the irreversible act, in the same spirit as sheets.require_full_header:
    if we cannot record the complete authoritative update, we do not send.

    Deliberately not a fallback that drops sent_at and carries on. That would
    trade duplicate sends for an uncounted daily cap, and the cap is the
    reputation control -- silently swapping one failure for another is how the
    four bugs in this class got their reach. Cached once the column is confirmed,
    since it cannot disappear mid-process."""
    global _send_log_verified
    if _send_log_verified:
        return
    try:
        _get_client().table(TABLE).select(
            ",".join(_AUTHORITATIVE_SEND_COLUMNS)
        ).limit(1).execute()
    except Exception as e:
        raise RuntimeError(
            "Sending is blocked: outreach_drafts.sent_at is missing or the "
            "follow-up workflow is incomplete, so the authoritative send record "
            "cannot be guaranteed. Apply outreach-agent/migrations/"
            "20260812_add_original_draft_columns.sql and "
            "20260813_add_follow_up_workflow.sql before sending. Run at minimum: "
            "alter table public.outreach_drafts add column if not exists sent_at "
            f"timestamptz; ({e})"
        ) from e
    _send_log_verified = True


def has_pending_for_row(account_id, row_index):
    existing = (
        _get_client().table(TABLE)
        .select("id")
        .eq("account_id", account_id)
        .eq("row_index", row_index)
        .eq("status", "pending")
        .limit(1)
        .execute()
    )
    return bool(existing.data)


def add_draft(account_id, row_index, name, email, company, subject, body):
    """Queues a draft, unless a pending one already exists for this sheet row.
    The dedupe both guards a double-clicked "Prepare" and complements the
    partial unique index in the schema, which is the true backstop against two
    overlapping prepare jobs inserting the same row twice."""
    if has_pending_for_row(account_id, row_index):
        return None
    result = _get_client().table(TABLE).insert({
        "account_id": account_id,
        "row_index": row_index,
        "name": name,
        "email": email,
        "company": company,
        "subject": subject,
        "body": body,
        # What the model wrote, frozen. subject/body are mutable -- mark_sent
        # replaces them with whatever the operator actually approved -- so
        # without these two columns the pair (what we generated, what a human
        # sent) is destroyed at send time rather than merely uncollected, and
        # it cannot be reconstructed afterwards. Written once here, never
        # updated anywhere.
        "original_subject": subject,
        "original_body": body,
        "status": "pending",
    }).execute()
    return result.data[0]["id"]


def list_pending_drafts(account_id):
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )
    return result.data


def get_draft(account_id, draft_id):
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .eq("id", draft_id)
        .execute()
    )
    return result.data[0] if result.data else None


def _follow_up_due(sent_at, delay_days):
    """The same local-time-of-day after N business days, stored as UTC."""
    due = sent_at
    remaining = max(1, min(int(3 if delay_days is None else delay_days), 14))
    while remaining:
        due += timedelta(days=1)
        if due.weekday() < 5:
            remaining -= 1
    return due


def mark_sent(account_id, draft_id, sent_subject, sent_body, thread_id="", follow_up_delay_days=3):
    """Records the copy that actually went out (the operator may have edited it
    in the UI before approving), and moves the row out of the pending set.

    This is the authoritative send log. send_prepared_draft calls it immediately
    after Gmail accepts the message and before touching the sheet, so one row
    here means exactly one email left -- which is what makes it safe to count the
    daily cap from (see count_sent_last_24_hours).

    Deliberately does NOT touch original_subject/original_body. Overwriting
    subject/body here is correct -- they are the record of what was sent -- but
    it used to be the ONLY record, which silently destroyed every labelled pair
    of (what the model wrote, what this person actually says). The diff between
    the two columns is the whole training signal, and it cannot be backfilled."""
    sent_at = datetime.now(timezone.utc)
    _get_client().table(TABLE).update({
        "status": "sent",
        "subject": sent_subject,
        "body": sent_body,
        "sent_at": sent_at.isoformat(),
        "thread_id": thread_id,
        "follow_up_due_at": _follow_up_due(sent_at, follow_up_delay_days).isoformat(),
        "follow_up_status": "waiting",
        "follow_up_cancel_reason": None,
    }).eq("account_id", account_id).eq("id", draft_id).execute()


ACTIVE_FOLLOW_UP_STATUSES = ("waiting", "queued", "sent")
FOLLOW_UP_PAGE_SIZE = 500


def list_active_follow_ups(account_id):
    """Returns every active schedule, not just PostgREST's first response page.

    Team accounts can exceed the API's configured row cap in a month. Silently
    dropping the tail here strands follow-ups, so advance by the number of rows
    actually returned rather than assuming the server honored our page size.
    """
    rows = []
    start = 0
    while True:
        result = (
            _get_client().table(TABLE).select("*")
            .eq("account_id", account_id)
            .in_("follow_up_status", list(ACTIVE_FOLLOW_UP_STATUSES))
            .order("id")
            .range(start, start + FOLLOW_UP_PAGE_SIZE - 1)
            .execute()
        )
        page = result.data or []
        if not page:
            break
        rows.extend(page)
        start += len(page)
    return rows


def is_follow_up_due(draft, now=None):
    if draft.get("follow_up_status") != "waiting" or not draft.get("follow_up_due_at"):
        return False
    due = datetime.fromisoformat(draft["follow_up_due_at"].replace("Z", "+00:00"))
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due <= (now or datetime.now(timezone.utc))


def follow_up_summary(account_id, now=None):
    active = list_active_follow_ups(account_id)
    due = sum(1 for d in active if d.get("follow_up_status") == "queued" or is_follow_up_due(d, now))
    return {
        "due": due,
        "waiting": sum(1 for d in active if d.get("follow_up_status") == "waiting"),
        "queued": sum(1 for d in active if d.get("follow_up_status") == "queued"),
        "sent": sum(1 for d in active if d.get("follow_up_status") == "sent"),
    }


def follow_up_outcomes(account_id):
    result = _get_client().table(TABLE).select(
        "follow_up_status,follow_up_sent_at,follow_up_replied_at"
    ).eq(
        "account_id", account_id
    ).execute()
    statuses = [row.get("follow_up_status") for row in result.data if row.get("follow_up_status")]
    return {
        "scheduled": len(statuses),
        "replied": statuses.count("replied"),
        "replies_after_follow_up": sum(
            1 for row in result.data
            if row.get("follow_up_sent_at") and row.get("follow_up_replied_at")
        ),
        "cancelled": statuses.count("cancelled"),
    }


def _set_follow_up(account_id, draft_id, status, from_statuses=None, **fields):
    """Transitions one follow-up row, returning True only if a row actually
    matched the from-status guard.

    The bool return is load-bearing, not cosmetic: a zero-row transition means
    the guard fired -- the row left the expected state between read and write
    (a racing watcher queued it, an opt-out cancelled it). Callers that treat
    a no-op as success are how "it changed" becomes "it didn't, but we
    proceeded as if it had" (see the plan-quota gate change)."""
    payload = {"follow_up_status": status, **fields}
    query = (
        _get_client().table(TABLE).update(payload)
        .eq("account_id", account_id).eq("id", draft_id)
    )
    allowed = tuple(from_statuses or ACTIVE_FOLLOW_UP_STATUSES)
    if len(allowed) == 1:
        query = query.eq("follow_up_status", allowed[0])
    else:
        query = query.in_("follow_up_status", list(allowed))
    result = query.execute()
    return bool(result.data)


def mark_follow_up_queued(account_id, draft_id):
    return _set_follow_up(account_id, draft_id, "queued", from_statuses=("waiting",))


def claim_follow_up_send(account_id, draft_id):
    """Atomically owns the one irreversible send; False means lost race."""
    return _set_follow_up(
        account_id, draft_id, "sending", from_statuses=("queued",)
    )


def release_follow_up_send(account_id, draft_id):
    """Makes a failed Gmail attempt reviewable again without creating a step."""
    return _set_follow_up(
        account_id, draft_id, "queued", from_statuses=("sending",)
    )


def mark_follow_up_sent(account_id, draft_id):
    return _set_follow_up(
        account_id, draft_id, "sent", from_statuses=("sending",),
        follow_up_sent_at=datetime.now(timezone.utc).isoformat()
    )


def mark_follow_up_replied(account_id, draft_id):
    return _set_follow_up(
        account_id, draft_id, "replied",
        follow_up_replied_at=datetime.now(timezone.utc).isoformat()
    )


def mark_claimed_follow_up_replied(account_id, draft_id):
    """Owner-only cancellation during the final pre-send Gmail recheck."""
    return _set_follow_up(
        account_id, draft_id, "replied", from_statuses=("sending",),
        follow_up_replied_at=datetime.now(timezone.utc).isoformat(),
    )


def cancel_follow_up(account_id, draft_id, reason):
    return _set_follow_up(account_id, draft_id, "cancelled", follow_up_cancel_reason=reason)


def cancel_claimed_follow_up(account_id, draft_id, reason):
    """Owner-only cancellation while a send claim is running preflight checks."""
    return _set_follow_up(
        account_id, draft_id, "cancelled", from_statuses=("sending",),
        follow_up_cancel_reason=reason,
    )


def cancel_follow_ups_for_email(account_id, email, reason):
    return (
        _get_client().table(TABLE).update({
            "follow_up_status": "cancelled", "follow_up_cancel_reason": reason,
        }).eq("account_id", account_id).eq("email", email)
        .in_("follow_up_status", list(ACTIVE_FOLLOW_UP_STATUSES)).execute()
    )


def mark_rewritten(account_id, draft_id):
    """Records that an AI rewrite touched this draft before it was sent, for
    Phase 6's future edit-diff corpus to exclude or label -- otherwise the
    diff between original_subject/original_body and the sent copy is the
    model's own rewrite, not the human's voice, and would train as if it
    were one.

    Deliberately a SEPARATE, best-effort write from mark_sent, never in the
    same update and never called before it. mark_sent is the durable record
    that stops a sent email going out again -- require_send_log fails the
    whole send closed, before Gmail is even called, specifically because a
    database that can't record that write must not be trusted to prevent a
    duplicate. Adding this analytics column to that same update would mean a
    database missing THIS column (which require_send_log has no way to know
    to check for) makes the critical write fail after Gmail has already
    accepted the message -- reopening the exact hazard require_send_log
    exists to close. Matches usage.record's own convention: never break the
    action being observed."""
    try:
        _get_client().table(TABLE).update({"rewritten": True}) \
            .eq("account_id", account_id).eq("id", draft_id).execute()
    except Exception as e:
        print(f"Could not record the rewritten flag for draft {draft_id}: {e}")


def count_sent_since(account_id, since):
    """How many emails this account has actually sent since `since` (an aware
    datetime).

    The authoritative send count, used as the denominator of both
    reputation-affecting guards: the daily cap and the bounce rate. Every input to
    those has to come from data the operator cannot edit, and the lead sheet is a
    spreadsheet they own. Measured against the sheet, both guards moved in both
    directions -- deleting the Sent rows or reformatting the SentAt column handed
    back a full day's allowance, and pasting rows that carry a status and a date
    inflated the bounce denominator until the rate no longer tripped."""
    result = (
        _get_client().table(TABLE)
        .select("id", count="exact")
        .eq("account_id", account_id)
        .eq("status", "sent")
        .gte("sent_at", since.isoformat())
        .limit(1)
        .execute()
    )
    return result.count or 0


def count_sent_last_24_hours(account_id):
    """The daily send cap's denominator. See count_sent_since."""
    from datetime import datetime, timedelta, timezone

    return count_sent_since(account_id, datetime.now(timezone.utc) - timedelta(days=1))


def discard(account_id, draft_id):
    _get_client().table(TABLE).update(
        {"status": "discarded"}
    ).eq("account_id", account_id).eq("id", draft_id).execute()
