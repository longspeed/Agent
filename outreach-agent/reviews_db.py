"""Review queue in Supabase, scoped per account. Every query filters on
account_id — combined with RLS on the table, one tenant can never see
another's reviews."""
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "reviews"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def add_review(account_id, row_index, name, email, thread_id, customer_reply, draft_reply):
    result = _get_client().table(TABLE).insert({
        "account_id": account_id,
        "row_index": row_index,
        "name": name,
        "email": email,
        "thread_id": thread_id,
        "customer_reply": customer_reply,
        "draft_reply": draft_reply,
        "status": "pending",
    }).execute()
    return result.data[0]["id"]


def list_pending_reviews(account_id):
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )
    return result.data


def get_review(account_id, review_id):
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .eq("id", review_id)
        .execute()
    )
    return result.data[0] if result.data else None


def mark_sent(account_id, review_id, sent_body):
    _get_client().table(TABLE).update(
        {"status": "sent", "draft_reply": sent_body}
    ).eq("account_id", account_id).eq("id", review_id).execute()
