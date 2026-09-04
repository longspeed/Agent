"""Explicit operator-confirmed sales outcomes.

The reliability epic already provides an account-scoped generic event history
in ``worker_events``. Keep outcome records there until a dedicated outcome
schema is justified by real usage. Outcomes are never inferred from Gmail
text, a calendar URL, or a reply; the operator must confirm them in the queue.
"""
from datetime import datetime, timezone

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "worker_events"
OUTCOME_TYPES = ("meeting_booked", "deal_won", "deal_lost")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def _now():
    return datetime.now(timezone.utc).isoformat()


def list_outcomes(account_id):
    rows = []
    start = 0
    while True:
        query = (
            _get_client().table(TABLE)
            .select("id,event_type,status,details,created_at")
            .eq("account_id", account_id)
            .in_("event_type", list(OUTCOME_TYPES))
            .order("created_at", desc=True)
        )
        try:
            query = query.range(start, start + 499)
        except AttributeError:
            page = query.limit(500).execute().data or []
            rows.extend(page)
            break
        page = query.execute().data or []
        if not page:
            break
        rows.extend(page)
        start += len(page)
        if len(page) < 500:
            break
    return rows


def summary(account_id, rows=None):
    rows = list_outcomes(account_id) if rows is None else rows
    return {
        "meeting_booked": sum(row.get("event_type") == "meeting_booked" for row in rows),
        "deal_won": sum(row.get("event_type") == "deal_won" for row in rows),
        "deal_lost": sum(row.get("event_type") == "deal_lost" for row in rows),
    }


def record(account_id, outcome, *, email, name="", company="", source="", value_usd=None):
    if outcome not in OUTCOME_TYPES:
        raise ValueError("Unsupported outcome")
    details = {
        "email": (email or "").strip().lower(),
        "name": (name or "").strip()[:200],
        "company": (company or "").strip()[:200],
        "source": (source or "").strip()[:80],
        "confirmed_at": _now(),
    }
    if value_usd is not None:
        details["value_usd"] = float(value_usd)
    payload = {
        "run_id": None,
        "account_id": account_id,
        "event_type": outcome,
        "status": "confirmed",
        "details": details,
        "dedupe_key": details["email"],
    }
    try:
        result = _get_client().table(TABLE).insert(payload).execute()
    except Exception as e:
        if "23505" not in str(e) and "duplicate key" not in str(e).lower():
            raise
        result = (
            _get_client().table(TABLE).select(
                "id,event_type,status,details,created_at"
            ).eq("account_id", account_id).eq("event_type", outcome)
            .eq("dedupe_key", details["email"]).limit(1).execute()
        )
    return result.data[0] if result.data else {"event_type": outcome, "details": details}
