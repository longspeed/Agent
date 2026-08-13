"""Unit tests for leads.find_leads()'s dedup logic.

Mocks every external call (LLM, Tavily, Sheets, and the plan quota meter) so
this runs with no network access and no real credentials. Run with:
    python -m unittest outreach-agent.tests.test_leads -v
(from the repo root) or `python -m unittest discover` from outreach-agent/.

That promise needs maintaining when find_leads grows a new dependency. The
plan-quota gate added one -- plans.check reads the usage meter out of Supabase --
and because nothing here faked it, this file started issuing live queries against
the production database on every run.
"""
import unittest
from unittest.mock import patch

import leads
import sheets


def _candidate(name, email_guess, company="Acme", reason="fits the target"):
    return {"name": name, "company": company, "url": "https://acme.example", "reason": reason, "email_guess": email_guess}


class FindLeadsTests(unittest.TestCase):
    def setUp(self):
        # Deliberately not a valid UUID. If a future dependency reaches the real
        # database again, Postgres rejects this id outright instead of quietly
        # reading production data and making the test's result depend on it.
        self.account = {"id": "acct-1"}
        patches = {
            "leads._expand_queries": patch("leads._expand_queries", return_value=["q1"]),
            "search.tavily_search": patch("leads.search.tavily_search", return_value=[]),
            "sheets.get_all_rows": patch("leads.sheets.get_all_rows", return_value=[]),
            "sheets.append_rows": patch("leads.sheets.append_rows"),
            # The plan-quota gate's usage meter. Zero consumed, so the real plan
            # rules run and simply find room -- rather than stubbing plans itself,
            # which would stop these tests exercising the gate at all.
            "accounts_db.usage_quantity_since": patch(
                "accounts_db.usage_quantity_since", return_value=0
            ),
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

    def test_guard_requires_whole_name_and_company_tokens_in_one_result(self):
        results = [{
            "url": "https://acme.example/about",
            "title": "Jane Doe, CEO of Acme",
            "content": "Jane Doe founded Acme.",
        }]
        candidates = [{"name": "An Doe", "company": "AI", "email_guess": "an@acme.example"}]
        # "An" and "AI" only occur inside unrelated words, so they are not
        # evidence that this person belongs to this company.
        assert leads._guard_candidates(candidates, results) == []

    def test_guard_accepts_a_missing_middle_initial_and_missing_url(self):
        results = [{
            "title": "Jane Doe, CEO of Acme",
            "content": "Jane Doe founded Acme. jane@acme.example",
        }]
        candidates = [{"name": "Jane A. Doe", "company": "Acme", "email_guess": "jane@acme.example"}]
        guarded = leads._guard_candidates(candidates, results)
        self.assertEqual(len(guarded), 1)
        self.assertEqual(guarded[0]["email_guess"], "jane@acme.example")


if __name__ == "__main__":
    unittest.main()
