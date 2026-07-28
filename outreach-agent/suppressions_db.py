"""Per-account opt-out list in Supabase. An address lands here when its owner
clicks the unsubscribe link in an outreach email; from then on it is filtered
out of every batch and re-checked immediately before each individual send.
Every query filters on account_id -- combined with RLS on the table, one
tenant can never see or suppress against another's list."""
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "suppressions"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def _norm(email):
    return (email or "").strip().lower()


def add(account_id, email, source="unsubscribe_link"):
    """Records an opt-out. Idempotent: a second unsubscribe for the same
    address is a no-op rather than an error (the table's unique constraint
    would otherwise raise on the duplicate)."""
    email = _norm(email)
    if not email:
        return
    if is_suppressed(account_id, email):
        return
    _get_client().table(TABLE).insert({
        "account_id": account_id,
        "email": email,
        "source": source,
    }).execute()


def is_suppressed(account_id, email):
    email = _norm(email)
    if not email:
        return False
    result = (
        _get_client().table(TABLE)
        .select("id")
        .eq("account_id", account_id)
        .eq("email", email)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def list_source_timestamps(account_id, source):
    """When each of this account's suppressions for one reason was added, as
    aware datetimes, oldest first.

    The authoritative record behind the bounce-rate guard (see bounces.stats).
    Rows here are append-only and ours; the sheet's "Bounced" rows are a
    spreadsheet the operator can edit, so any safety threshold measured against
    those can be cleared by deleting them.

    Returns timestamps rather than a count because stats() needs two numbers from
    them -- the total, which the acknowledgement watermark is compared against,
    and how many fall inside the rate window -- and the row count per account is
    small enough that one fetch beats two counting queries."""
    result = (
        _get_client().table(TABLE)
        .select("created_at")
        .eq("account_id", account_id)
        .eq("source", source)
        .order("created_at")
        .execute()
    )
    stamps = []
    for row in result.data:
        parsed = _parse_timestamp(row.get("created_at"))
        if parsed is not None:
            stamps.append(parsed)
    return stamps


def _parse_timestamp(value):
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def list_suppressed_emails(account_id):
    """The full opt-out set for an account, lower-cased, as a Python set so
    callers can filter a batch in one pass instead of a query per address."""
    result = (
        _get_client().table(TABLE)
        .select("email")
        .eq("account_id", account_id)
        .execute()
    )
    return {_norm(row["email"]) for row in result.data}
