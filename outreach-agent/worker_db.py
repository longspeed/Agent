"""Durable telemetry for the background reply watcher.

The watcher must keep doing useful work when Supabase is unavailable, so every
write in this module is best-effort. The health endpoint reads the same data,
which means an absent or stale run record is itself a visible failure instead
of something the web process has to guess from logs.
"""
from datetime import datetime, timezone
import os
import socket

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

RUNS_TABLE = "worker_runs"
EVENTS_TABLE = "worker_events"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def enabled() -> bool:
    """Whether durable telemetry is enabled.

    Production defaults to on. Tests and local one-off development can turn it
    off explicitly so a unit test never reaches the network by accident.
    """
    return os.environ.get("WORKER_TELEMETRY_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def host_id() -> str:
    return (os.environ.get("WORKER_HOST_ID") or socket.gethostname()).strip()[:120]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_run() -> int | None:
    if not enabled():
        return None
    try:
        result = _get_client().table(RUNS_TABLE).insert({
            "host_id": host_id(),
            "status": "running",
            "started_at": _now(),
        }).execute()
        row = result.data[0] if result.data else {}
        return row.get("id")
    except Exception as e:  # telemetry must never stop reply detection
        print(f"Could not record worker start: {e}")
        return None


def finish_run(run_id: int | None, summary: dict, status: str, error: str | None = None):
    if not enabled() or run_id is None:
        return
    fields = {
        "finished_at": _now(),
        "status": status,
        "checked_accounts": int(summary.get("checked", 0)),
        "skipped_accounts": int(summary.get("skipped_no_token", 0)),
        "failed_accounts": int(summary.get("failed", 0)),
        "degraded_accounts": int(summary.get("degraded", 0)),
        "degraded_reasons": dict(summary.get("degraded_reasons") or {}),
        "elapsed_seconds": float(summary.get("elapsed", 0.0)),
        "error": (error or "")[:1000] or None,
    }
    try:
        _get_client().table(RUNS_TABLE).update(fields).eq("id", run_id).execute()
    except Exception as e:
        print(f"Could not record worker finish: {e}")


def record_event(
    run_id: int | None,
    account_id: str | None,
    event_type: str,
    status: str,
    details: dict | None = None,
    dedupe_key: str | None = None,
):
    if not enabled():
        return
    # Every worker event has a stable key. Account-scoped events use the
    # account index; cycle events use the run index because account_id is NULL
    # for a cycle-level failure and PostgreSQL treats NULLs as distinct.
    dedupe_key = dedupe_key or f"{run_id or 'adhoc'}:{account_id or 'cycle'}:{event_type}"
    payload = {
        "run_id": run_id,
        "account_id": account_id,
        "event_type": event_type,
        "status": status,
        "details": details or {},
        "dedupe_key": dedupe_key,
    }
    try:
        conflict = (
            "run_id,event_type,dedupe_key"
            if run_id is not None
            else "account_id,event_type,dedupe_key"
        )
        _get_client().table(EVENTS_TABLE).upsert(
            payload, on_conflict=conflict
        ).execute()
    except Exception as e:
        print(f"Could not record worker event {event_type}: {e}")


def latest_run() -> dict | None:
    if not enabled():
        return None
    try:
        result = (
            _get_client().table(RUNS_TABLE).select("*")
            .order("started_at", desc=True).limit(1).execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        print(f"Could not read worker health: {e}")
        return None


def health_snapshot(stale_after_seconds: int = 900) -> dict:
    """Return a small, monitor-safe view of the last worker cycle."""
    run = latest_run()
    if not run:
        return {
            "ok": False,
            "status": "not_started",
            "last_started_at": None,
            "last_finished_at": None,
            "age_seconds": None,
        }

    finished_at = run.get("finished_at")
    if finished_at:
        last_seen = finished_at
    else:
        last_seen = run.get("started_at")

    try:
        parsed = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        age_seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    except (AttributeError, TypeError, ValueError):
        age_seconds = None

    fresh = age_seconds is not None and age_seconds <= stale_after_seconds
    run_status = run.get("status") or "unknown"
    # "degraded" is deliberately not healthy for an external monitor: the
    # worker may be alive, but at least one account is disconnected or failed
    # and the product promise is not being met for that account.
    ok = fresh and run_status == "success"
    return {
        "ok": ok,
        "status": "healthy" if ok else ("degraded" if fresh else "stale"),
        "last_started_at": run.get("started_at"),
        "last_finished_at": finished_at,
        "age_seconds": age_seconds,
        "run_status": run_status,
        "checked_accounts": run.get("checked_accounts", 0),
        "skipped_accounts": run.get("skipped_accounts", 0),
        "failed_accounts": run.get("failed_accounts", 0),
        "degraded_accounts": run.get("degraded_accounts", 0),
        "degraded_reasons": run.get("degraded_reasons") or {},
    }
