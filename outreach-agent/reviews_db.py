from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SERVICE_KEY

TABLE = "reviews"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


def add_review(row_index, name, email, thread_id, customer_reply, draft_reply):
    result = _get_client().table(TABLE).insert({
        "row_index": row_index,
        "name": name,
        "email": email,
        "thread_id": thread_id,
        "customer_reply": customer_reply,
        "draft_reply": draft_reply,
        "status": "pending",
    }).execute()
    return result.data[0]["id"]


def list_pending_reviews():
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )
    return result.data


def get_review(review_id):
    result = _get_client().table(TABLE).select("*").eq("id", review_id).execute()
    return result.data[0] if result.data else None


def mark_sent(review_id, sent_body):
    _get_client().table(TABLE).update(
        {"status": "sent", "draft_reply": sent_body}
    ).eq("id", review_id).execute()
