"""In-memory fixed-window rate limiting.

Single-process only, matching the current one-uvicorn-process deployment
(see TODOS.md's job-persistence entry for the same caveat). Would need a
shared store (Redis, or a Supabase table) once this runs behind more than
one worker process.
"""
import threading
import time

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def check(key: str, limit: int, window_seconds: int) -> bool:
    """Records a call under `key` and returns True if it's allowed — i.e.
    fewer than `limit` calls under this key landed in the trailing
    `window_seconds`. Returns False (and does NOT record the call) if the
    caller is already over the limit."""
    now = time.time()
    with _lock:
        hits = [t for t in _hits.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            _hits[key] = hits
            return False
        hits.append(now)
        _hits[key] = hits
        return True
