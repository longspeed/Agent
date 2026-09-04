"""Regression coverage for the live QA findings from 2026-09-03."""
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for name, value in {
    "OPENROUTER_API_KEY": "test-key",
    "APP_SECRET_KEY": "test-secret",
    "SUPABASE_URL": "http://localhost",
    "SUPABASE_SECRET_KEY": "test-secret-key",
}.items():
    os.environ.setdefault(name, value)

sys.path.insert(0, str(ROOT / "outreach-agent"))

import reviews_db  # noqa: E402


def test_answered_elsewhere_is_retained_for_audit_but_not_shown_as_open_work():
    """Regression: ISSUE-001 — an already-answered reply inflated the open queue.

    Found by /qa on 2026-09-03.
    Report: qa-reports/full-interactive-2026-09-03/report.md
    """
    assert "answered_elsewhere" not in reviews_db.VISIBLE_STATUSES
    assert "answered_elsewhere" not in reviews_db.SENDABLE_STATUSES
