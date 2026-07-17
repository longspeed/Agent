"""Unit tests for leads.find_leads()'s dedup + concurrent-verification logic.

Mocks every external call (LLM, Tavily, Sheets, NeverBounce) so this runs
with no network access and no real credentials. Run with:
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
        with patch("leads._extract_candidates", return_value=candidates), \
             patch("leads.email_verification.verify", return_value="verified") as mock_verify:
            result = leads.find_leads(self.account, "target", limit=10)

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped_duplicates"], 1)
        mock_verify.assert_called_once_with("alice@acme.com")

    def test_skips_candidates_already_in_the_sheet(self):
        existing_row = [""] * 9
        existing_row[sheets.COL_EMAIL] = "bob@acme.com"
        self.mocks["sheets.get_all_rows"].return_value = [(2, existing_row)]

        with patch("leads._extract_candidates", return_value=[_candidate("Bob", "bob@acme.com")]), \
             patch("leads.email_verification.verify") as mock_verify:
            result = leads.find_leads(self.account, "target", limit=10)

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_duplicates"], 1)
        mock_verify.assert_not_called()

    def test_respects_limit_and_stops_verifying_beyond_it(self):
        candidates = [_candidate(f"Person {i}", f"p{i}@acme.com") for i in range(5)]
        with patch("leads._extract_candidates", return_value=candidates), \
             patch("leads.email_verification.verify", return_value="verified") as mock_verify:
            result = leads.find_leads(self.account, "target", limit=2)

        self.assertEqual(result["added"], 2)
        self.assertEqual(mock_verify.call_count, 2)

    def test_verified_and_unverified_map_to_correct_row_status(self):
        candidates = [_candidate("Verified Person", "v@acme.com"), _candidate("Unverified Person", "u@acme.com")]

        def fake_verify(email):
            return "verified" if email == "v@acme.com" else "invalid"

        with patch("leads._extract_candidates", return_value=candidates), \
             patch("leads.email_verification.verify", side_effect=fake_verify):
            leads.find_leads(self.account, "target", limit=10)

        appended_rows = self.mocks["sheets.append_rows"].call_args[0][1]
        by_email = {row[sheets.COL_EMAIL]: row for row in appended_rows}
        self.assertEqual(by_email["v@acme.com"][sheets.COL_STATUS], "Ready for review")
        self.assertEqual(by_email["v@acme.com"][sheets.COL_EMAIL_CONFIDENCE], "verified")
        self.assertEqual(by_email["u@acme.com"][sheets.COL_STATUS], "Needs verification")
        self.assertEqual(by_email["u@acme.com"][sheets.COL_EMAIL_CONFIDENCE], "invalid")

    def test_candidates_without_a_name_or_email_guess_are_dropped(self):
        candidates = [_candidate("", "noname@acme.com"), _candidate("No Email", None)]
        with patch("leads._extract_candidates", return_value=candidates), \
             patch("leads.email_verification.verify") as mock_verify:
            result = leads.find_leads(self.account, "target", limit=10)

        self.assertEqual(result["added"], 0)
        mock_verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
