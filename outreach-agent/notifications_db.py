"""Durable notification outbox access for the service-role backend."""
from supabase import create_client

from config import SUPABASE_SECRET_KEY, SUPABASE_URL

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def _rpc(name, params=None):
    return _get_client().rpc(name, params or {}).execute().data


def queue_test(account_id):
    return _rpc("queue_test_notification", {"p_account_id": account_id})


def get_event(account_id, event_id):
    result = (
        _get_client().table("notification_outbox").select("id,status,last_error_code")
        .eq("account_id", account_id).eq("id", event_id).limit(1).execute()
    )
    return result.data[0] if result.data else None


def recover_expired_claims(account_id=None):
    return int(_rpc("recover_expired_notification_claims", {
        "p_account_id": account_id,
    }) or 0)


def claim(limit=25, lease_seconds=60, account_id=None):
    rows = _rpc("claim_notification_batch", {
        "p_limit": limit,
        "p_lease_seconds": lease_seconds,
        "p_account_id": account_id,
    })
    return rows if isinstance(rows, list) else []


def claim_event(account_id, event_id, lease_seconds=60):
    rows = _rpc("claim_notification_event", {
        "p_account_id": account_id,
        "p_event_id": event_id,
        "p_lease_seconds": lease_seconds,
    })
    return rows if isinstance(rows, list) else []


def finalize(event, outcome, *, provider_message_id=None, error_code=None,
             retry_at=None):
    return bool(_rpc("finalize_notification_delivery", {
        "p_event_id": event["id"],
        "p_claim_token": event["claim_token"],
        "p_outcome": outcome,
        "p_provider_message_id": provider_message_id,
        "p_error_code": error_code,
        "p_retry_at": retry_at,
    }))


def get_account(account_id):
    result = (
        _get_client().table("accounts").select("id,email,last_error,google_token")
        .eq("id", account_id).limit(1).execute()
    )
    return result.data[0] if result.data else None


def get_source(event):
    source_type = event.get("source_type")
    if source_type == "review":
        result = (
            _get_client().table("reviews").select("id,status,kind,source_draft_id")
            .eq("account_id", event["account_id"])
            .eq("id", event["source_id"]).limit(1).execute()
        )
    elif source_type == "commitment":
        result = (
            _get_client().table("commitments").select("id,status")
            .eq("account_id", event["account_id"])
            .eq("id", event["source_id"]).limit(1).execute()
        )
    else:
        return None
    return result.data[0] if result.data else None


def is_actionable(event, account=None):
    """Recheck current state immediately before the external email call."""
    event_type = event.get("event_type")
    if event_type == "test_alert":
        return True
    if event_type == "gmail_recovered":
        account = account or get_account(event["account_id"])
        return bool(account and not account.get("last_error"))
    if event_type == "gmail_disconnected":
        account = account or get_account(event["account_id"])
        return bool(account and (
            not account.get("google_token")
            or account.get("last_error") == "Google disconnected — reconnect in Settings"
        ))
    source = get_source(event)
    if not source:
        return False
    if event_type == "reply_unhandled":
        return source.get("kind") == "reply" and source.get("status") in {"pending", "flagged"}
    if event_type == "promise_due":
        return source.get("status") == "due"
    if event_type == "send_uncertain":
        return source.get("status") == "send_uncertain"
    if event_type == "follow_up_send_uncertain":
        if source.get("kind") != "follow_up" or source.get("status") != "pending":
            return False
        result = (
            _get_client().table("outreach_drafts").select("id,follow_up_status")
            .eq("account_id", event["account_id"])
            .eq("id", source.get("source_draft_id")).limit(1).execute()
        )
        draft = result.data[0] if result.data else None
        return bool(draft and draft.get("follow_up_status") == "sending")
    return False
