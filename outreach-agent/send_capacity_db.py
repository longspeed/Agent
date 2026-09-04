"""Atomic rolling send-cap reservations.

The Gmail API and Postgres cannot share a transaction.  Every send path first
reserves one slot in Postgres; only the request that owns that reservation may
call Gmail.  A reservation is deliberately kept after an ambiguous Gmail
failure, because under-counting an uncertain send is less safe than briefly
counting mail that may not have left.
"""

from supabase import create_client

from config import SUPABASE_SECRET_KEY, SUPABASE_URL


TABLE = "send_reservations"
_client = None


class SendCapacityExceeded(RuntimeError):
    pass


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def reserve(account_id, operation_key, limit):
    """Return True only for the caller that atomically owns one send slot."""
    result = _get_client().rpc(
        "try_reserve_send",
        {
            "p_account_id": account_id,
            "p_operation_key": str(operation_key),
            "p_limit": int(limit),
        },
    ).execute()
    return result.data is True


def complete(account_id, operation_key):
    """Close a reservation after the authoritative send log is durable."""
    result = _get_client().table(TABLE).update({"status": "completed"}).eq(
        "account_id", account_id
    ).eq("operation_key", str(operation_key)).eq("status", "reserved").execute()
    return bool(result.data)


def release(account_id, operation_key):
    """Release only when Gmail was definitely not called."""
    result = _get_client().table(TABLE).delete().eq(
        "account_id", account_id
    ).eq("operation_key", str(operation_key)).eq("status", "reserved").execute()
    return bool(result.data)
