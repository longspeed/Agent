"""Phase 1 contract tests for the Gmail-native promise ledger."""
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

import commitments_db  # noqa: E402
import server  # noqa: E402


def test_migration_preserves_records_and_defines_the_exact_ledger_states():
    sql = (ROOT / "supabase" / "migrations" / "20260904000000_promise_ledger.sql").read_text(encoding="utf-8").lower()

    assert "update public.commitments" in sql
    assert "set status = 'scheduled'" in sql
    assert "where status in ('confirmed', 'queued')" in sql
    drop_constraint = sql.index("drop constraint if exists commitments_status_check")
    rewrite_rows = sql.index("update public.commitments")
    add_constraint = sql.index("add constraint commitments_status_check")
    assert drop_constraint < rewrite_rows < add_constraint
    assert "delete from public.commitments" not in sql
    assert "create table if not exists public.commitment_events" in sql
    assert "transition_commitment_with_event" in sql
    assert "'detected', 'scheduled', 'due', 'completed', 'dismissed', 'expired'" in sql
    assert "unique (account_id, commitment_id, transition_key)" in sql
    assert "revoke all on function public.transition_commitment_with_event" in sql


def test_primary_surfaces_use_ledger_questions_and_hide_vanity_metrics():
    home = (ROOT / "static" / "index.html").read_text(encoding="utf-8").lower()
    outreach = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8").lower()

    for text in ("you owe", "they owe you", "due", "completed"):
        assert text in home
    assert "reply rate" not in home
    assert "meetings confirmed" not in home
    assert "deals won" not in home
    assert "promise-confirmation-receipt" in outreach
    assert "scheduled-you-list" in outreach
    assert "scheduled-them-list" in outreach
    assert "completed-commitments-list" in outreach
    assert 'id="revenue-leak-card"' not in outreach
    assert 'id="promise-precision-card"' not in outreach


def test_ledger_endpoint_normalizes_actor_and_attaches_account_scoped_audit(monkeypatch):
    rows = [{
        "id": 7,
        "thread_id": "gmail-thread-7",
        "actor": "operator",
        "status": "scheduled",
        "action_text": "send the deck",
    }]
    events = [{
        "commitment_id": 7,
        "account_id": "acct-1",
        "from_status": "detected",
        "to_status": "scheduled",
    }]
    monkeypatch.setattr(server.commitments_db, "list_ledger", lambda account_id: rows)
    monkeypatch.setattr(server.commitments_db, "list_events", lambda account_id, ids: events)
    monkeypatch.setattr(server, "_tracked_contact", lambda account_id, thread_id: {
        "name": "Ava", "email": "ava@example.com",
    })

    request = SimpleNamespace(state=SimpleNamespace(account_id="acct-1"))
    result = server.list_commitment_ledger(request)

    assert result[0]["actor"] == "you"
    assert result[0]["contact_name"] == "Ava"
    assert result[0]["gmail_url"].endswith("gmail-thread-7")
    assert result[0]["audit_events"] == events


def test_confirm_uses_one_atomic_transition_with_timezone_and_stable_key(monkeypatch):
    calls = []

    class Result:
        data = {"id": 9, "status": "scheduled", "timezone": "Asia/Bangkok"}

    class Rpc:
        def execute(self):
            return Result()

    class Client:
        def rpc(self, name, payload):
            calls.append((name, payload))
            return Rpc()

    monkeypatch.setattr(commitments_db, "_get_client", lambda: Client())
    first = commitments_db.confirm(
        "acct-1",
        9,
        due_at="2026-12-12T09:00:00+00:00",
        reminder_at="2026-12-11T09:00:00+00:00",
        timezone_name="Asia/Bangkok",
    )

    assert first["status"] == "scheduled"
    assert calls == [("transition_commitment_with_event", {
        "p_account_id": "acct-1",
        "p_commitment_id": 9,
        "p_to_status": "scheduled",
        "p_transition_key": "schedule:9",
        "p_due_at": "2026-12-12T09:00:00+00:00",
        "p_reminder_at": "2026-12-11T09:00:00+00:00",
        "p_timezone": "Asia/Bangkok",
        "p_dismissal_reason": None,
    })]
