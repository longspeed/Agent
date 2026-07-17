"""Unit tests for email_verification.verify()'s response-parsing branches.

Uses only the standard library (unittest + unittest.mock) -- no test
framework is configured for this repo yet (see CLAUDE.md's Health Stack
section), so this deliberately doesn't pick one. Run with:
    python -m unittest outreach-agent.tests.test_email_verification -v
(from the repo root) or `python -m unittest discover` from outreach-agent/.
"""
import unittest
from unittest.mock import patch, MagicMock

import requests

import email_verification


def _response(json_body, status_ok=True):
    resp = MagicMock()
    resp.json.return_value = json_body
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.HTTPError("bad status")
    return resp


class VerifyTests(unittest.TestCase):
    def test_empty_email_is_unverified_without_calling_provider(self):
        with patch("email_verification.requests.get") as mock_get:
            self.assertEqual(email_verification.verify(""), "unverified")
            mock_get.assert_not_called()

    def test_unconfigured_is_unverified_without_calling_provider(self):
        with patch("email_verification.NEVERBOUNCE_API_KEY", ""):
            with patch("email_verification.requests.get") as mock_get:
                self.assertEqual(email_verification.verify("a@b.com"), "unverified")
                mock_get.assert_not_called()

    def test_valid_result_is_verified(self):
        with patch("email_verification.NEVERBOUNCE_API_KEY", "key"):
            with patch("email_verification.requests.get", return_value=_response({"result": "valid"})):
                self.assertEqual(email_verification.verify("a@b.com"), "verified")

    def test_invalid_result_is_invalid(self):
        with patch("email_verification.NEVERBOUNCE_API_KEY", "key"):
            with patch("email_verification.requests.get", return_value=_response({"result": "invalid"})):
                self.assertEqual(email_verification.verify("a@b.com"), "invalid")

    def test_unknown_result_value_is_invalid(self):
        # Only an exact "valid" is trusted; anything else (catchall, disposable,
        # unknown, etc.) must NOT be treated as sendable.
        with patch("email_verification.NEVERBOUNCE_API_KEY", "key"):
            with patch("email_verification.requests.get", return_value=_response({"result": "catchall"})):
                self.assertEqual(email_verification.verify("a@b.com"), "invalid")

    def test_missing_result_field_is_unverified_not_invalid(self):
        # NeverBounce returns 200 with no "result" field on auth failure or
        # credit exhaustion -- must not be misread as a real invalid verdict.
        with patch("email_verification.NEVERBOUNCE_API_KEY", "key"):
            body = {"status": "general_failure", "message": "Insufficient credit balance."}
            with patch("email_verification.requests.get", return_value=_response(body)):
                self.assertEqual(email_verification.verify("a@b.com"), "unverified")

    def test_http_error_is_unverified(self):
        with patch("email_verification.NEVERBOUNCE_API_KEY", "key"):
            with patch("email_verification.requests.get", return_value=_response({}, status_ok=False)):
                self.assertEqual(email_verification.verify("a@b.com"), "unverified")

    def test_network_timeout_is_unverified(self):
        with patch("email_verification.NEVERBOUNCE_API_KEY", "key"):
            with patch("email_verification.requests.get", side_effect=requests.Timeout("timed out")):
                self.assertEqual(email_verification.verify("a@b.com"), "unverified")

    def test_connection_error_is_unverified(self):
        with patch("email_verification.NEVERBOUNCE_API_KEY", "key"):
            with patch("email_verification.requests.get", side_effect=requests.ConnectionError("refused")):
                self.assertEqual(email_verification.verify("a@b.com"), "unverified")


if __name__ == "__main__":
    unittest.main()
