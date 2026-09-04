"""Regression coverage for session-aware navigation on the public guide."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_nav_checks_the_session_before_choosing_account_actions():
    """Regression: ISSUE-008 — signed-in guide visitors were offered Sign in.

    Found by /qa on 2026-09-03.
    Report: qa-reports/full-interactive-2026-09-03/report.md
    """
    source = (ROOT / "app" / "src" / "components" / "NavBar.tsx").read_text(
        encoding="utf-8"
    )

    assert "fetch('/api/me', { credentials: 'same-origin' })" in source
    assert "publicSession === 'signed-in'" in source
    assert "publicSession === 'signed-out'" in source
    assert "publicPage ? publicSignInLink" not in source
