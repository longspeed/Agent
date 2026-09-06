"""Regression coverage for authenticated dashboard queue links."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_cards_open_the_matching_tab_instead_of_removed_legacy_sections():
    """Regression: ISSUE-003 — the promise card opened Inbox through a stale hash.

    Found by /qa on 2026-09-03.
    Report: qa-reports/full-interactive-2026-09-03/report.md
    """
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'href="/outreach?tab=inbox"' not in html
    assert html.count('href="/outreach?tab=promises"') >= 6
    assert 'id="pipeline-you-owe"' in html
    assert 'id="pipeline-they-owe"' in html
    assert 'id="pipeline-due"' in html
    assert 'id="pipeline-completed"' in html
    assert "/outreach#reviews-list" not in html
    assert "/outreach#commitments-list" not in html
    assert "/outreach#deal-view" not in html
