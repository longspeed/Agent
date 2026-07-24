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
    in the UI before approving), and moves the row out of the pending set."""
    _get_client().table(TABLE).update(
        {"status": "sent", "subject": sent_subject, "body": sent_body}
    ).eq("account_id", account_id).eq("id", draft_id).execute()


def discard(account_id, draft_id):
    _get_client().table(TABLE).update(
        {"status": "discarded"}
    ).eq("account_id", account_id).eq("id", draft_id).execute()
