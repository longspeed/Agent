"""Regression coverage for the browser favicon request."""
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
for name, value in {
    "OPENROUTER_API_KEY": "test-key",
    "APP_SECRET_KEY": "test-secret",
    "SUPABASE_URL": "http://localhost",
    "SUPABASE_SECRET_KEY": "test-secret-key",
}.items():
    os.environ.setdefault(name, value)

sys.path.insert(0, str(ROOT / "outreach-agent"))
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def test_favicon_uses_the_existing_brand_asset_instead_of_returning_404():
    """Regression: ISSUE-007 — every browser visit logged /favicon.ico 404.

    Found by /qa on 2026-09-03.
    Report: qa-reports/full-interactive-2026-09-03/report.md
    """
    response = TestClient(server.app).get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.content.lstrip().startswith(b"<svg")
