"""Regression coverage for explicit month-first promise dates."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(ROOT / "outreach-agent"))

import commitments  # noqa: E402


def test_explicit_month_first_date_is_attached_to_the_promise():
    """Regression: ISSUE-002 — December 12, 2026 rendered as no date detected.

    Found by /qa on 2026-09-03.
    Report: qa-reports/full-interactive-2026-09-03/report.md
    """
    candidates = commitments.extract_commitments(
        "Sure, I'll send the meeting invite for December 12, 2026.",
        reference_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        actor="operator",
    )

    assert len(candidates) == 1
    assert candidates[0]["due_text"] == "for December 12, 2026"
    assert candidates[0]["due_at"] == "2026-12-12T09:00:00+00:00"


def test_month_first_date_without_year_rolls_forward_when_needed():
    candidates = commitments.extract_commitments(
        "I will send the renewal on January 4.",
        reference_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
    )

    assert candidates[0]["due_at"] == "2027-01-04T09:00:00+00:00"
