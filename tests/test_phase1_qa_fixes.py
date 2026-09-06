"""Regressions for the interactive ledger QA findings."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'outreach-agent'))
sys.path.insert(0, str(ROOT))
for key, value in {'OPENROUTER_API_KEY': 'test', 'APP_SECRET_KEY': 'test-secret',
                   'SUPABASE_URL': 'http://localhost', 'SUPABASE_SECRET_KEY': 'test'}.items():
    os.environ.setdefault(key, value)
import accounts_db
import server


def query_client(monkeypatch, outcomes):
    query = Mock()
    query.table.return_value = query
    query.select.return_value = query
    query.eq.return_value = query
    query.execute.side_effect = outcomes
    monkeypatch.setattr(accounts_db, '_get_client', lambda: query)
    monkeypatch.setattr(accounts_db.time, 'sleep', lambda _: None)
    return query


def test_transient_account_read_retries_only_the_scoped_read(monkeypatch):
    query = query_client(monkeypatch, [httpx.ReadError('socket'), SimpleNamespace(data=[{'id': 'a'}])])
    assert accounts_db.get_account('a') == {'id': 'a'}
    assert query.execute.call_count == 2
    assert all(call.args == ('id', 'a') for call in query.eq.call_args_list)
    query.update.assert_not_called()
    query.insert.assert_not_called()


def test_missing_account_does_not_retry(monkeypatch):
    query = query_client(monkeypatch, [SimpleNamespace(data=[])])
    assert accounts_db.get_account('a') is None
    assert query.execute.call_count == 1


def test_non_transport_failures_are_not_retried(monkeypatch):
    query = query_client(monkeypatch, [ValueError('bad query')])
    with pytest.raises(ValueError):
        accounts_db.get_account('a')
    assert query.execute.call_count == 1


def test_exhausted_account_read_returns_private_retryable_503(monkeypatch):
    query = query_client(monkeypatch, [httpx.ReadError('private database hostname')] * 2)
    monkeypatch.setattr(server.auth, 'verify_session_token', lambda _: 'a')
    response = TestClient(server.app).get('/api/me', cookies={'session': 'test'})
    assert response.status_code == 503
    assert response.json()['code'] == 'account_store_unavailable'
    assert response.json()['retryable'] is True
    assert response.headers['retry-after'] == '5'
    assert 'private database hostname' not in response.text
    assert query.execute.call_count == 2


def test_public_examples_and_guide_contract():
    mockup = (ROOT / 'site/src/components/mockup.tsx').read_text(encoding='utf-8')
    assert 'pessiskibi' not in mockup and 'longspeed2828' not in mockup
    assert '@example.com' in mockup
    guide = (ROOT / 'app/src/GettingStartedPage.tsx').read_text(encoding='utf-8')
    assert 'Will be emailed on the next run' not in guide
    assert 'Confirming a promise never sends email' in guide
