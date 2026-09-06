"""Durable commitment candidates and confirmation state."""
from datetime import datetime, timezone
import os

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "commitments"
EVENTS_TABLE = "commitment_events"
# The Outreach queue is for candidates needing confirmation or promises that
# have reached their scheduled due date. Scheduled-but-not-due rows remain
# durable in the table without being rendered as if they still need approval.
ACTIVE_STATUSES = ("detected", "due")
LEDGER_STATUSES = ("detected", "scheduled", "due", "completed", "dismissed", "expired")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def enabled() -> bool:
    return os.environ.get("COMMITMENTS_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


def add_candidates(account_id, thread_id, source_message_id, candidates):
    """Insert candidates idempotently without overwriting a confirmation."""
    if not enabled() or not candidates:
        return []
    created = []
    for candidate in candidates:
        action = (candidate.get("action_text") or "").strip()
        kind = (candidate.get("commitment_type") or "promised_action").strip()
        if not action:
            continue
        query = (
            _get_client().table(TABLE).select("*")
            .eq("account_id", account_id)
            .eq("source_message_id", source_message_id)
            .eq("commitment_type", kind)
            .eq("action_text", action)
            .limit(1).execute()
        )
        if query.data:
            created.append(query.data[0])
            continue
        payload = {
            "account_id": account_id,
            "thread_id": thread_id,
            "source_message_id": source_message_id,
            "actor": candidate.get("actor") or "contact",
            "commitment_type": kind,
            "action_text": action,
            "due_at": candidate.get("due_at"),
            "due_text": candidate.get("due_text") or "",
            "evidence": candidate.get("evidence") or action,
            "confidence": float(candidate.get("confidence") or 0),
            "status": "detected",
            "created_at": _now(),
            "updated_at": _now(),
        }
        try:
            result = _get_client().table(TABLE).insert(payload).execute()
        except Exception:
            # A concurrent worker may have won the unique insert. Re-read the
            # authoritative row rather than creating a duplicate or dropping a
            # candidate from the operator's queue.
            query = (
                _get_client().table(TABLE).select("*")
                .eq("account_id", account_id)
                .eq("source_message_id", source_message_id)
                .eq("commitment_type", kind)
                .eq("action_text", action)
                .limit(1).execute()
            )
            if not query.data:
                raise
            created.append(query.data[0])
            continue
        if result.data:
            created.append(result.data[0])
    return created


def list_active(account_id):
    if not enabled():
        return []
    return (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id)
        .in_("status", list(ACTIVE_STATUSES))
        .order("created_at").execute().data
    )


def list_ledger(account_id, limit=200):
    """Return the durable promise ledger without collapsing lifecycle states."""
    if not enabled():
        return []
    return (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id)
        .in_("status", list(LEDGER_STATUSES))
        .order("updated_at", desc=True).limit(limit).execute().data or []
    )


def list_events(account_id, commitment_ids):
    """Return account-scoped transition history for the requested promises."""
    ids = [value for value in commitment_ids if value is not None]
    if not enabled() or not ids:
        return []
    return (
        _get_client().table(EVENTS_TABLE).select("*")
        .eq("account_id", account_id).in_("commitment_id", ids)
        .order("created_at").execute().data or []
    )


def repair_missing_due_date(account_id, commitment_id, due_at, due_text, confidence=None):
    """Fill a date missed by an older extractor without changing workflow state.

    The compare-and-set filters make this safe to run while a worker or operator
    is acting on the same promise: only an active row whose date is still NULL
    can be repaired. Confirmation remains a separate human action.
    """
    if not due_at:
        return None
    fields = {
        "due_at": due_at,
        "due_text": due_text or "",
        "updated_at": _now(),
    }
    if confidence is not None:
        fields["confidence"] = float(confidence)
    result = (
        _get_client().table(TABLE).update(fields)
        .eq("account_id", account_id).eq("id", commitment_id)
        .in_("status", list(ACTIVE_STATUSES))
        .is_("due_at", "null").execute()
    )
    return result.data[0] if result.data else None


def list_due(account_id, now=None):
    """Return scheduled promises whose reminder time has arrived."""
    if not enabled():
        return []
    reminder_at = now or _now()
    return (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id)
        .eq("status", "scheduled")
        .lte("reminder_at", reminder_at)
        .order("reminder_at").execute().data
    )


def get(account_id, commitment_id):
    if not enabled():
        return None
    result = (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id).eq("id", commitment_id).limit(1).execute()
    )
    return result.data[0] if result.data else None


def _transition(account_id, commitment_id, to_status, transition_key, **fields):
    payload = {
        "p_account_id": account_id,
        "p_commitment_id": commitment_id,
        "p_to_status": to_status,
        "p_transition_key": transition_key,
        "p_due_at": fields.get("due_at"),
        "p_reminder_at": fields.get("reminder_at"),
        "p_timezone": fields.get("timezone_name"),
        "p_dismissal_reason": fields.get("dismissal_reason"),
    }
    result = _get_client().rpc("transition_commitment_with_event", payload).execute()
    data = result.data
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def confirm(account_id, commitment_id, due_at=None, reminder_at=None, timezone_name="UTC"):
    """Schedule a detected promise and atomically append its audit event."""
    return _transition(
        account_id,
        commitment_id,
        "scheduled",
        f"schedule:{commitment_id}",
        due_at=due_at,
        reminder_at=reminder_at or due_at,
        timezone_name=timezone_name or "UTC",
    )


def complete(account_id, commitment_id):
    """Mark a scheduled/due promise complete, idempotently.

    Completion is an operator statement that the promised action was handled;
    it does not infer a meeting or deal. The UI can then ask for an explicit
    sales outcome without turning the commitments table into a CRM.
    """
    existing = get(account_id, commitment_id)
    if existing and existing.get("status") == "completed":
        return existing
    return _transition(
        account_id, commitment_id, "completed", f"complete:{commitment_id}"
    )


def list_completed(account_id, limit=20):
    if not enabled():
        return []
    return (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id).eq("status", "completed")
        .order("completed_at", desc=True).limit(limit).execute().data
    )


def summary(account_id):
    """Aggregate promise health, overdue work, and user-entered value."""
    if not enabled():
        return {
            "open_count": 0, "overdue_count": 0, "oldest_overdue": None,
            "valued_count": 0, "estimated_value": 0, "value_provenance": None,
            "completed_count": 0, "reviewed_count": 0, "dismissed_count": 0,
            "false_positive_rate": None, "precision_status": "insufficient_data",
            "stop_onboarding": False,
        }
    precision_available = True
    try:
        rows = (
            _get_client().table(TABLE).select(
                "status,dismissal_reason,estimated_value,due_at,created_at,thread_id,evidence,action_text"
            )
            .eq("account_id", account_id).in_(
                "status", list(LEDGER_STATUSES)
            ).execute().data or []
        )
    except Exception:
        # The value and dismissal-reason columns are optional until their
        # migrations are applied. Keep the promise graph useful on an older
        # schema, but do not pretend legacy dismissals are reviewed extraction
        # errors when the reason was never recorded.
        precision_available = False
        rows = (
            _get_client().table(TABLE).select("status")
            .eq("account_id", account_id).in_(
                "status", list(LEDGER_STATUSES)
            ).execute().data or []
        )
    open_rows = [row for row in rows if row.get("status") in {"detected", "scheduled", "due"}]
    overdue_rows = [row for row in rows if row.get("status") == "due"]
    overdue_rows.sort(key=lambda row: row.get("due_at") or row.get("created_at") or "")
    values = [float(row.get("estimated_value") or 0) for row in open_rows]
    decided = [row for row in rows if row.get("status") in {"scheduled", "due", "completed", "dismissed"}]
    dismissed_count = sum(
        row.get("status") == "dismissed" and row.get("dismissal_reason") == "incorrect"
        for row in decided
    )
    false_positive_rate = (
        dismissed_count / len(decided) if precision_available and decided else None
    )
    if false_positive_rate is None:
        precision_status = "insufficient_data"
    elif false_positive_rate < 0.10:
        precision_status = "healthy"
    elif false_positive_rate <= 0.20:
        precision_status = "dangerous"
    else:
        precision_status = "stop"
    return {
        "open_count": len(open_rows),
        "overdue_count": len(overdue_rows),
        "oldest_overdue": overdue_rows[0] if overdue_rows else None,
        "valued_count": sum(value > 0 for value in values),
        "estimated_value": sum(values),
        "value_provenance": "user_entered" if any(value > 0 for value in values) else None,
        "completed_count": sum(row.get("status") == "completed" for row in rows),
        "reviewed_count": len(decided),
        "dismissed_count": dismissed_count,
        "false_positive_rate": false_positive_rate,
        "precision_status": precision_status,
        "stop_onboarding": precision_status == "stop",
    }


def dismiss(account_id, commitment_id, reason="incorrect"):
    if reason not in {"incorrect", "not_applicable"}:
        raise ValueError(f"Unsupported commitment dismissal reason: {reason}")
    result = _transition(
        account_id,
        commitment_id,
        "dismissed",
        f"dismiss:{commitment_id}:{reason}",
        dismissal_reason=reason,
    )
    return bool(result)


def onboarding_precision():
    """Deployment-wide extraction precision used to gate new Gmail monitors.

    This returns aggregate counts only. No account or message data leaves the
    database query, and existing connected users keep access to their queue.
    """
    if not enabled():
        return {
            "reviewed_count": 0, "dismissed_count": 0, "account_count": 0,
            "false_positive_rate": None, "precision_status": "insufficient_data",
            "stop_onboarding": False,
        }
    try:
        rows = []
        start = 0
        while True:
            query = (
                _get_client().table(TABLE).select("account_id,status,dismissal_reason")
                .in_("status", ["scheduled", "due", "completed", "dismissed"])
            )
            try:
                query = query.range(start, start + 499)
            except AttributeError:
                rows.extend(query.execute().data or [])
                break
            page = query.execute().data or []
            if not page:
                break
            rows.extend(page)
            start += len(page)
            if len(page) < 500:
                break
    except Exception:
        return {
            "reviewed_count": 0, "dismissed_count": 0, "account_count": 0,
            "false_positive_rate": None, "precision_status": "insufficient_data",
            "stop_onboarding": False,
        }
    reviewed_count = len(rows)
    account_count = len({row.get("account_id") for row in rows if row.get("account_id")})
    dismissed_count = sum(
        row.get("status") == "dismissed" and row.get("dismissal_reason") == "incorrect"
        for row in rows
    )
    rate = dismissed_count / reviewed_count if reviewed_count else None
    # A single tenant or a handful of early labels must not pause onboarding
    # for the entire deployment. Require both enough decisions and diversity.
    if rate is None or reviewed_count < 30 or account_count < 3:
        status = "insufficient_data"
    elif rate < 0.10:
        status = "healthy"
    elif rate <= 0.20:
        status = "dangerous"
    else:
        status = "stop"
    return {
        "reviewed_count": reviewed_count,
        "account_count": account_count,
        "dismissed_count": dismissed_count,
        "false_positive_rate": rate,
        "precision_status": status,
        "stop_onboarding": status == "stop",
    }


def mark_due(account_id, commitment_id):
    client = _get_client()
    if hasattr(client, "rpc"):
        result = client.rpc("mark_commitment_due_with_alert", {
            "p_account_id": account_id,
            "p_commitment_id": commitment_id,
        }).execute()
        return bool(result.data)
    result = (
        client.table(TABLE).update({
            "status": "due", "updated_at": _now(),
        })
        .eq("account_id", account_id).eq("id", commitment_id)
        .eq("status", "scheduled").execute()
    )
    return bool(result.data)


def mark_due_with_alert(account_id, commitment_id):
    return mark_due(account_id, commitment_id)
