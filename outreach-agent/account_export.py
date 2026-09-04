"""Build a portable, account-scoped export without exposing OAuth secrets.

The export is intentionally JSON rather than a database dump: customers can
inspect it, keep it, or hand it to another system. Google mail itself remains
in Google, so the export includes the Sendkeep records and the configured Sheet
reference but never pretends to export a customer's Gmail mailbox.
"""

from datetime import datetime, timezone
from urllib.parse import quote

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY


_client = None

# These are account-owned records that make the queue, audit trail, and
# follow-up state portable. The accounts row is queried separately by id.
_ACCOUNT_TABLES = (
    "outreach_drafts",
    "reviews",
    "tracked_threads",
    "commitments",
    "worker_events",
    "usage_events",
    "suppressions",
)

# Never put a credential or a session secret in a customer download, even if a
# future migration adds one to a selected table.
_SECRET_KEYS = {
    "google_token",
    "access_token",
    "refresh_token",
    "client_secret",
    "app_secret_key",
    "supabase_secret_key",
    "api_key",
    "token_json",
}


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def _redact(value):
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if key.lower() not in _SECRET_KEYS and "token" not in key.lower()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _portable_rows(table, rows):
    """Add a human-usable Gmail handoff link to tracked-thread exports."""
    if table != "tracked_threads":
        return rows
    portable = []
    for row in rows:
        item = dict(row)
        thread_id = (item.get("thread_id") or "").strip()
        if thread_id:
            item["gmail_url"] = (
                "https://mail.google.com/mail/u/0/#all/" + quote(thread_id, safe="")
            )
        portable.append(item)
    return portable


def _list_account_rows(client, table, account_id):
    """Page exports so PostgREST's default row cap cannot truncate history."""
    rows = []
    start = 0
    while True:
        query = client.table(table).select("*").eq("account_id", account_id)
        try:
            query = query.order("created_at", desc=False).range(start, start + 499)
        except AttributeError:
            page = query.execute().data or []
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


def build(account_id: str, account: dict | None = None) -> dict:
    """Return a complete Sendkeep export for one authenticated account.

    Failing loudly on a table read is preferable to handing a customer a file
    that looks complete while silently omitting part of their history.
    """
    client = _get_client()
    account_row = account or (
        client.table("accounts").select("*").eq("id", account_id).limit(1).execute().data[0]
    )
    data = {"account": _redact(account_row)}
    for table in _ACCOUNT_TABLES:
        rows = _list_account_rows(client, table, account_id)
        data[table] = _redact(_portable_rows(table, rows))

    return {
        "format": "sendkeep-account-export",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "data": data,
        "notes": [
            "OAuth credentials and session secrets are intentionally excluded.",
            "Gmail messages and Google Sheet rows remain in Google; this file contains Sendkeep's records and references.",
            "Tracked Gmail records include a direct Gmail handoff URL so the conversation remains usable without Sendkeep.",
        ],
    }
