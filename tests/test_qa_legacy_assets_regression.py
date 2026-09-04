"""Regression coverage for production-safe legacy page assets."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_authenticated_legacy_pages_use_compiled_css_and_no_gsap_runtime():
    """Regression: ISSUE-005 — production loaded Tailwind CDN and stale GSAP code.

    Found by /qa on 2026-09-03.
    Report: qa-reports/full-interactive-2026-09-03/report.md
    """
    outreach = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    for html in (outreach, dashboard):
        assert "cdn.tailwindcss.com" not in html
        assert "/static/legacy-tailwind.css" in html
    assert "gsap" not in outreach.lower()
    assert (ROOT / "static" / "legacy-tailwind.css").stat().st_size > 10_000
