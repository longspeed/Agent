"""Unit tests for leads.find_leads()'s dedup logic.

Mocks every external call (LLM, Tavily, Sheets) so this runs with no
network access and no real credentials. Run with:
    python -m unittest outreach-agent.tests.test_leads -v
(from the repo root) or `python -m unittest discover` from outreach-agent/.
"""
import unittest
from unittest.mock import patch

import leads
import sheets


def _candidate(name, email_guess, company="Acme", reason="fits the target"):
    return {"name": name, "company": company, "url": "https://acme.example", "reason": reason, "email_guess": email_guess}


class FindLeadsTests(unittest.TestCase):
    def setUp(self):
        # find_leads() doesn't use the account object directly (only passes
        # it through to sheets.*, which we mock), so a plain dict is enough.
        self.account = {"id": "acct-1"}
        patches = {
            "leads._expand_queries": patch("leads._expand_queries", return_value=["q1"]),
            "search.tavily_search": patch("leads.search.tavily_search", return_value=[]),
            "sheets.get_all_rows": patch("leads.sheets.get_all_rows", return_value=[]),
            "sheets.append_rows": patch("leads.sheets.append_rows"),
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)

    def test_dedups_candidates_sharing_a_guessed_email_within_the_batch(self):
        candidates = [_candidate("Alice", "alice@acme.com"), _candidate("Alice Duplicate", "alice@acme.com")]
        with patch("leads._extract_candidates", return_value=candidates):
            result = leads.find_leads(self.account, "target", limit=10)

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped_duplicates"], 1)

    def test_skips_candidates_already_in_the_sheet(self):
        existing_row = [""] * 9
        existing_row[sheets.COL_EMAIL] = "bob@acme.com"
        self.mocks["sheets.get_all_rows"].return_value = [(2, existing_row)]

        with patch("leads._extract_candidates", return_value=[_candidate("Bob", "bob@acme.com")]):
            result = leads.find_leads(self.account, "target", limit=10)

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_duplicates"], 1)

    def test_respects_limit(self):
        candidates = [_candidate(f"Person {i}", f"p{i}@acme.com") for i in range(5)]
        with patch("leads._extract_candidates", return_value=candidates):
            result = leads.find_leads(self.account, "target", limit=2)

        self.assertEqual(result["added"], 2)

    def test_new_rows_always_land_as_unverified_needing_review(self):
        candidates = [_candidate("New Person", "new@acme.com")]
        with patch("leads._extract_candidates", return_value=candidates):
            leads.find_leads(self.account, "target", limit=10)

        appended_rows = self.mocks["sheets.append_rows"].call_args[0][1]
        row = next(r for r in appended_rows if r[sheets.COL_EMAIL] == "new@acme.com")
        self.assertEqual(row[sheets.COL_STATUS], "Needs verification")
        self.assertEqual(row[sheets.COL_EMAIL_CONFIDENCE], "unverified")

    def test_candidates_without_a_name_or_email_guess_are_dropped(self):
        candidates = [_candidate("", "noname@acme.com"), _candidate("No Email", None)]
        with patch("leads._extract_candidates", return_value=candidates):
            result = leads.find_leads(self.account, "target", limit=10)

        self.assertEqual(result["added"], 0)


if __name__ == "__main__":
    unittest.main()
