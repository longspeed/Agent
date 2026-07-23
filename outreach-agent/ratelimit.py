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

# Opportunistic eviction. check() trims a key's own timestamps on access, but
# a key that's never touched again (a one-off signup IP, say) would otherwise
# sit in _hits forever -- an unbounded leak over the process's lifetime. Once a
# key's most recent hit is older than the largest window we've been asked
# about, it can never contribute to any future limit, so it's safe to drop.
# Swept at most once per _SWEEP_INTERVAL so the hot path stays O(1)-ish.
_SWEEP_INTERVAL = 300
_last_sweep = 0.0
_max_window = 0.0


def _sweep(now: float) -> None:
    horizon = now - _max_window
    stale = [k for k, hits in _hits.items() if not hits or hits[-1] < horizon]
    for k in stale:
        del _hits[k]


def check(key: str, limit: int, window_seconds: int) -> bool:
    """Records a call under `key` and returns True if it's allowed — i.e.
    fewer than `limit` calls under this key landed in the trailing
    `window_seconds`. Returns False (and does NOT record the call) if the
    caller is already over the limit."""
    global _last_sweep, _max_window
    now = time.time()
    with _lock:
        _max_window = max(_max_window, window_seconds)
        if now - _last_sweep > _SWEEP_INTERVAL:
            _sweep(now)
            _last_sweep = now
        hits = [t for t in _hits.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            _hits[key] = hits
            return False
        hits.append(now)
        _hits[key] = hits
        return True
