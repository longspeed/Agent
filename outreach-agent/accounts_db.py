"""Supabase-backed account storage: one row per customer, holding their login,
their encrypted Google OAuth token, their sheet id, and their outreach settings.
Also records per-account usage of metered third-party services (LLM, search)."""
import base64
import hashlib
import re

from cryptography.fernet import Fernet
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY, APP_SECRET_KEY

ACCOUNTS_TABLE = "accounts"
USAGE_TABLE = "usage_events"


# Settings a customer may edit about themselves via the API.
EDITABLE_SETTINGS = (
    "sender_name",
    "sender_company",
    "meeting_purpose",
    "custom_instructions",
    "calendar_booking_link",
    "notify_email",
    "google_sheet_id",
)

# Columns the code needs that a project created before them will not have, as
# (column, what breaks without it, the statement that adds it). Checked at boot
# rather than on first use -- see check_schema for why that distinction matters
# for this particular column.
_MIGRATED_COLUMNS = (
    (
        ACCOUNTS_TABLE,
        "bounce_ack_count",
        "a paused account cannot clear its own bounce pause, leaving deleting "
        "sheet rows as the only way out",
        "alter table public.accounts add column if not exists "
        "bounce_ack_count integer not null default 0;",
    ),
    (
        "outreach_drafts",
        "sent_at",
        "sending is blocked outright (drafts_db.require_send_log), because a send "
        "that cannot be recorded would be re-sent on the next batch",
        "alter table public.outreach_drafts add column if not exists sent_at timestamptz;",
    ),
    (
        ACCOUNTS_TABLE,
        "plan",
        "every account is treated as the free trial, so paid customers silently "
        "get trial quotas and a Stripe webhook has nowhere to write the plan it "
        "just collected money for",
        "alter table public.accounts add column if not exists plan text not null "
        "default 'trial';",
    ),
    (
        "reviews",
        "gmail_message_id",
        "reply dedupe falls back to matching the reply BODY, which collides on "
        "two identical short replies, 414s on a long quoted chain, and cannot "
        "carry a unique index -- so two processes detecting the same reply both "
        "queue it and the prospect can be answered twice",
        "alter table public.reviews add column if not exists gmail_message_id text; "
        "create unique index if not exists reviews_thread_message_idx on "
        "public.reviews (account_id, thread_id, gmail_message_id);",
    ),
    (
        "outreach_drafts",
        "original_body",
        "every edit an operator makes before sending is destroyed at send time "
        "-- mark_sent overwrites subject/body with the approved copy, so the "
        "pair (what the model wrote, what this person actually says) is lost "
        "and cannot be backfilled",
        "alter table public.outreach_drafts add column if not exists "
        "original_subject text; "
        "alter table public.outreach_drafts add column if not exists "
        "original_body text;",
    ),
    (
        "reviews",
        "original_draft_reply",
        "the same loss on the reply path: mark_sent overwrites draft_reply with "
        "what the operator sent, so what the model proposed is gone",
        "alter table public.reviews add column if not exists "
        "original_draft_reply text;",
    ),
    (
        "reviews",
        "validator_problems",
        "a reply draft that failed validation twice is queued anyway (losing the "
        "notification is worse), and without this column it looks identical to a "
        "clean one -- the operator is never told which draft the validator "
        "already objected to",
        "alter table public.reviews add column if not exists "
        "validator_problems text;",
    ),
    (
        "reviews",
        "degraded_classification",
        "add_review's insert fails outright rather than degrading, so reply "
        "detection stops entirely -- not just the degraded-lookup marker this "
        "column exists to carry",
        "alter table public.reviews add column if not exists "
        "degraded_classification boolean not null default false;",
    ),
    (
        ACCOUNTS_TABLE,
        "worker_heartbeat_at",
        "set_worker_heartbeat fails silently (it is best-effort by design), so "
        "the background reply-watcher has no way to prove it is actually "
        "running for this account -- a stale heartbeat and a missing column "
        "look identical from here",
        "alter table public.accounts add column if not exists "
        "worker_heartbeat_at timestamptz; "
        "alter table public.accounts add column if not exists last_error text;",
    ),
    (
        "outreach_drafts",
        "rewritten",
        "mark_rewritten's best-effort write fails silently, so Phase 6's future "
        "edit-diff corpus has no way to exclude a draft the model rewrote from "
        "one the operator wrote by hand -- the send itself is unaffected either "
        "way, since this write is deliberately separate from mark_sent",
        "alter table public.outreach_drafts add column if not exists "
        "rewritten boolean not null default false;",
    ),
    (
        "reviews",
        "rewritten",
        "the same gap on the reply path: mark_rewritten fails silently, and "
        "Phase 6 loses the ability to tell a model-rewritten reply from the "
        "operator's own voice -- again, never affects whether the reply sends",
        "alter table public.reviews add column if not exists "
        "rewritten boolean not null default false;",
    ),
)

_client = None


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
        result = (
            _get_client().table(ACCOUNTS_TABLE)
            .update({"google_id": google_id}).eq("id", by_email["id"]).execute()
        )
        return result.data[0]

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
    result = _get_client().table(ACCOUNTS_TABLE).select("*").execute()
    return [a for a in result.data if (a.get("google_sheet_id") or "").strip()]


def set_worker_heartbeat(account_id: str, error: str | None = None):
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
        _get_client().table(ACCOUNTS_TABLE).update({
            "worker_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "last_error": (error or "")[:500] or None,
        }).eq("id", account_id).execute()
    except Exception as e:
        print(f"Could not record worker heartbeat for account {account_id}: {e}")


def check_schema() -> list[str]:
    """Warnings about this database versus what the code expects. Empty is good.

    Run at startup, not lazily, because of when the gap would otherwise surface.
    bounce_ack_count is only ever needed by an account that is *already* paused
    for a bad bounce rate -- so the customers who hit a missing column are the
    ones already having a bad day, and the only exit left to them is deleting
    sheet rows, which is precisely the remediation bounces.pause_reason exists to
    avoid recommending. A warning at boot costs one query and turns that into
    something the operator fixes before anyone is stuck behind it."""
    try:
        _get_client().table(ACCOUNTS_TABLE).select("id").limit(1).execute()
    except Exception as e:
        # Unreachable database is a different problem with its own symptoms.
        # Reported as unverified rather than as a missing column, so this never
        # sends someone off to run a migration they may not need.
        return [f"Could not verify the accounts table schema: {e}"]

    warnings = []
    for table, column, consequence, statement in _MIGRATED_COLUMNS:
        try:
            _get_client().table(table).select(column).limit(1).execute()
        except Exception:
            warnings.append(
                f"{table}.{column} is missing, so {consequence}. Run: {statement}"
            )
    return warnings


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
        # The column is a documented migration (see README). Say so, rather than
        # surfacing a raw PostgREST error to someone clicking a button.
        raise RuntimeError(
            "Could not save the bounce acknowledgement. If this database has not "
            "been migrated yet, run: alter table public.accounts add column "
            f"bounce_ack_count integer not null default 0; ({e})"
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
    query = (
        _get_client().table(USAGE_TABLE)
        .select("quantity")
        .eq("account_id", account_id)
        .eq("kind", kind)
    )
    if since_iso:
        query = query.gte("created_at", since_iso)
    return sum(row["quantity"] for row in query.execute().data)


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
            "yet, run: alter table public.accounts add column if not exists "
            f"plan text not null default 'trial'; ({e})"
        ) from e


def usage_summary(account_id: str) -> dict:
    """Totals per kind, e.g. {"llm": {"events": 12, "quantity": 48211}, ...}."""
    result = (
        _get_client().table(USAGE_TABLE)
        .select("kind, quantity")
        .eq("account_id", account_id)
        .execute()
    )
    summary: dict[str, dict] = {}
    for row in result.data:
        bucket = summary.setdefault(row["kind"], {"events": 0, "quantity": 0})
        bucket["events"] += 1
        bucket["quantity"] += row["quantity"]
    return summary
