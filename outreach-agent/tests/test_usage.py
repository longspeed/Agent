"""Unit tests for the COGS metering in usage.py.

The dollar figures these produce are what per-lead / per-draft margins get
computed from, so the arithmetic and — just as importantly — the refusal to
guess when inputs are missing are both pinned here.

Run with:
    python -m unittest outreach-agent.tests.test_usage -v
(from the repo root) or `python -m unittest discover` from outreach-agent/.
"""
import unittest
from unittest.mock import patch

import usage

# $3.00 per 1M input tokens, $15.00 per 1M output tokens.
PRICED = {"priced/model": (3.0, 15.0)}


class MeteringTestCase(unittest.TestCase):
    """Captures what would have been written to the usage_events table."""

    def setUp(self):
        self.recorded = []
        patcher = patch.object(
            usage.accounts_db,
            "record_usage",
            side_effect=lambda account_id, kind, quantity, detail: self.recorded.append((kind, quantity, detail)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        usage.set_account("acct-1")
        self.addCleanup(usage.set_account, None)

    def kinds(self):
        return {kind: quantity for kind, quantity, _ in self.recorded}


class RecordLlmTests(MeteringTestCase):
    def test_prices_a_known_model_by_its_input_output_split(self):
        with patch.dict(usage.MODEL_PRICES, PRICED, clear=True):
            usage.record_llm(usage.DRAFT_EMAIL, "priced/model", 1000, 200)

        # 1000 * $3/1M = $0.003, 200 * $15/1M = $0.003 -> 6000 micro-USD.
        self.assertEqual(self.kinds()["cost:draft_email"], 6000)
        self.assertEqual(self.kinds()["llm"], 1200)

    def test_output_tokens_are_priced_higher_than_input_tokens(self):
        """The whole reason the split is recorded: reply drafts carry a long
        thread as input, first-touch emails are output-heavy. Same token count
        must not produce the same cost."""
        with patch.dict(usage.MODEL_PRICES, PRICED, clear=True):
            usage.record_llm(usage.DRAFT_REPLY, "priced/model", 1000, 0)
            input_heavy = self.kinds()["cost:draft_reply"]
            self.recorded.clear()
            usage.record_llm(usage.DRAFT_REPLY, "priced/model", 0, 1000)
            output_heavy = self.kinds()["cost:draft_reply"]

        self.assertEqual(input_heavy, 3000)
        self.assertEqual(output_heavy, 15000)

    def test_unknown_model_is_unpriced_not_free(self):
        with patch.dict(usage.MODEL_PRICES, PRICED, clear=True):
            usage.record_llm(usage.DRAFT_EMAIL, "some/unlisted-model", 1000, 200)

        self.assertIn("cost:unpriced", self.kinds())
        self.assertNotIn("cost:draft_email", self.kinds())
        # Token volume is still metered even when we can't price it.
        self.assertEqual(self.kinds()["llm"], 1200)

    def test_missing_token_split_is_unpriced_rather_than_guessed(self):
        with patch.dict(usage.MODEL_PRICES, PRICED, clear=True):
            usage.record_llm(usage.DRAFT_EMAIL, "priced/model", None, None, total_tokens=1200)

        self.assertIn("cost:unpriced", self.kinds())
        self.assertNotIn("cost:draft_email", self.kinds())
        self.assertEqual(self.kinds()["llm"], 1200)

    def test_a_genuinely_free_model_costs_zero(self):
        with patch.dict(usage.MODEL_PRICES, {"free/model": (0.0, 0.0)}, clear=True):
            usage.record_llm(usage.DRAFT_EMAIL, "free/model", 1000, 200)

        self.assertEqual(self.kinds()["cost:draft_email"], 0)
        self.assertNotIn("cost:unpriced", self.kinds())

    def test_nothing_is_recorded_without_an_active_account(self):
        usage.set_account(None)
        with patch.dict(usage.MODEL_PRICES, PRICED, clear=True):
            usage.record_llm(usage.DRAFT_EMAIL, "priced/model", 1000, 200)

        self.assertEqual(self.recorded, [])


class RecordSearchTests(MeteringTestCase):
    def test_priced_search_is_charged_to_lead_sourcing(self):
        with patch.object(usage, "TAVILY_COST_PER_SEARCH_USD", 0.008):
            usage.record_search("some query")

        self.assertEqual(self.kinds()["cost:source_leads"], 8000)
        self.assertEqual(self.kinds()["search"], 1)

    def test_unset_search_price_is_unpriced_not_free(self):
        with patch.object(usage, "TAVILY_COST_PER_SEARCH_USD", 0):
            usage.record_search("some query")

        self.assertIn("cost:unpriced", self.kinds())
        self.assertNotIn("cost:source_leads", self.kinds())
        self.assertEqual(self.kinds()["search"], 1)


if __name__ == "__main__":
    unittest.main()
