import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "outreach-agent"))

import notify
import drafts_db


class Response:
    status_code = 200
    content = b'{"id":"provider-1"}'

    def json(self):
        return {"id": "provider-1"}


class RejectedResponse(Response):
    status_code = 400
    content = b'{}'


def _event(event_type="reply_unhandled"):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "account_id": "account-1",
        "event_type": event_type,
        "source_type": "review",
        "source_id": "42",
        "claim_token": "00000000-0000-0000-0000-000000000002",
        "attempt_count": 1,
    }


def test_dispatch_uses_signed_in_email_and_provider_idempotency(monkeypatch):
    event = _event()
    finalized = []
    calls = []
    monkeypatch.setattr(notify.notifications_db, "recover_expired_claims", lambda **_kwargs: 0)
    monkeypatch.setattr(notify.notifications_db, "claim", lambda **_kwargs: [event])
    monkeypatch.setattr(
        notify.notifications_db, "get_account",
        lambda _account_id: {
            "email": "signed-in@example.com",
            "notify_email": "custom@example.com",
            "last_error": None,
        },
    )
    monkeypatch.setattr(notify.notifications_db, "is_actionable", lambda *_args: True)
    monkeypatch.setattr(
        notify.notifications_db, "finalize",
        lambda *args, **kwargs: finalized.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        notify.requests, "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    )
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_API_KEY", "test-key")
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_FROM", "Sendkeep <alerts@example.com>")

    result = notify.dispatch_pending(account_id="account-1")

    assert result["delivered"] == 1
    assert calls[0][1]["json"]["to"] == ["signed-in@example.com"]
    assert calls[0][1]["headers"]["Idempotency-Key"] == f"sendkeep/{event['id']}"
    assert finalized[0][0][1] == "delivered"


def test_resolved_event_is_cancelled_before_provider_call(monkeypatch):
    event = _event()
    finalized = []
    monkeypatch.setattr(notify.notifications_db, "recover_expired_claims", lambda **_kwargs: 0)
    monkeypatch.setattr(notify.notifications_db, "claim", lambda **_kwargs: [event])
    monkeypatch.setattr(notify.notifications_db, "get_account", lambda _id: {"email": "a@x.com"})
    monkeypatch.setattr(notify.notifications_db, "is_actionable", lambda *_args: False)
    monkeypatch.setattr(
        notify.notifications_db, "finalize",
        lambda *args, **kwargs: finalized.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        notify.requests, "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_API_KEY", "test-key")
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_FROM", "alerts@example.com")

    result = notify.dispatch_pending()

    assert result["cancelled"] == 1
    assert finalized[0][0][1] == "cancelled"


def test_email_copy_is_privacy_safe_and_deep_linked():
    forbidden = ["Jane", "jane@example.com", "send the contract", "traceback"]
    for event_type in (
        "reply_unhandled", "promise_due", "send_uncertain",
        "follow_up_send_uncertain", "gmail_disconnected", "gmail_recovered",
        "test_alert",
    ):
        subject, body, path = notify._message(_event(event_type))
        combined = f"{subject} {body}"
        assert not any(value in combined for value in forbidden)
        assert path.startswith("/")
    assert "review=42" in notify._message(_event("reply_unhandled"))[2]
    assert "commitment=42" in notify._message(_event("promise_due"))[2]
    assert "tab=follow-ups&review=42" in notify._message(
        _event("follow_up_send_uncertain")
    )[2]


def test_dispatch_event_claims_only_the_requested_event(monkeypatch):
    event = _event("test_alert")
    calls = []
    monkeypatch.setattr(
        notify.notifications_db, "claim_event",
        lambda account_id, event_id: calls.append((account_id, event_id)) or [event],
    )
    monkeypatch.setattr(
        notify.notifications_db, "recover_expired_claims",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("exact dispatch must not run maintenance")),
    )
    monkeypatch.setattr(
        notify.notifications_db, "claim",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("exact dispatch must not claim a batch")),
    )
    monkeypatch.setattr(
        notify.notifications_db, "get_account",
        lambda _account_id: {"email": "signed-in@example.com", "last_error": None},
    )
    monkeypatch.setattr(notify.notifications_db, "is_actionable", lambda *_args: True)
    monkeypatch.setattr(notify.notifications_db, "finalize", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(notify.requests, "post", lambda *_args, **_kwargs: Response())
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_API_KEY", "test-key")
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_FROM", "alerts@example.com")

    result = notify.dispatch_event(account_id="account-1", event_id=event["id"])

    assert calls == [("account-1", event["id"])]
    assert result["claimed"] == 1
    assert result["delivered"] == 1


def test_permanent_provider_failure_is_finalized_not_retried(monkeypatch):
    event = _event("test_alert")
    finalized = []
    monkeypatch.setattr(notify.notifications_db, "claim_event", lambda *_args: [event])
    monkeypatch.setattr(
        notify.notifications_db, "get_account",
        lambda _account_id: {"email": "signed-in@example.com", "last_error": None},
    )
    monkeypatch.setattr(notify.notifications_db, "is_actionable", lambda *_args: True)
    monkeypatch.setattr(
        notify.notifications_db, "finalize",
        lambda *args, **kwargs: finalized.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        notify.requests, "post", lambda *_args, **_kwargs: RejectedResponse()
    )
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_API_KEY", "test-key")
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_FROM", "alerts@example.com")

    result = notify.dispatch_event(account_id="account-1", event_id=event["id"])

    assert result["failed"] == 1
    assert result["retrying"] == 0
    assert finalized[0][0][1] == "failed"
    assert finalized[0][1]["error_code"] == "provider_400"


def test_follow_up_claim_uses_atomic_alert_rpc(monkeypatch):
    calls = []

    class RPC:
        data = True

        def execute(self):
            return self

    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return RPC()

    monkeypatch.setattr(drafts_db, "_get_client", lambda: Client())

    assert drafts_db.claim_follow_up_send("account-1", 44) is True
    assert calls == [("claim_follow_up_send_with_alert", {
        "p_account_id": "account-1", "p_draft_id": 44,
    })]


def test_test_alert_endpoint_dispatches_exact_event(monkeypatch):
    import server

    event_id = "00000000-0000-0000-0000-000000000099"
    calls = []
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_API_KEY", "test-key")
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(server, "_account", lambda _request: {
        "id": "account-1", "email": "signed-in@example.com",
    })
    monkeypatch.setattr(server.notifications_db, "queue_test", lambda _account_id: event_id)
    monkeypatch.setattr(
        server.notify, "dispatch_event",
        lambda **kwargs: calls.append(kwargs) or {"delivered": 1},
    )
    monkeypatch.setattr(
        server.notifications_db, "get_event",
        lambda _account_id, _event_id: {"status": "delivered"},
    )

    result = server.send_test_alert(object())

    assert result == {"ok": True, "recipient": "signed-in@example.com"}
    assert calls == [{"account_id": "account-1", "event_id": event_id}]


def test_test_alert_endpoint_does_not_call_a_failed_event_a_retry(monkeypatch):
    import server

    monkeypatch.setenv("TRANSACTIONAL_EMAIL_API_KEY", "test-key")
    monkeypatch.setenv("TRANSACTIONAL_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(server, "_account", lambda _request: {
        "id": "account-1", "email": "signed-in@example.com",
    })
    monkeypatch.setattr(server.notifications_db, "queue_test", lambda _account_id: "event-1")
    monkeypatch.setattr(server.notify, "dispatch_event", lambda **_kwargs: {"failed": 1})
    monkeypatch.setattr(
        server.notifications_db, "get_event",
        lambda _account_id, _event_id: {
            "status": "failed", "last_error_code": "provider_400",
        },
    )

    try:
        server.send_test_alert(object())
        raise AssertionError("expected provider failure")
    except server.HTTPException as exc:
        assert exc.status_code == 502
        assert "rejected" in exc.detail
        assert "queued for retry" not in exc.detail


def test_recovery_alert_is_cancelled_if_the_account_failed_again(monkeypatch):
    event = _event("gmail_recovered")
    assert notify.notifications_db.is_actionable(
        event, {"last_error": "Google disconnected — reconnect in Settings"}
    ) is False
    assert notify.notifications_db.is_actionable(event, {"last_error": None}) is True


def test_migration_contains_atomic_claim_and_transition_contracts():
    sql = (ROOT / "supabase" / "migrations" / "20260830000000_notification_outbox.sql").read_text()
    for name in (
        "queue_reply_review_with_alert",
        "mark_commitment_due_with_alert",
        "mark_review_send_uncertain_with_alert",
        "claim_follow_up_send_with_alert",
        "record_account_monitoring_state",
        "claim_notification_batch",
        "claim_notification_event",
        "finalize_notification_delivery",
    ):
        assert f"function public.{name}" in sql
    assert "for update skip locked" in sql.lower()
    assert "claim_token" in sql
    assert "gmail-disconnected:closed:" in sql
    assert "follow_up_send_uncertain" in sql
    assert "p_account_id is null or account_id = p_account_id" in sql
    assert "status in ('pending', 'retry', 'sending')" in sql
    assert "v_disconnect_status in ('delivered', 'sending')" in sql
    assert "service_role_execute" in sql
    assert "client_execute_revoked" in sql
    assert "revoke all on table public.notification_outbox" in sql.lower()


def test_outreach_page_handles_exact_and_resolved_deep_links():
    html = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")
    assert "params.get('review')" in html
    assert "params.get('commitment')" in html
    assert "Already resolved" in html
    assert "No action needed." in html
    start = html.index("window.addEventListener('popstate'")
    popstate = html[start:start + 400]
    assert "applyGuidedDeepLink()" in popstate
    assert "renderGuidedDesk(guidedDeskTab)" in popstate
