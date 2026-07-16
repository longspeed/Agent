"""Per-account usage metering for pooled third-party keys (OpenRouter, Tavily).

The account whose request is being served is set once at the entry point
(server.py sets it per request/background job); agent.py and search.py then
record usage without every call site having to thread an account through."""
import contextvars

import accounts_db

_current_account_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_account_id", default=None
)


def set_account(account_id: str | None):
    _current_account_id.set(account_id)


def run_as(account_id: str | None, fn, *args, **kwargs):
    """Runs fn with usage attributed to account_id. Needed for worker threads
    (ThreadPoolExecutor, background jobs) which don't inherit the submitting
    thread's contextvars."""
    set_account(account_id)
    return fn(*args, **kwargs)


def record(kind: str, quantity: int = 1, detail: str = ""):
    """Best-effort: metering must never break the action being metered."""
    account_id = _current_account_id.get()
    if not account_id:
        return
    try:
        accounts_db.record_usage(account_id, kind, quantity, detail)
    except Exception as e:
        print(f"Usage metering failed ({kind}): {e}")
