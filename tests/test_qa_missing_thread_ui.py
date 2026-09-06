"""Regression coverage for live-account QA issue ISSUE-008."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_account_export_never_returns_oauth_secrets(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace(account_id="acct-1"))
    monkeypatch.setattr(
        server.account_export,
        "build",
        lambda account_id, account: {
            "format": "sendkeep-account-export",
            "account_id": account_id,
            "data": {"account": {"email": account["email"]}},
        },
    )
    monkeypatch.setattr(server, "_account", lambda request: {"id": "acct-1", "email": "owner@example.com"})

    response = server.export_account(request)

    assert response.media_type == "application/json"
    assert "sendkeep-account-export" in response.body.decode("utf-8")
    assert "oauth" not in response.body.decode("utf-8").lower()
    assert response.headers["content-disposition"].endswith('"sendkeep-account-export.json"')


def test_account_export_redaction_removes_nested_tokens():
    assert server.account_export._redact({
        "email": "owner@example.com",
        "google_token": "secret",
        "details": {"refresh_token": "also-secret", "count": 2},
    }) == {"email": "owner@example.com", "details": {"count": 2}}


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
    monkeypatch.setattr(server, "_account", lambda request: {
        "id": "acct-1", "google_sheet_id": "sheet-1",
    })
    monkeypatch.setattr(server.tracked_threads, "list_active", lambda _account_id: [])
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


def test_gmail_only_campaign_exposes_safe_tracked_id_for_removal(monkeypatch):
    monkeypatch.setattr(server, "_account", lambda request: {
        "id": "acct-1", "google_sheet_id": "",
    })
    monkeypatch.setattr(server.tracked_threads, "list_active", lambda account_id: [{
        "id": 73,
        "thread_id": "gmail-secret-thread",
        "name": "Test User",
        "email": "test@example.com",
        "company": "Example",
        "source": "gmail_discovery",
    }])
    monkeypatch.setattr(
        server.reviews_db, "latest_reply_for_thread", lambda *_args: None,
    )

    campaigns = server.list_campaigns(SimpleNamespace())

    assert campaigns[0]["trackedId"] == 73
    assert "thread_id" not in campaigns[0]
    assert "gmail-secret-thread" not in repr(campaigns)


def test_delete_thread_purges_contact_and_sheet_but_never_gmail(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_account", lambda request: {
        "id": "acct-1", "google_sheet_id": "sheet-1",
    })
    monkeypatch.setattr(server.tracked_threads, "get_by_id", lambda *_args: {
        "id": 73, "status": "active", "email": "contact@example.com",
    })
    monkeypatch.setattr(server.tracked_threads, "begin_delete", lambda *_args: True)
    monkeypatch.setattr(
        server.sheets, "delete_contact_rows",
        lambda account, email: calls.append(("sheet", account["id"], email)) or 2,
    )
    monkeypatch.setattr(
        server.tracked_threads, "purge_contact",
        lambda account_id, tracked_id: calls.append(("db", account_id, tracked_id)) or {"ok": True},
    )

    result = server.remove_tracked_thread(SimpleNamespace(), 73)

    assert calls == [
        ("sheet", "acct-1", "contact@example.com"),
        ("db", "acct-1", 73),
    ]
    assert result == {"ok": True, "sheet_rows_deleted": 2, "gmail_deleted": False}


def test_remove_thread_hides_foreign_or_missing_ids(monkeypatch):
    monkeypatch.setattr(server, "_account", lambda request: {"id": "acct-1"})
    monkeypatch.setattr(server.tracked_threads, "get_by_id", lambda *_args: None)

    with pytest.raises(server.HTTPException) as exc:
        server.remove_tracked_thread(SimpleNamespace(), 999)
    assert exc.value.status_code == 404


def test_remove_thread_fails_closed_when_send_state_cannot_be_verified(monkeypatch):
    monkeypatch.setattr(server, "_account", lambda request: {"id": "acct-1"})
    monkeypatch.setattr(server.tracked_threads, "get_by_id", lambda *_args: {
        "id": 73, "status": "active", "thread_id": "thread-1", "email": "contact@example.com",
    })
    monkeypatch.setattr(
        server.tracked_threads, "send_blocker",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(server.HTTPException) as exc:
        server.remove_tracked_thread(SimpleNamespace(), 73)
    assert exc.value.status_code == 503
    assert "Nothing was deleted" in exc.value.detail


def test_gmail_discovery_does_not_reactivate_a_removed_thread(monkeypatch):
    state = {"operation": None, "filters": [], "inserted": False}

    class Result:
        def __init__(self, data):
            self.data = data

    class Table:
        def update(self, payload):
            state["operation"] = "update"
            state["payload"] = payload
            return self

        def select(self, _columns):
            state["operation"] = "select"
            return self

        def insert(self, _payload):
            state["inserted"] = True
            state["operation"] = "insert"
            return self

        def eq(self, key, value):
            state["filters"].append(("eq", key, value))
            return self

        def neq(self, key, value):
            state["filters"].append(("neq", key, value))
            return self

        def limit(self, _value):
            return self

        def execute(self):
            if state["operation"] == "update":
                return Result([])
            if state["operation"] == "select":
                return Result([{"id": 73, "status": "removed"}])
            raise AssertionError("a removed thread must not be inserted again")

    class Client:
        def table(self, _name):
            return Table()

    monkeypatch.setattr(server.tracked_threads, "_get_client", lambda: Client())
    monkeypatch.setattr(server.tracked_threads, "enabled", lambda: True)

    row = server.tracked_threads.upsert_thread(
        "acct-1", "gmail-secret-thread", email="test@example.com",
        source="gmail_discovery",
    )

    assert row["status"] == "removed"
    assert ("neq", "status", "removed") in state["filters"]
    assert state["inserted"] is False


def test_dogfood_log_rejects_non_iso_dates_before_persisting():
    request = SimpleNamespace(state=SimpleNamespace(account_id="acct-1"))
    with pytest.raises(server.HTTPException) as exc:
        server.save_dogfood_log(request, server.DogfoodLogBody(date="not-a-date"))
    assert exc.value.status_code == 422
    assert "YYYY-MM-DD" in str(exc.value.detail)


def test_dogfood_log_accepts_only_aggregate_labels_and_known_safety_events(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace(account_id="acct-1"))
    body = server.DogfoodLogBody(date="2026-08-20", inbox_label="client@example.com")
    with pytest.raises(server.HTTPException) as exc:
        server.save_dogfood_log(request, body)
    assert exc.value.status_code == 422
    assert "email addresses" in str(exc.value.detail)

    body = server.DogfoodLogBody(date="2026-08-20", safety_event="made-up")
    with pytest.raises(server.HTTPException) as exc:
        server.save_dogfood_log(request, body)
    assert exc.value.status_code == 422
    assert "Unsupported safety event" in str(exc.value.detail)


def test_outreach_ui_disables_check_when_thread_is_unavailable():
    source = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")

    assert "c.status === 'Sent' && c.hasThread" in source
    assert ">Unavailable</span>" in source


def test_outreach_ui_deletes_contact_and_sheet_without_claiming_to_delete_gmail():
    source = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")

    assert "remove-thread-btn" in source
    assert "method: 'DELETE'" in source
    assert "/api/outreach/threads/${encodeURIComponent(trackedId)}" in source
    assert "every matching row in the connected Google Sheet" in source
    assert "Pending replies, promises, and follow-ups" in source
    assert "The Gmail conversation will not be deleted" in source
    assert ">Delete</button>" in source
    assert "gmail/delete" not in source


def test_threads_offer_a_thread_aware_ai_reply_composer():
    source = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")

    assert "draft-thread-reply-btn" in source
    assert "thread-reply-composer" in source
    assert "/api/outreach/threads/${encodeURIComponent(trackedId)}/draft-reply" in source
    assert "/api/outreach/replies/${encodeURIComponent(reviewId)}/send" in source
    assert "Rechecking Gmail" in source
    assert "Nothing sends until you click Send reply" in source
    assert "threadReplyRequestToken" in source
    assert "threadReplyMode === mode" in source
    assert "threadReplyReviewId === reviewId" in source
    assert "send-thread-email-btn" not in source
    assert "gmail-secret-thread" not in source


def test_threads_offer_a_reviewed_first_email_for_pending_sheet_contacts():
    source = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")

    assert "write-first-email-btn" in source
    assert ">Write email</button>" in source
    assert "/api/outreach/campaigns/row/${encodeURIComponent(row)}/draft" in source
    assert "/api/outreach/drafts/${encodeURIComponent(draftId)}/send" in source
    assert "Nothing sends until you click Send email" in source
    assert "recipient, opt-outs, bounces, duplicates, sender, and daily cap" in source


def test_prepare_contact_draft_endpoint_runs_one_account_scoped_job(monkeypatch):
    captured = {}
    account = {"id": "acct-1", "google_sheet_id": "sheet-1"}
    monkeypatch.setattr(server, "_account", lambda _request: account)
    monkeypatch.setattr(server.ratelimit, "check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        server.send_outreach,
        "prepare_draft_for_contact",
        lambda received, row, email: {
            "draft_id": 81, "row_index": row, "email": email, "subject": "Hello", "body": "Hi",
        },
    )
    monkeypatch.setattr(
        server,
        "start_job",
        lambda account_id, fn: captured.update(account_id=account_id, fn=fn) or "job-one",
    )

    response = server.prepare_contact_draft(
        SimpleNamespace(), 2, server.PrepareContactDraftBody(email="person@example.com")
    )

    assert response == {"job_id": "job-one"}
    assert captured["account_id"] == "acct-1"
    assert captured["fn"]()["draft_id"] == 81
    assert "acct-1" not in server._accounts_sending


def test_prepare_draft_for_contact_follows_email_after_sheet_sort(monkeypatch):
    row = [""] * (server.sheets.COL_EMAIL_CONFIDENCE + 1)
    row[server.sheets.COL_NAME] = "Sorted Person"
    row[server.sheets.COL_EMAIL] = "person@example.com"
    row[server.sheets.COL_COMPANY] = "Example"
    row[server.sheets.COL_LEAD_REASON] = "Asked about the product"
    row[server.sheets.COL_EMAIL_CONFIDENCE] = "verified"
    account = {"id": "acct-1"}
    monkeypatch.setattr(server.sheets, "get_all_rows", lambda _account: [(7, row)])
    monkeypatch.setattr(server.sheets, "require_full_header", lambda _account: None)
    monkeypatch.setattr(server.agent, "account_send_blockers", lambda _account: [])
    monkeypatch.setattr(server.bounces, "assert_sendable", lambda _account: None)
    monkeypatch.setattr(server.send_outreach, "assert_domain_safe", lambda _account: None)
    monkeypatch.setattr(server.suppressions_db, "is_suppressed", lambda *_args: False)
    monkeypatch.setattr(server.suppressions_db, "list_suppressed_emails", lambda *_args: set())
    monkeypatch.setattr(server.drafts_db, "count_sent_last_24_hours", lambda *_args: 0)
    monkeypatch.setattr(
        server.sheets,
        "campaign_readiness",
        lambda *_args, **_kwargs: {"eligible": [(7, row)], "remaining_today": 50},
    )
    monkeypatch.setattr(server.plans, "check", lambda *_args: None)
    monkeypatch.setattr(server.drafts_db, "get_pending_for_row", lambda *_args: None)
    monkeypatch.setattr(
        server.send_outreach,
        "_prepare_one",
        lambda _account, row_index, *_args: {"draft_id": 82, "row_index": row_index},
    )
    monkeypatch.setattr(
        server.drafts_db,
        "get_draft",
        lambda *_args: {
            "id": 82, "name": "Sorted Person", "email": "person@example.com",
            "company": "Example", "subject": "A subject", "body": "A body",
        },
    )

    result = server.send_outreach.prepare_draft_for_contact(
        account, 2, "person@example.com"
    )

    assert result["row_index"] == 7
    assert result["draft_id"] == 82
    assert result["subject"] == "A subject"


def test_delete_contact_migration_scrubs_pii_and_preserves_reimport_fence():
    sql = (ROOT / "supabase" / "migrations" / "20260831000000_delete_tracked_contacts.sql").read_text()
    lowered = sql.lower()

    assert "delete from public.reviews" in lowered
    assert "delete from public.commitments" in lowered
    assert "delete from public.outreach_drafts" in lowered
    assert "status = 'cancelled'" in lowered
    assert "if to_regclass('public.notification_outbox') is not null then" in lowered
    assert "email = '', name = '', company = ''" in lowered
    assert "status = 'removed'" in lowered
    assert "delete from public.suppressions" not in lowered
    assert "delete from public.worker_events" not in lowered


def test_delete_contact_guard_preserves_in_flight_and_uncertain_send_evidence():
    sql = (ROOT / "supabase" / "migrations" / "20260831010000_delete_send_reconciliation_guard.sql").read_text()
    lowered = sql.lower()

    assert "status = 'send_uncertain'" in lowered
    assert "status = 'sending'" in lowered
    assert "follow_up_status = 'sending'" in lowered
    assert "reason', 'send_uncertain'" in lowered
    assert "reason', 'send_in_progress'" in lowered
    assert lowered.index("status = 'send_uncertain'") < lowered.index("delete from public.reviews")


def test_outreach_ui_has_a_gmail_native_promise_ledger():
    source = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")

    assert "Promise ledger" in source
    assert "What do I owe?" in source
    assert "What do they owe me?" in source
    assert "scheduled-you-list" in source
    assert "scheduled-them-list" in source
    assert "completed-commitments-list" in source
    assert "promise-confirmation-receipt" in source
    assert 'id="revenue-leak-card"' not in source
    assert 'id="promise-precision-card"' not in source
    assert "Incorrect detection" in source
    assert "/commitments/${commitment.id}/complete" in source
    assert "resolved-commitments-list" in source
    assert "A derived view of Gmail activity" in source
    assert "Book next meeting" in source
    assert "calendarBookingLink" in source
    assert "Proof ledger" in source
    assert "Meeting attribution" in source
    assert "Observed reply rate" in source
    assert "monitored Gmail threads" in source
    assert "Evidence stays scoped to this account" in source
    assert "reply-monitoring-status" in source
    assert "renderMonitoringStatus" in source
    assert "Deals won" in source
    assert "evidence.outcomes.deal_won" in source
    assert "Copy proof summary" in source
    assert "Aggregate account evidence only" in source
    assert "const confirmedCount = (value) =>" in source
    assert "count > 0 ? count.toLocaleString('en-US') : 'Not tracked'" in source
    assert "Confirmed meetings: ${confirmedCount(outcomes.meeting_booked)}" in source
    assert "Founder / agency dogfood log" in source
    assert "/api/outreach/dogfood-log" in source
    assert "no prospect addresses, message bodies, or Gmail thread IDs" in source
    assert "Prefill observed counts" in source
    assert "Observed counts filled. Review them before saving." in source
    assert "Add or approve first-touch leads" in source
    assert "The approval contract" in source
    assert "What approval protects" in source
    assert "What approval cannot protect" in source
    assert "Grounded in the newest Gmail reply" in source


def test_settings_ui_exposes_per_inbox_checkout():
    source = (ROOT / "app" / "src" / "SettingsPage.tsx").read_text(encoding="utf-8")

    assert "/api/billing/checkout" in source
    assert "Agency Proof · $99/mo" not in source
    assert "Inbox is $29/mo" in source
    assert "one isolated Gmail" in source
    assert "/api/account/export" in source
    assert "Download my data" in source
    assert "Request account deletion" in source
    assert "Gmail sending is requested only" in source
    assert "/api/deliverability" in source
    assert "Domain safety check" in source
    assert "Optional first-touch sending pauses" in source


def test_public_site_leads_with_promise_ledger_and_has_no_agency_mailto_cta():
    hero = (ROOT / "site" / "src" / "components" / "hero.tsx").read_text(encoding="utf-8")
    sections = (ROOT / "site" / "src" / "components" / "sections.tsx").read_text(encoding="utf-8")
    pricing = (ROOT / "site" / "src" / "components" / "pricing.tsx").read_text(encoding="utf-8")

    assert "Know what you owe." in hero
    assert "Remember what they owe." in hero
    assert "VITE_PUBLIC_BOOKING_URL" in sections
    assert "VITE_PUBLIC_BOOKING_URL" in pricing
    assert "$400/mo" not in sections
    assert "$49/inbox/mo" in pricing
    assert "$29/mo" in pricing
    assert "$49/inbox/mo" in pricing
    assert "Agency pilot" in pricing
    assert "validation offers" in pricing
    assert "The core is read-only against Gmail Sent" in sections
    assert "subject=Agency%20pilot" not in sections
    assert "subject=Agency%20pilot" not in pricing
    assert "subject=Demo%20request" not in sections
