"""Outreach drafts awaiting human approval, in Supabase, scoped per account.

The outreach path is deliberately two-phase: "prepare" generates and validates
an email per eligible contact and parks it here as `pending`; a human then edits
and approves each one on the /outreach page, at which point it is actually sent.
Nothing in this table has been emailed. Mirrors reviews_db.py (the reply-review
queue) closely on purpose -- same per-account scoping, same RLS backing.
"""
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

    mark_sent writes sent_at, and mark_sent is the durable record that stops a
    sent email being sent again. On a database missing that column the write fails
    -- after Gmail has already accepted the message -- which is precisely the
    unbounded re-send loop send_prepared_draft was restructured to prevent. So
    this is checked BEFORE the irreversible act, in the same spirit as
    sheets.require_full_header: if we cannot record it, we do not do it.

    Deliberately not a fallback that drops sent_at and carries on. That would
    trade duplicate sends for an uncounted daily cap, and the cap is the
    reputation control -- silently swapping one failure for another is how the
    four bugs in this class got their reach. Cached once the column is confirmed,
    since it cannot disappear mid-process."""
    global _send_log_verified
    if _send_log_verified:
        return
    try:
        _get_client().table(TABLE).select("sent_at").limit(1).execute()
    except Exception as e:
        raise RuntimeError(
            "Sending is blocked: outreach_drafts.sent_at is missing, so a send "
            "could not be recorded and the same email would go out again on the "
            "next batch. Run: alter table public.outreach_drafts add column if "
            f"not exists sent_at timestamptz; ({e})"
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


def mark_sent(account_id, draft_id, sent_subject, sent_body):
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
    from datetime import datetime, timezone

    _get_client().table(TABLE).update({
        "status": "sent",
        "subject": sent_subject,
        "body": sent_body,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("account_id", account_id).eq("id", draft_id).execute()


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
