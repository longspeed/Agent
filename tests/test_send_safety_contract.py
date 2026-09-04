import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "outreach-agent"))
sys.path.insert(0, str(ROOT))

import bounces
import drafts_db
import gmail
import schema_contract
import send_outreach
import sheets
import suppressions_db
import tracked_threads


ACCOUNT = {
    "id": "11111111-1111-1111-1111-111111111111",
    "follow_up_delay_days": 3,
    "plan": "inbox",
}
DRAFT = {
    "id": 41,
    "row_index": 2,
    "email": "person@example.com",
    "subject": "Hello",
    "body": "Checking in",
}


def row(email="person@example.com", status="", confidence="verified"):
    values = [""] * 9
    values[sheets.COL_EMAIL] = email
    values[sheets.COL_STATUS] = status
    values[sheets.COL_EMAIL_CONFIDENCE] = confidence
    return values


def healthy_send(monkeypatch, rows=None):
    monkeypatch.setattr(drafts_db, "require_send_log", lambda: None)
    monkeypatch.setattr(bounces, "assert_sendable", lambda account: None)
    monkeypatch.setattr(send_outreach, "assert_domain_safe", lambda account: None)
    monkeypatch.setattr(sheets, "get_all_rows", lambda account: rows or [(2, row())])
    monkeypatch.setattr(suppressions_db, "is_suppressed", lambda *args: False)
    monkeypatch.setattr(drafts_db, "has_first_touch_conflict", lambda *args, **kwargs: False)
    monkeypatch.setattr(drafts_db, "claim_send", lambda *args: True)
    monkeypatch.setattr(drafts_db, "release_send_claim", lambda *args: True)
    monkeypatch.setattr(send_outreach.auth, "unsubscribe_url", lambda *args: "https://example.test/u")
    monkeypatch.setattr(send_outreach.send_capacity_db, "reserve", lambda *args: True)
    monkeypatch.setattr(send_outreach.send_capacity_db, "complete", lambda *args: True)
    monkeypatch.setattr(drafts_db, "mark_sent", lambda *args, **kwargs: True)
    monkeypatch.setattr(tracked_threads, "upsert_thread", lambda *args, **kwargs: None)
    monkeypatch.setattr(send_outreach, "mark_row_sent", lambda *args, **kwargs: None)


@pytest.mark.parametrize(
    "case,expected_retryable",
    [
        ("deleted_row", False),
        ("changed_recipient", False),
        ("completed_row", False),
        ("unverified", False),
        ("suppressed", False),
        ("duplicate_draft", False),
        ("duplicate_completed_sheet_row", False),
        ("bounce_pause", True),
        ("unsafe_domain", True),
        ("sheet_unavailable", True),
    ],
)
def test_every_preflight_failure_stops_before_gmail(monkeypatch, case, expected_retryable):
    rows = [(2, row())]
    if case == "deleted_row":
        rows = [(3, row("other@example.com"))]
    elif case == "changed_recipient":
        rows = [(2, row("changed@example.com"))]
    elif case == "completed_row":
        rows = [(2, row(status="Sent"))]
    elif case == "unverified":
        rows = [(2, row(confidence="unverified"))]
    elif case == "duplicate_completed_sheet_row":
        rows.append((3, row(status="Replied")))

    healthy_send(monkeypatch, rows)
    discarded = []
    gmail_calls = []
    monkeypatch.setattr(drafts_db, "discard", lambda account_id, draft_id: discarded.append(draft_id) or True)
    monkeypatch.setattr(gmail, "send_email", lambda *args, **kwargs: gmail_calls.append(args) or "thread")
    if case == "suppressed":
        monkeypatch.setattr(suppressions_db, "is_suppressed", lambda *args: True)
    elif case == "duplicate_draft":
        monkeypatch.setattr(drafts_db, "has_first_touch_conflict", lambda *args, **kwargs: True)
    elif case == "bounce_pause":
        monkeypatch.setattr(bounces, "assert_sendable", lambda account: (_ for _ in ()).throw(RuntimeError("bounce pause")))
    elif case == "unsafe_domain":
        monkeypatch.setattr(send_outreach, "assert_domain_safe", lambda account: (_ for _ in ()).throw(RuntimeError("unsafe domain")))
    elif case == "sheet_unavailable":
        monkeypatch.setattr(sheets, "get_all_rows", lambda account: (_ for _ in ()).throw(RuntimeError("sheet offline")))

    with pytest.raises(send_outreach.SendFailure) as raised:
        send_outreach.send_prepared_draft(ACCOUNT, dict(DRAFT))

    assert raised.value.retryable is expected_retryable
    assert raised.value.code == ("send_temporarily_blocked" if expected_retryable else "send_blocked")
    assert gmail_calls == []
    assert bool(discarded) is (not expected_retryable)


def test_reservation_infrastructure_failure_releases_only_the_pre_gmail_claim(monkeypatch):
    healthy_send(monkeypatch)
    released_claims = []
    gmail_calls = []
    monkeypatch.setattr(drafts_db, "release_send_claim", lambda *args: released_claims.append(args) or True)
    monkeypatch.setattr(
        send_outreach.send_capacity_db,
        "reserve",
        lambda *args: (_ for _ in ()).throw(RuntimeError("rpc unavailable")),
    )
    monkeypatch.setattr(gmail, "send_email", lambda *args, **kwargs: gmail_calls.append(args) or "thread")

    with pytest.raises(send_outreach.CapacityUnavailable) as raised:
        send_outreach.send_prepared_draft(ACCOUNT, dict(DRAFT))
    assert raised.value.code == "capacity_unavailable"
    assert raised.value.retryable is True
    assert released_claims and gmail_calls == []


def test_ambiguous_gmail_result_keeps_claim_and_reservation_locked(monkeypatch):
    healthy_send(monkeypatch)
    released_claims = []
    released_capacity = []
    monkeypatch.setattr(drafts_db, "release_send_claim", lambda *args: released_claims.append(args) or True)
    monkeypatch.setattr(send_outreach.send_capacity_db, "release", lambda *args: released_capacity.append(args) or True)
    monkeypatch.setattr(gmail, "send_email", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("lost response")))

    with pytest.raises(send_outreach.DeliveryUncertain) as raised:
        send_outreach.send_prepared_draft(ACCOUNT, dict(DRAFT))
    assert raised.value.code == "send_uncertain" and raised.value.retryable is False
    assert released_claims == [] and released_capacity == []


def test_post_gmail_recording_failure_is_uncertain_and_keeps_fences(monkeypatch):
    healthy_send(monkeypatch)
    released_claims = []
    released_capacity = []
    monkeypatch.setattr(gmail, "send_email", lambda *args, **kwargs: "gmail-thread")
    monkeypatch.setattr(
        drafts_db, "mark_sent",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database offline")),
    )
    monkeypatch.setattr(drafts_db, "release_send_claim", lambda *args: released_claims.append(args) or True)
    monkeypatch.setattr(send_outreach.send_capacity_db, "release", lambda *args: released_capacity.append(args) or True)

    with pytest.raises(send_outreach.DeliveryUncertain) as raised:
        send_outreach.send_prepared_draft(ACCOUNT, dict(DRAFT))

    assert raised.value.code == "send_uncertain"
    assert raised.value.retryable is False
    assert released_claims == [] and released_capacity == []


def test_atomic_contract_requires_unique_indexes_and_protects_itself():
    sql = (ROOT / "supabase" / "migrations" / "20260901000000_atomic_send_contract.sql").read_text()
    assert sql.count("i.indisunique") >= 2
    assert sql.count("to_regprocedure('public.atomic_send_contract()')") >= 3
    assert "grant execute on function public.atomic_send_contract() to service_role" in sql.lower()


def test_two_tabs_can_cross_preflight_but_only_one_claim_calls_gmail(monkeypatch):
    healthy_send(monkeypatch)
    claims = iter((True, False))
    gmail_calls = []
    monkeypatch.setattr(drafts_db, "claim_send", lambda *args: next(claims))
    monkeypatch.setattr(gmail, "send_email", lambda *args, **kwargs: gmail_calls.append(args) or "thread")

    send_outreach.send_prepared_draft(ACCOUNT, dict(DRAFT))
    with pytest.raises(send_outreach.SendFailure) as raised:
        send_outreach.send_prepared_draft(ACCOUNT, dict(DRAFT))
    assert raised.value.code == "send_blocked"
    assert len(gmail_calls) == 1


def test_campaign_actionability_is_pending_normal_reply_only(monkeypatch):
    import server

    account = dict(ACCOUNT, google_sheet_id="")
    tracked = [
        {"id": index, "thread_id": f"thread-{index}", "email": f"p{index}@example.com", "status": "active"}
        for index in range(1, 6)
    ]
    reviews = [
        {"thread_id": "thread-1", "status": "pending", "kind": "reply"},
        {"thread_id": "thread-2", "status": "pending", "kind": "follow_up"},
        {"thread_id": "thread-3", "status": "send_uncertain", "kind": "reply"},
        {"thread_id": "thread-4", "status": "flagged", "kind": "reply"},
        {"thread_id": "thread-5", "status": "dismissed", "kind": "reply"},
    ]
    monkeypatch.setattr(server, "_account", lambda request: account)
    monkeypatch.setattr(server.reviews_db, "list_pending_reviews", lambda account_id: reviews)
    monkeypatch.setattr(server.reviews_db, "latest_reply_for_thread", lambda *args: None)
    monkeypatch.setattr(server.tracked_threads, "list_active", lambda account_id: tracked)

    result = server.list_campaigns(SimpleNamespace(state=SimpleNamespace(account_id=ACCOUNT["id"])))
    by_id = {item["trackedId"]: item["replyActionable"] for item in result}
    assert by_id == {1: True, 2: False, 3: False, 4: False, 5: False}


def healthy_contract_snapshot():
    return {
        "version": schema_contract.BASELINE_MIGRATION,
        "columns": {f"{item.table}.{item.column}": True for item in schema_contract.REQUIREMENTS},
        "nullable": {"reviews.row_index": True},
        "indexes": {key: True for key in schema_contract.INDEX_REQUIREMENTS},
        "rls": {key: True for key in schema_contract.RLS_TABLES},
        "constraints": {key: True for key in schema_contract.CONSTRAINT_REQUIREMENTS},
    }


def test_atomic_contract_missing_invariant_is_a_startup_warning(monkeypatch):
    monkeypatch.setattr(schema_contract, "_contract_snapshot", healthy_contract_snapshot)
    monkeypatch.setattr(schema_contract, "_outbox_snapshot", lambda: {
        "version": schema_contract.OUTBOX_MIGRATION,
        **{key: True for key in (
            "table", "rls", "ready_index", "dedupe_constraint", "status_constraint",
            "event_constraint", "source_constraint", "rpcs", "service_role_execute",
            "client_execute_revoked",
        )},
    })
    atomic = {
        "version": schema_contract.ATOMIC_SEND_MIGRATION,
        **{key: True for key in (
            "table", "rls", "client_table_access_revoked", "operation_unique",
            "status_constraint", "active_index", "active_row_index", "recipient_index",
            "reservation_rpc", "duplicate_check_rpc", "stripe_rpc", "service_role_execute",
            "client_execute_revoked",
        )},
    }
    atomic["recipient_index"] = False
    monkeypatch.setattr(schema_contract, "_atomic_send_snapshot", lambda: atomic)
    warnings = schema_contract.check()
    assert any("recipient_index" in warning and schema_contract.ATOMIC_SEND_MIGRATION in warning for warning in warnings)


def test_atomic_contract_rpc_unreachable_is_not_reported_as_many_missing_invariants(monkeypatch):
    monkeypatch.setattr(schema_contract, "_contract_snapshot", healthy_contract_snapshot)
    monkeypatch.setattr(schema_contract, "_outbox_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("outbox offline")))
    monkeypatch.setattr(schema_contract, "_atomic_send_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("contract offline")))
    warnings = schema_contract.check()
    assert sum("atomic send" in warning.lower() for warning in warnings) == 1
