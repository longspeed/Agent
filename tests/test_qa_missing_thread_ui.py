"""Regression coverage for live-account QA issue ISSUE-008."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace


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


def _row(*, thread_id: str) -> list[str]:
    row = [""] * (server.sheets.COL_EMAIL_CONFIDENCE + 1)
    row[server.sheets.COL_NAME] = "Test User"
    row[server.sheets.COL_EMAIL] = "test@example.com"
    row[server.sheets.COL_COMPANY] = "Example"
    row[server.sheets.COL_STATUS] = "Sent"
    row[server.sheets.COL_SENT_AT] = "2026-08-17T10:00:00Z"
    row[server.sheets.COL_THREAD_ID] = thread_id
    row[server.sheets.COL_EMAIL_CONFIDENCE] = "verified"
    return row


def test_campaign_api_exposes_capability_without_leaking_thread_id(monkeypatch):
    monkeypatch.setattr(server, "_account", lambda request: {"id": "acct-1"})
    monkeypatch.setattr(
        server.sheets,
        "get_all_rows",
        lambda account: [(2, _row(thread_id="gmail-secret-thread")), (3, _row(thread_id=""))],
    )

    campaigns = server.list_campaigns(SimpleNamespace())

    assert campaigns[0]["hasThread"] is True
    assert campaigns[1]["hasThread"] is False
    assert "thread_id" not in campaigns[0]
    assert "gmail-secret-thread" not in repr(campaigns)


def test_outreach_ui_disables_check_when_thread_is_unavailable():
    source = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")

    assert "c.status === 'Sent' && c.hasThread" in source
    assert ">Unavailable</span>" in source
