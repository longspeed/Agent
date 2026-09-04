"""Durable source of truth for Gmail threads Sendkeep is responsible for.

Google Sheets remains a useful customer-facing mirror, but a row can be moved
or deleted without changing what Gmail conversation the system must watch. This
module stores that responsibility separately and lets the watcher continue when
the Sheet is unavailable.
"""
import os
from datetime import datetime, timezone

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "tracked_threads"
ACTIVE_STATUS = "active"
PAGE_SIZE = 500

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def enabled() -> bool:
    return os.environ.get("TRACKED_THREADS_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def upsert_thread(
    account_id,
    thread_id,
    *,
    email="",
    name="",
    company="",
    source="sendkeep_send",
    row_index=None,
):
    """Register a thread without making the Sheet a prerequisite.

    Upsert is intentional: a Sheet backfill may know more contact metadata than
    the original send, and the same thread can be encountered from more than
    one source without creating a second watch record.
    """
    if not enabled() or not thread_id:
        return None
    payload = {
        "account_id": account_id,
        "thread_id": thread_id,
        "source": source,
        "status": ACTIVE_STATUS,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # A Gmail-only discovery record may not know contact metadata yet. Do not
    # overwrite richer Sheet/backfill values with empty strings on a later
    # upsert.
    for key, value in (("email", email), ("name", name), ("company", company)):
        value = (value or "").strip()
        if value:
            payload[key] = value
    if row_index is not None:
        payload["row_index"] = row_index
    client = _get_client()
    # Update first with a database-side fence. A manually removed thread must
    # stay removed when the 30-minute Gmail discovery pass sees it again.
    result = (
        client.table(TABLE).update(payload)
        .eq("account_id", account_id).eq("thread_id", thread_id)
        .neq("status", "removed").execute()
    )
    if result.data:
        return result.data[0]

    existing = get(account_id, thread_id)
    if existing:
        return existing

    try:
        result = client.table(TABLE).insert(payload).execute()
        return result.data[0] if result.data else None
    except Exception:
        # Another worker may have inserted the same account/thread after the
        # update missed. Read the winner instead of overwriting its status.
        existing = get(account_id, thread_id)
        if existing:
            return existing
        raise


def list_active(account_id):
    """Return every active thread, advancing through Supabase pages."""
    if not enabled():
        return []
    rows = []
    start = 0
    while True:
        result = (
            _get_client().table(TABLE).select("*")
            .eq("account_id", account_id)
            .eq("status", ACTIVE_STATUS)
            .order("id")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        page = result.data or []
        if not page:
            break
        rows.extend(page)
        start += len(page)
    return rows


def get(account_id, thread_id):
    """Return one tracked thread without scanning the account's whole queue."""
    if not enabled() or not thread_id:
        return None
    result = (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id).eq("thread_id", thread_id)
        .limit(1).execute()
    )
    return result.data[0] if result.data else None


def get_by_id(account_id, tracked_id):
    """Return an account-owned tracked row by its browser-safe numeric id."""
    if not enabled() or not tracked_id:
        return None
    result = (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id).eq("id", tracked_id)
        .limit(1).execute()
    )
    return result.data[0] if result.data else None


def begin_delete(account_id, tracked_id):
    """Quiesce a thread before its external Sheet row is removed."""
    if not enabled() or not tracked_id:
        return False
    result = (
        _get_client().table(TABLE).update({
            "status": "deleting",
            "last_error": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("account_id", account_id).eq("id", tracked_id).execute()
    )
    return bool(result.data)


def send_blocker(account_id, thread_id):
    """Return why deleting this thread would destroy send reconciliation.

    This is the user-friendly preflight before the external Sheet mutation.
    The deletion RPC repeats the same checks transactionally, because this
    read alone cannot close a race with another browser starting a send.
    """
    if not enabled() or not thread_id:
        return None
    client = _get_client()
    reviews = (
        client.table("reviews").select("id,status")
        .eq("account_id", account_id).eq("thread_id", thread_id)
        .in_("status", ["sending", "send_uncertain"])
        .limit(1).execute()
    )
    if reviews.data:
        return (
            "send_uncertain" if reviews.data[0].get("status") == "send_uncertain"
            else "send_in_progress"
        )
    drafts = (
        client.table("outreach_drafts").select("id")
        .eq("account_id", account_id).eq("thread_id", thread_id)
        .eq("follow_up_status", "sending").limit(1).execute()
    )
    return "send_in_progress" if drafts.data else None


def restore_after_delete_failure(account_id, tracked_id, error):
    """Put a failed deletion back in the visible queue so it can be retried."""
    _get_client().table(TABLE).update({
        "status": ACTIVE_STATUS,
        "last_error": (str(error) or "Contact deletion failed")[:500],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("account_id", account_id).eq("id", tracked_id).execute()


def purge_contact(account_id, tracked_id):
    """Atomically purge active work and scrub the durable re-import tombstone."""
    result = _get_client().rpc("delete_tracked_contact", {
        "p_account_id": account_id,
        "p_tracked_id": tracked_id,
    }).execute()
    value = result.data
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else None


def remove(account_id, tracked_id):
    """Stop monitoring one account-owned thread without touching Gmail."""
    if not enabled() or not tracked_id:
        return False
    client = _get_client()
    result = (
        client.table(TABLE).update({
            "status": "removed",
            "last_error": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("account_id", account_id).eq("id", tracked_id)
        .neq("status", "removed").execute()
    )
    if result.data:
        return True
    existing = (
        client.table(TABLE).select("id,status").eq("account_id", account_id)
        .eq("id", tracked_id).limit(1).execute()
    )
    return bool(existing.data and existing.data[0].get("status") == "removed")


def mark_checked(account_id, thread_id, message_id=None):
    if not enabled() or not thread_id:
        return
    fields = {
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }
    if message_id:
        fields["last_contact_message_id"] = message_id
    try:
        _get_client().table(TABLE).update(fields).eq("account_id", account_id) \
            .eq("thread_id", thread_id).execute()
    except Exception as e:
        print(f"Could not record tracked thread check for {thread_id}: {e}")


def mark_error(account_id, thread_id, error):
    if not enabled() or not thread_id:
        return
    try:
        _get_client().table(TABLE).update({
            "last_error": (error or "")[:500] or None,
        }).eq("account_id", account_id).eq("thread_id", thread_id).execute()
    except Exception as e:
        print(f"Could not record tracked thread error for {thread_id}: {e}")


def mark_quiescent(account_id, thread_id):
    """Stop polling an idle discovery thread until the next Sent refresh."""
    if not enabled() or not thread_id:
        return
    try:
        _get_client().table(TABLE).update({
            "status": "quiescent",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("account_id", account_id).eq("thread_id", thread_id) \
            .eq("source", "gmail_discovery").eq("status", ACTIVE_STATUS).execute()
    except Exception as e:
        print(f"Could not retire idle tracked thread {thread_id}: {e}")
