"""In-memory fixed-window rate limiting.

Single-process only, matching the current one-uvicorn-process deployment
(see TODOS.md's job-persistence entry for the same caveat). Would need a
shared store (Redis, or a Supabase table) once this runs behind more than
one worker process.

Two properties of that design are worth stating plainly, because both fail
silently -- an under-enforcing limiter and a working one produce identical
output on every request that is under the limit, which is nearly all of them:

  Restart amnesia. State is process memory, so every window empties on restart.
  A deploy, a crash loop, or an OOM kill hands back a full login allowance with
  no signal. Inherent to the design rather than a bug, and the reason the login
  limit is a speed bump and not the account-security control.

  Worker multiplication. Behind N worker processes each has its own _hits, so
  the effective limit is not N x limit but something between limit and N x limit
  depending on which worker answers -- a limit that cannot be reasoned about at
  all. Unlike the above this is a misconfiguration, so enforcement_warnings()
  detects it and the startup diagnostic prints it.
"""
import os
import sys
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


def _configured_worker_count() -> int:
    """How many worker processes this deployment is asking for, from the two
    places it is actually set. Returns 1 when it cannot tell, because guessing
    high would cry wolf on every boot."""
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    if raw.isdigit() and int(raw) > 1:
        return int(raw)
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--workers" and i + 1 < len(argv) and argv[i + 1].strip().isdigit():
            return int(argv[i + 1])
        if arg.startswith("--workers="):
            tail = arg.split("=", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
    return 1


def enforcement_warnings() -> list[str]:
    """Reasons this limiter is not enforcing what its call sites assume.

    Deliberately silent about restart amnesia, which is inherent to the design
    and documented at the top of this module: a warning printed on every single
    boot is a warning an operator learns to scroll past, which would cost more
    than it buys. This reports only the part that is a mistake and fixable."""
    workers = _configured_worker_count()
    if workers > 1:
        return [
            f"rate limiting is in-memory and per-process, but this deployment is "
            f"configured for {workers} workers. Every limit -- including login "
            f"({workers}x10 attempts per 5 min instead of 10) -- is effectively "
            "unenforced and cannot be reasoned about. Run one worker, or move the "
            "limiter to a shared store."
        ]
    return []


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
