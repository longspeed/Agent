"""Regression coverage for the focused outreach decision desk.

Found by the design implementation and /qa pass on 2026-08-26.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest


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


def _request():
    return object()


def test_snooze_moves_a_queued_follow_up_back_to_waiting(monkeypatch):
    """Regression: a due item needs a real Snooze action, not a fake UI verb."""
    calls = {}
    monkeypatch.setattr(server, "_account", lambda request: {"id": "a1"})
    monkeypatch.setattr(
        server.reviews_db,
        "get_review",
        lambda account_id, review_id: {
            "id": review_id,
            "status": "pending",
            "kind": "follow_up",
            "source_draft_id": 44,
        },
    )

    def snooze(account_id, draft_id, due_at):
        calls["snooze"] = (account_id, draft_id, due_at)
        return True

    monkeypatch.setattr(server.drafts_db, "snooze_follow_up", snooze)
    monkeypatch.setattr(
        server.reviews_db,
        "dismiss",
        lambda account_id, review_id: calls.setdefault(
            "dismiss", (account_id, review_id)
        ),
    )

    result = server.snooze_follow_up(
        _request(), 90, server.SnoozeFollowUpBody(days=1)
    )

    assert result["status"] == "snoozed"
    assert calls["snooze"][:2] == ("a1", 44)
    assert calls["dismiss"] == ("a1", 90)
    assert datetime.fromisoformat(calls["snooze"][2]).tzinfo is not None


def test_snooze_fails_closed_when_the_follow_up_left_the_queue(monkeypatch):
    """A stale tab cannot snooze a follow-up that another action already owns."""
    monkeypatch.setattr(server, "_account", lambda request: {"id": "a1"})
    monkeypatch.setattr(
        server.reviews_db,
        "get_review",
        lambda account_id, review_id: {
            "id": review_id,
            "status": "pending",
            "kind": "follow_up",
            "source_draft_id": 44,
        },
    )
    monkeypatch.setattr(
        server.drafts_db, "snooze_follow_up", lambda *args: False
    )
    monkeypatch.setattr(
        server.reviews_db,
        "dismiss",
        lambda *args: pytest.fail("a failed source transition must not hide the review"),
    )

    with pytest.raises(server.HTTPException) as exc:
        server.snooze_follow_up(
            _request(), 90, server.SnoozeFollowUpBody(days=1)
        )

    assert exc.value.status_code == 409


def test_outreach_desk_is_the_only_primary_operator_surface():
    """Regression: analytics and dogfood furniture must not be reachable as a tab."""
    html = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")
    nav = (ROOT / "static" / "nav.js").read_text(encoding="utf-8")

    assert "const OUTREACH_TABS = ['inbox', 'promises', 'follow-ups', 'threads'];" in html
    assert 'data-outreach-tab-link="more"' not in nav
    assert 'src="/static/outreach-desk.js' in html
    assert 'src="/static/outreach-desk-renderers.js' in html
    assert 'id="guided-system-state"' in html
    assert 'id="guided-back"' in html


def test_outreach_desk_keeps_actions_deliberate_and_mobile_navigation_explicit():
    html = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")
    controller = (ROOT / "static" / "outreach-desk.js").read_text(
        encoding="utf-8"
    )
    renderers = (ROOT / "static" / "outreach-desk-renderers.js").read_text(
        encoding="utf-8"
    )

    assert "min-height:44px" in html
    assert ".is-detail-open .guided-detail-pane{display:flex;}" in html
    assert "event.ctrlKey || event.metaKey" in controller
    assert "event.key.toLowerCase() === 'e'" in controller
    assert "Snooze 1 day" in html
    assert "Result unknown — checking Gmail. Do not resend." in html
    assert "Send it, snooze it, or cancel it." in renderers


def test_send_permission_grant_returns_to_the_same_outreach_item():
    html = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")
    settings = (ROOT / "app" / "src" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )

    assert "sendkeep:return-after-google-grant" in html
    assert 'capability=send' in html
    assert "returnTo.startsWith('/outreach')" in settings
    assert "window.location.replace(returnTo)" in settings
