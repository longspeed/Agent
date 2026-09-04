"""Account-scoped daily operator logs for the founder distribution loop.

These records are deliberately smaller than a CRM. They contain only the
operator's aggregate activity counts and a short internal label; prospect
addresses, bodies, and Gmail thread IDs never enter this table. Reuse the
durable worker event history so the feature needs no new migration.
"""

from datetime import datetime, timezone

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "worker_events"
EVENT_TYPE = "operator_daily_log"
MAX_LOGS = 31

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def _now():
    return datetime.now(timezone.utc).isoformat()


def _public(row):
    details = row.get("details") or {}
    return {
        "id": row.get("id"),
        "date": details.get("date") or "",
        "inboxLabel": details.get("inbox_label") or "",
        "reviewed": int(details.get("reviewed") or 0),
        "sent": int(details.get("sent") or 0),
        "replies": int(details.get("replies") or 0),
        "followUpReplies": int(details.get("follow_up_replies") or 0),
        "meetings": int(details.get("meetings") or 0),
        "deals": int(details.get("deals") or 0),
        "humanEdits": int(details.get("human_edits") or 0),
        "minutesSaved": int(details.get("minutes_saved") or 0),
        "nextInbox": details.get("next_inbox") or "",
        "safetyEvent": details.get("safety_event") or "none",
        "createdAt": row.get("created_at") or "",
    }


def list_logs(account_id, limit=MAX_LOGS):
    result = (
        _get_client().table(TABLE)
        .select("id,details,created_at")
        .eq("account_id", account_id)
        .eq("event_type", EVENT_TYPE)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [_public(row) for row in (result.data or [])]


def upsert(account_id, *, date, inbox_label, reviewed, sent, replies,
           follow_up_replies, meetings, deals, human_edits, minutes_saved,
           next_inbox, safety_event):
    details = {
        "date": date,
        "inbox_label": inbox_label[:80],
        "reviewed": reviewed,
        "sent": sent,
        "replies": replies,
        "follow_up_replies": follow_up_replies,
        "meetings": meetings,
        "deals": deals,
        "human_edits": human_edits,
        "minutes_saved": minutes_saved,
        "next_inbox": next_inbox[:120],
        "safety_event": safety_event[:160],
        "recorded_at": _now(),
    }
    payload = {
            "run_id": None,
            "account_id": account_id,
            "event_type": EVENT_TYPE,
            "status": "logged",
            "details": details,
            "dedupe_key": date,
        }
    # The unique dedupe key is the source of truth; first try to update an
    # existing row without scanning only the newest 31 logs.
    existing = (
        _get_client().table(TABLE).select("id")
        .eq("account_id", account_id).eq("event_type", EVENT_TYPE)
        .eq("dedupe_key", date).limit(1).execute()
    )
    if existing.data:
        result = (
            _get_client().table(TABLE)
            .update({"status": "logged", "details": details})
            .eq("id", existing.data[0]["id"])
            .eq("account_id", account_id).execute()
        )
    else:
        try:
            result = _get_client().table(TABLE).insert(payload).execute()
        except Exception as e:
            if "23505" not in str(e) and "duplicate key" not in str(e).lower():
                raise
            existing = (
                _get_client().table(TABLE).select("id")
                .eq("account_id", account_id).eq("event_type", EVENT_TYPE)
                .eq("dedupe_key", date).limit(1).execute()
            )
            if not existing.data:
                raise
            result = (
                _get_client().table(TABLE)
                .update({"status": "logged", "details": details})
                .eq("id", existing.data[0]["id"])
                .eq("account_id", account_id).execute()
            )
    row = result.data[0] if result.data else {"details": details}
    return _public(row)
