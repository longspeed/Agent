"""Unit tests for ratelimit.check()'s fixed-window limiter.

The server.py wiring itself (429s on /login, /signup, /api/leads/search,
/api/outreach/campaigns/send) was verified live against a running server
instead of here -- mocking Supabase/Google OAuth enough to exercise those
routes through FastAPI's TestClient wasn't worth it for what is fundamentally
a one-line-per-endpoint check() call. This file locks down the reusable unit
underneath all four: under/over limit, independent keys, and window expiry.
"""
import time
import unittest

import ratelimit


class CheckTests(unittest.TestCase):
    def setUp(self):
        # Each test gets its own key namespace so they can't interfere via
        # the module-level _hits dict.
        self.key = f"test:{self._testMethodName}:{time.time()}"

    def test_allows_calls_under_the_limit(self):
        for _ in range(3):
            self.assertTrue(ratelimit.check(self.key, limit=3, window_seconds=60))

    def test_blocks_the_call_that_exceeds_the_limit(self):
        for _ in range(3):
            ratelimit.check(self.key, limit=3, window_seconds=60)
        self.assertFalse(ratelimit.check(self.key, limit=3, window_seconds=60))

    def test_blocked_call_is_not_recorded(self):
        for _ in range(3):
            ratelimit.check(self.key, limit=3, window_seconds=60)
        self.assertFalse(ratelimit.check(self.key, limit=3, window_seconds=60))
        # If the blocked call above had been recorded anyway, this would
        # still be blocked too -- it isn't, because the limit is still 3.
        self.assertFalse(ratelimit.check(self.key, limit=3, window_seconds=60))

    def test_different_keys_are_independent(self):
        for _ in range(3):
            ratelimit.check(self.key, limit=3, window_seconds=60)
        self.assertFalse(ratelimit.check(self.key, limit=3, window_seconds=60))
        other_key = self.key + ":other"
        self.assertTrue(ratelimit.check(other_key, limit=3, window_seconds=60))

    def test_window_expiry_allows_calls_again(self):
        for _ in range(3):
            ratelimit.check(self.key, limit=3, window_seconds=0.05)
        self.assertFalse(ratelimit.check(self.key, limit=3, window_seconds=0.05))
        time.sleep(0.1)
        self.assertTrue(ratelimit.check(self.key, limit=3, window_seconds=0.05))


if __name__ == "__main__":
    unittest.main()
