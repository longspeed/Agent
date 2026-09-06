"""One read-only contract between application code and the Supabase schema.

The contract is evaluated by a protected Postgres function in one request. That
function reads the catalog for columns, nullability, indexes, RLS, and checks;
unlike PostgREST's OpenAPI document, it can prove the invariants that protect
send deduplication and tenant isolation.
"""
from dataclasses import dataclass

import requests

from config import SUPABASE_SECRET_KEY, SUPABASE_URL

BASELINE_MIGRATION = "20260827000000_sendkeep_baseline"
OUTBOX_MIGRATION = "20260830000000_notification_outbox"
ATOMIC_SEND_MIGRATION = "20260901000000_atomic_send_contract"
PROMISE_LEDGER_MIGRATION = "20260904000000_promise_ledger"
CONTRACT_RPC = "sendkeep_schema_contract"


@dataclass(frozen=True)
class Requirement:
    table: str
    column: str
    consequence: str
    nullable: bool | None = None


REQUIREMENTS = (
    Requirement("accounts", "bounce_ack_count", "bounce pauses cannot be acknowledged"),
    Requirement("accounts", "plan", "paid accounts fall back to trial limits"),
    Requirement("accounts", "worker_heartbeat_at", "worker freshness cannot be verified"),
    Requirement("reviews", "gmail_message_id", "reply dedupe is not race-safe"),
    Requirement("reviews", "original_draft_reply", "reply edit history is lost"),
    Requirement("reviews", "sent_at", "reply sends bypass the rolling send-cap log"),
    Requirement("reviews", "validator_problems", "unsafe draft warnings disappear"),
    Requirement("reviews", "degraded_classification", "reply inserts can fail"),
    Requirement("reviews", "rewritten", "AI rewrites pollute voice-learning data"),
    Requirement("reviews", "kind", "follow-up reviews cannot be distinguished"),
    Requirement("reviews", "row_index", "Gmail-only replies cannot be stored", nullable=True),
    Requirement("outreach_drafts", "original_subject", "subject edits are lost"),
    Requirement("outreach_drafts", "original_body", "draft edits are lost"),
    Requirement("outreach_drafts", "rewritten", "AI rewrites pollute voice-learning data"),
    Requirement("outreach_drafts", "sent_at", "the rolling send cap cannot be enforced"),
    Requirement("outreach_drafts", "follow_up_status", "follow-ups cannot be scheduled safely"),
    Requirement("tracked_threads", "id", "Gmail threads depend on mutable Sheet rows"),
    Requirement("commitments", "id", "detected promises are not durable"),
    Requirement("commitments", "estimated_value", "promise value cannot be saved"),
    Requirement("commitments", "dismissal_reason", "promise precision is not measurable"),
    Requirement("worker_runs", "id", "worker liveness is not durable"),
    Requirement("worker_runs", "degraded_accounts", "degraded runs cannot explain their scope"),
    Requirement("worker_runs", "degraded_reasons", "degraded runs cannot explain their cause"),
    Requirement("worker_events", "id", "worker failures disappear with process logs"),
    Requirement("worker_events", "dedupe_key", "worker events are not idempotent"),
)

INDEX_REQUIREMENTS = {
    "reviews_thread_message_idx": "reply dedupe is not race-safe",
    "worker_events_account_dedupe_unique_idx": "account event writes are not idempotent",
    "worker_events_run_dedupe_unique_idx": "cycle event writes are not idempotent",
}

RLS_TABLES = (
    "accounts", "reviews", "usage_events", "outreach_drafts", "suppressions",
    "tracked_threads", "commitments", "worker_runs", "worker_events",
)

CONSTRAINT_REQUIREMENTS = {
    "reviews_kind_check": "review kinds can contain unsupported values",
    "commitments_status_check": "commitment statuses can contain unsupported values",
    "commitments_dismissal_reason_check": "dismissal reasons can contain unsupported values",
}


def _rpc_snapshot(rpc_name: str, label: str, timeout_seconds: float = 15.0) -> dict:
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/{rpc_name}",
        headers=headers,
        json={},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    snapshot = response.json()
    if not isinstance(snapshot, dict):
        raise ValueError(f"{label} returned a non-object response")
    return snapshot


def _contract_snapshot(timeout_seconds: float = 15.0) -> dict:
    return _rpc_snapshot(CONTRACT_RPC, "schema contract", timeout_seconds)


def _outbox_snapshot(timeout_seconds: float = 15.0) -> dict:
    return _rpc_snapshot(
        "notification_outbox_contract", "notification outbox contract", timeout_seconds
    )


def _atomic_send_snapshot(timeout_seconds: float = 15.0) -> dict:
    return _rpc_snapshot("atomic_send_contract", "atomic send contract", timeout_seconds)


def _promise_ledger_snapshot(timeout_seconds: float = 15.0) -> dict:
    return _rpc_snapshot("promise_ledger_contract", "promise ledger contract", timeout_seconds)


def check() -> list[str]:
    try:
        snapshot = _contract_snapshot()
    except Exception as exc:
        return [f"Could not verify the database schema ({type(exc).__name__}: {exc})"]

    warnings = []
    if snapshot.get("version") != BASELINE_MIGRATION:
        warnings.append(
            f"Database schema contract version is {snapshot.get('version')!r}, "
            f"expected {BASELINE_MIGRATION}. Deploy the current Supabase migration."
        )

    columns = snapshot.get("columns") or {}
    nullable = snapshot.get("nullable") or {}
    for requirement in REQUIREMENTS:
        key = f"{requirement.table}.{requirement.column}"
        if not columns.get(key, False):
            warnings.append(
                f"{key} is missing, so {requirement.consequence}. "
                f"Apply Supabase migration {BASELINE_MIGRATION}."
            )
            continue
        if requirement.nullable is not None:
            is_nullable = bool(nullable.get(key, False))
            if is_nullable != requirement.nullable:
                expected = "nullable" if requirement.nullable else "NOT NULL"
                warnings.append(
                    f"{key} must be {expected}, so {requirement.consequence}. "
                    f"Apply Supabase migration {BASELINE_MIGRATION}."
                )

    for index, consequence in INDEX_REQUIREMENTS.items():
        if not (snapshot.get("indexes") or {}).get(index, False):
            warnings.append(
                f"{index} is missing, so {consequence}. "
                f"Apply Supabase migration {BASELINE_MIGRATION}."
            )

    for table in RLS_TABLES:
        if not (snapshot.get("rls") or {}).get(table, False):
            warnings.append(
                f"RLS is disabled on public.{table}, so tenant data is not isolated. "
                f"Apply Supabase migration {BASELINE_MIGRATION}."
            )

    for constraint, consequence in CONSTRAINT_REQUIREMENTS.items():
        if not (snapshot.get("constraints") or {}).get(constraint, False):
            warnings.append(
                f"{constraint} is missing, so {consequence}. "
                f"Apply Supabase migration {BASELINE_MIGRATION}."
            )
    try:
        outbox = _outbox_snapshot()
    except Exception as exc:
        warnings.append(
            f"Could not verify the notification outbox ({type(exc).__name__}: {exc}). "
            f"Apply Supabase migration {OUTBOX_MIGRATION}."
        )
    else:
        if outbox.get("version") != OUTBOX_MIGRATION:
            warnings.append(
                f"Notification outbox version is {outbox.get('version')!r}, expected "
                f"{OUTBOX_MIGRATION}. Apply the current Supabase migration."
            )
        for key in (
            "table", "rls", "ready_index", "dedupe_constraint",
            "status_constraint", "event_constraint", "source_constraint",
            "rpcs", "service_role_execute", "client_execute_revoked",
        ):
            if not outbox.get(key, False):
                warnings.append(
                    f"Notification outbox invariant {key!r} is missing. "
                    f"Apply Supabase migration {OUTBOX_MIGRATION}."
                )

    try:
        atomic_send = _atomic_send_snapshot()
    except Exception as exc:
        warnings.append(
            f"Could not verify atomic send safety ({type(exc).__name__}: {exc}). "
            f"Apply Supabase migration {ATOMIC_SEND_MIGRATION}."
        )
        return warnings
    if atomic_send.get("version") != ATOMIC_SEND_MIGRATION:
        warnings.append(
            f"Atomic send contract version is {atomic_send.get('version')!r}, expected "
            f"{ATOMIC_SEND_MIGRATION}. Apply the current Supabase migration."
        )
    for key in (
        "table", "rls", "client_table_access_revoked", "operation_unique",
        "status_constraint", "active_index", "active_row_index", "recipient_index",
        "reservation_rpc", "duplicate_check_rpc", "stripe_rpc", "service_role_execute",
        "client_execute_revoked",
    ):
        if not atomic_send.get(key, False):
            warnings.append(
                f"Atomic send invariant {key!r} is missing. "
                f"Apply Supabase migration {ATOMIC_SEND_MIGRATION}."
            )
    try:
        promise_ledger = _promise_ledger_snapshot()
    except Exception as exc:
        warnings.append(
            f"Could not verify the promise ledger ({type(exc).__name__}: {exc}). "
            f"Apply Supabase migration {PROMISE_LEDGER_MIGRATION}."
        )
        return warnings
    if promise_ledger.get("version") != PROMISE_LEDGER_MIGRATION:
        warnings.append(
            f"Promise ledger contract version is {promise_ledger.get('version')!r}, "
            f"expected {PROMISE_LEDGER_MIGRATION}. Apply the current Supabase migration."
        )
    for key in (
        "timezone_column", "events_table", "events_rls", "events_index",
        "events_unique", "transition_rpc", "due_rpc", "service_role_execute",
        "client_execute_revoked", "events_service_role_select",
    ):
        if not promise_ledger.get(key, False):
            warnings.append(
                f"Promise ledger invariant {key!r} is missing. "
                f"Apply Supabase migration {PROMISE_LEDGER_MIGRATION}."
            )
    return warnings
