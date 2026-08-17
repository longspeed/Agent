"""Regression contracts for issues found by /qa on 2026-08-17.

Report: qa-reports/qa-report-daytonaproxy01-net-2026-08-17.md
"""
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

import auth  # noqa: E402
import server  # noqa: E402


def test_logged_out_app_page_redirects_to_login(monkeypatch):
    """Regression: ISSUE-005 — /settings rendered a blank page after logout."""
    monkeypatch.setattr(auth, "verify_session_token", lambda token: None)
    response = TestClient(server.app).get("/settings", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/settings"
    assert response.headers["cache-control"] == "private, no-store"


def test_root_auth_variants_are_not_cacheable(monkeypatch):
    """Regression: ISSUE-001 — cached marketing HTML survived Google sign-in."""
    client = TestClient(server.app)
    monkeypatch.setattr(auth, "verify_session_token", lambda token: None)
    public = client.get("/")
    assert public.headers["cache-control"] == "private, no-store"
    assert public.headers["vary"] == "Cookie"

    monkeypatch.setattr(auth, "verify_session_token", lambda token: "acct-1")
    private = client.get("/")
    assert private.headers["cache-control"] == "private, no-store"
    assert private.headers["vary"] == "Cookie"
    assert private.content != public.content


def test_follow_up_migration_contains_every_runtime_column():
    """Regression: ISSUE-002/003 — production missed part of the grouped schema."""
    sql = (ROOT / "outreach-agent" / "migrations" /
           "20260813_add_follow_up_workflow.sql").read_text(encoding="utf-8").lower()
    for column in (
        "outreach_send_mode", "follow_up_delay_days", "thread_id",
        "follow_up_due_at", "follow_up_status", "follow_up_cancel_reason",
        "follow_up_sent_at", "follow_up_replied_at", "kind", "source_draft_id",
    ):
        assert f"{column}" in sql
    assert "notify pgrst, 'reload schema'" in sql


def test_public_copy_matches_auto_send_and_has_no_launch_placeholders():
    """Regression: ISSUE-004/006/007 — public copy contradicted the product."""
    pages = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "static/security.html", "static/privacy.html", "static/terms.html",
            "static/acceptable-use.html", "site/src/components/sections.tsx",
        )
    ).lower()
    for banned in (
        "nothing sends without an explicit confirmation",
        "draft pending legal review",
        "do not rely on this as a binding contract",
        "your registered business address goes here",
    ):
        assert banned not in pages
    assert "auto mode" in pages
