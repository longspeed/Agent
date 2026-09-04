"""Supabase-backed account storage: one row per customer, holding their login,
their encrypted Google OAuth token, their sheet id, and their outreach settings.
Also records per-account usage of metered third-party services (LLM, search)."""
import base64
import hashlib
import os
import re

from cryptography.fernet import Fernet
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY, APP_SECRET_KEY
import schema_contract

ACCOUNTS_TABLE = "accounts"
USAGE_TABLE = "usage_events"


class GoogleIdentityConflict(RuntimeError):
    """An email is already bound to a different immutable Google subject."""


# Settings a customer may edit about themselves via the API.
EDITABLE_SETTINGS = (
    "sender_name",
    "sender_company",
    "meeting_purpose",
    "custom_instructions",
    "calendar_booking_link",
    "notify_email",
    "google_sheet_id",
    "outreach_send_mode",
    "follow_up_delay_days",
)


_client = None


def _paged_rows(table_name, columns="*", *, filters=()):
    """Read a PostgREST table without relying on its implicit row cap."""
    rows = []
    start = 0
    while True:
        query = _get_client().table(table_name).select(columns)
        for column, value in filters:
            query = query.eq(column, value)
        try:
            query = query.range(start, start + 499)
        except AttributeError:
            # Small query fakes used by unit tests do not implement range;
            # production Supabase clients always do.
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


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


# --- Google token encryption at rest ---------------------------------------

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET_KEY.encode()).digest()))


def encrypt_secret(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()


# --- Accounts ---------------------------------------------------------------

def get_account_by_email(email: str) -> dict | None:
    result = _get_client().table(ACCOUNTS_TABLE).select("*").eq("email", email).execute()
    return result.data[0] if result.data else None


def get_account(account_id: str) -> dict | None:
    result = _get_client().table(ACCOUNTS_TABLE).select("*").eq("id", account_id).execute()
    return result.data[0] if result.data else None


def get_account_by_google_id(google_id: str) -> dict | None:
    result = _get_client().table(ACCOUNTS_TABLE).select("*").eq("google_id", google_id).execute()
    return result.data[0] if result.data else None


def link_or_create_google_account(email: str, google_id: str) -> dict:
    """Signs in with a Google identity: reuses an account already linked to
    this Google id, links it to an existing account with the same email, or
    creates a fresh account."""
    existing = get_account_by_google_id(google_id)
    if existing:
        return existing

    by_email = get_account_by_email(email)
    if by_email:
        bound_id = (by_email.get("google_id") or "").strip()
        if bound_id and bound_id != google_id:
            raise GoogleIdentityConflict(
                "This email is already linked to a different Google identity."
            )
        result = (
            _get_client().table(ACCOUNTS_TABLE)
            .update({"google_id": google_id}).eq("id", by_email["id"])
            .is_("google_id", "null").execute()
        )
        if result.data:
            return result.data[0]
        # A concurrent callback may have won the null-to-value transition.
        current = get_account(by_email["id"])
        if current and current.get("google_id") == google_id:
            return current
        raise GoogleIdentityConflict(
            "This email was linked to a different Google identity while signing in."
        )

    result = _get_client().table(ACCOUNTS_TABLE).insert({
        "email": email,
        "google_id": google_id,
    }).execute()
    return result.data[0]


_SHEET_URL_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def _normalize_sheet_id(value: str) -> str:
    """Accepts either a bare sheet ID or a full Google Sheets URL (people
    reliably paste the URL from their browser bar) and returns just the ID.
    Falls back to stripping query string, fragment, and trailing path
    segments so accidental junk on a bare ID (or an unrecognized URL shape)
    still comes out clean instead of passing through unchanged."""
    match = _SHEET_URL_ID.search(value)
    if match:
        return match.group(1)
    return value.split("?")[0].split("#")[0].split("/")[0]


def update_settings(account_id: str, settings: dict) -> dict:
    fields = {k: v for k, v in settings.items() if k in EDITABLE_SETTINGS and v is not None}
    if "google_sheet_id" in fields:
        fields["google_sheet_id"] = _normalize_sheet_id(fields["google_sheet_id"])
    if not fields:
        return get_account(account_id)
    result = (
        _get_client().table(ACCOUNTS_TABLE).update(fields).eq("id", account_id).execute()
    )
    return result.data[0]


def set_google_token(account_id: str, token_json: str | None):
    """Stores the account's Google OAuth token (encrypted), or clears it."""
    value = encrypt_secret(token_json) if token_json else None
    _get_client().table(ACCOUNTS_TABLE).update({"google_token": value}).eq("id", account_id).execute()


def list_accounts_with_a_sheet() -> list[dict]:
    """Every account that has configured a lead sheet -- the background
    reply-watcher's candidate list.

    Deliberately NOT also filtered on google_token being set. A Google token
    is cleared (set to None) the moment it stops refreshing -- see
    google_auth.get_credentials -- and Testing-mode tokens die on their own
    after 7 days regardless, which is the normal lifecycle of every account
    today, not an edge case. Filtering on the token too would remove an
    account from this list the moment its token dies, which freezes
    worker_heartbeat_at at exactly the moment something needs attention --
    indistinguishable from the worker itself being down. The caller checks
    google_token per account instead, and records why via
    set_worker_heartbeat rather than dropping the account from rotation."""
    return [
        a for a in _paged_rows(ACCOUNTS_TABLE)
        if (a.get("google_sheet_id") or "").strip()
    ]


def list_accounts_for_worker() -> list[dict]:
    """Return every account with work the Gmail watcher is responsible for.

    A Sheet is one way to discover threads, not a prerequisite for watching a
    thread already recorded in ``tracked_threads``. The fallback keeps legacy
    deployments working while that table's migration is being applied; once the
    durable table is enabled, accounts with only tracked Gmail threads remain in
    rotation even after their Sheet is removed.
    """
    accounts = _paged_rows(ACCOUNTS_TABLE)
    candidates = {
        account.get("id")
        for account in accounts
        if (account.get("google_sheet_id") or "").strip()
    }
    tracking_enabled = os.environ.get("TRACKED_THREADS_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    discovery_enabled = tracking_enabled and os.environ.get("GMAIL_THREAD_DISCOVERY_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    if discovery_enabled:
        # A connected Gmail inbox is itself a valid reply-desk source. The
        # watcher performs a bounded in:sent discovery and persists the thread
        # before it reads replies, so a Sheet is not a hidden prerequisite.
        candidates.update(
            account.get("id") for account in accounts if account.get("google_token")
        )
    if tracking_enabled:
        try:
            tracked = _paged_rows(
                "tracked_threads", "account_id", filters=(("status", "active"),)
            )
            candidates.update(row.get("account_id") for row in tracked)
        except Exception as e:
            # A pre-migration deployment must retain the old Sheet-backed worker
            # path instead of losing every account because the new table is absent.
            print(f"Durable thread account discovery unavailable; using Sheet accounts: {e}")
    return [account for account in accounts if account.get("id") in candidates]


def set_worker_heartbeat(account_id: str, error: str | None = None,
                         alert_disconnected: bool = False):
    """Records that the background reply-watcher looked at this account just
    now, and what (if anything) went wrong. The only way to tell "the worker
    is alive for this account" from "the worker is down" without reading
    process logs.

    Best-effort, matching usage.record's convention ("metering must never
    break the action being metered") -- this is most often called right after
    something has already gone wrong for this account, and a second failure
    here recording that must not replace the real error or stop the loop
    from moving on to the next account."""
    from datetime import datetime, timezone

    try:
        alert_disconnected = alert_disconnected or error == "Google disconnected — reconnect in Settings"
        client = _get_client()
        if hasattr(client, "rpc"):
            client.rpc("record_account_monitoring_state", {
                "p_account_id": account_id,
                "p_error": (error or "")[:500] or None,
                "p_alert_disconnected": alert_disconnected,
            }).execute()
        else:
            # Table-only unit fakes exercise the heartbeat data shape without
            # emulating PostgREST RPC dispatch.
            client.table(ACCOUNTS_TABLE).update({
                "worker_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "last_error": (error or "")[:500] or None,
            }).eq("id", account_id).execute()
    except Exception as e:
        print(f"Could not record worker heartbeat for account {account_id}: {e}")


def check_schema() -> list[str]:
    """Warnings about this database versus what the code expects. Empty is good.

    The contract is fetched once from PostgREST's OpenAPI document. Production
    treats any warning as a startup failure; local development prints it."""
    return schema_contract.check()


def set_bounce_ack(account_id: str, bounced_count: int):
    """Watermark for bounces.acknowledge(): how many hard bounces this operator
    has already reviewed and chosen to continue past.

    Not routed through update_settings because it is not a customer-editable
    setting -- it is a safety-guard acknowledgement, and the only writer is
    bounces.acknowledge()."""
    try:
        (
            _get_client().table(ACCOUNTS_TABLE)
            .update({"bounce_ack_count": int(bounced_count)})
            .eq("id", account_id).execute()
        )
    except Exception as e:
        # Point at the versioned migration rather than teaching operators to
        # mutate production with an untracked SQL snippet.
        raise RuntimeError(
            "Could not save the bounce acknowledgement. If this database has not "
            "been migrated, deploy Supabase migration "
            f"{schema_contract.BASELINE_MIGRATION}. ({e})"
        ) from e


def get_google_token(account: dict) -> str | None:
    """Returns the decrypted token JSON for an account row, or None."""
    if not account.get("google_token"):
        return None
    return decrypt_secret(account["google_token"])


# --- Usage metering ----------------------------------------------------------

def record_usage(account_id: str, kind: str, quantity: int = 1, detail: str = ""):
    _get_client().table(USAGE_TABLE).insert({
        "account_id": account_id,
        "kind": kind,
        "quantity": quantity,
        "detail": detail[:200],
    }).execute()


def usage_quantity_since(account_id: str, kind: str, since_iso: str | None = None) -> int:
    """Total quantity of one usage kind for one account, optionally only since a
    timestamp. This is what plans.py counts a quota against.

    Sums `quantity` rather than counting rows because a unit event may carry
    more than one unit -- one lead search returns a batch of leads and records
    them as a single row with quantity=N, and counting rows there would let a
    trial account source unlimited leads in one call.

    A failure here is deliberately allowed to propagate. Everywhere else in this
    module a metering error is swallowed so it cannot break the work, but this
    number is the *input to a refusal*: if the query fails and we return 0, the
    caller concludes the account has used nothing and lets the work through.
    That turns a transient database blip into free unlimited service, so the
    quota check fails closed by raising instead."""
    total = 0
    start = 0
    while True:
        query = (
            _get_client().table(USAGE_TABLE).select("quantity")
            .eq("account_id", account_id).eq("kind", kind)
        )
        if since_iso:
            query = query.gte("created_at", since_iso)
        try:
            query = query.range(start, start + 499)
        except AttributeError:
            page = query.execute().data or []
            total += sum(row["quantity"] for row in page)
            break
        page = query.execute().data or []
        if not page:
            break
        total += sum(row["quantity"] for row in page)
        start += len(page)
        if len(page) < 500:
            break
    return total


def set_plan(account_id: str, plan_name: str) -> None:
    """Records which plan an account is on. The only writers are the Stripe
    webhook (on checkout completion and on subscription cancellation) and the
    CLI, so this is not routed through update_settings -- a customer must never
    be able to PATCH themselves onto a plan they have not paid for, and
    update_settings is reachable from the settings endpoint."""
    try:
        (
            _get_client().table(ACCOUNTS_TABLE)
            .update({"plan": plan_name})
            .eq("id", account_id).execute()
        )
    except Exception as e:
        raise RuntimeError(
            "Could not save the plan. If this database has not been migrated "
            "yet, deploy Supabase migration "
            f"{schema_contract.BASELINE_MIGRATION}. ({e})"
        ) from e


def apply_billing_event(change: dict) -> str:
    """Atomically apply one ordered, subscription-scoped Stripe event."""
    result = _get_client().rpc("apply_stripe_plan_event", {
        "p_account_id": change["account_id"],
        "p_plan": change["plan"],
        "p_subscription_id": change.get("subscription_id"),
        "p_status": change.get("status") or "",
        "p_event_created": int(change["event_created"]),
        "p_event_id": change["event_id"],
        "p_event_rank": int(change.get("event_rank") or 0),
    }).execute()
    return str(result.data or "")


def usage_summary(account_id: str) -> dict:
    """Totals per kind, e.g. {"llm": {"events": 12, "quantity": 48211}, ...}."""
    rows = _paged_rows(
        USAGE_TABLE, "kind, quantity", filters=(("account_id", account_id),)
    )
    summary: dict[str, dict] = {}
    for row in rows:
        bucket = summary.setdefault(row["kind"], {"events": 0, "quantity": 0})
        bucket["events"] += 1
        bucket["quantity"] += row["quantity"]
    return summary
