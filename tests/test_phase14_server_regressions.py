"""Behavioural regressions for the deployed HTTP service."""
import pytest
from fastapi.testclient import TestClient
from aletheia.mcp_server import app as tool_app
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate
from aletheia.server import app


def test_deployed_app_serves_scoped_custom_status(tmp_path, monkeypatch):
    monkeypatch.setenv('ALETHEIA_API_KEY', 'test-key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES', 'coast')
    monkeypatch.setenv('ALETHEIA_DB_PATH', str(tmp_path / 'store.db'))
    client = TestClient(app)
    assert client.post('/mcp/status', json={'scope': 'coast'}).status_code == 401
    headers = {'Authorization': 'Bearer test-key'}
    assert client.post('/mcp/status', json={'scope': 'inland'}, headers=headers).status_code == 403
    response = client.post('/mcp/status', json={'scope': 'coast'}, headers=headers)
    assert response.status_code == 200
    assert response.json()['record_count'] == 0


@pytest.mark.parametrize('path', ['/mcp/ingest', '/mcp/call'])
def test_ingest_writes_agent_source_without_user_authority(tmp_path, monkeypatch, path):
    monkeypatch.setenv('ALETHEIA_API_KEY', 'prefix-very-secret')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES', 'coast')
    db = tmp_path / 'store.db'
    monkeypatch.setenv('ALETHEIA_DB_PATH', str(db))
    args = {'scope': 'coast', 'records': [{'record_id': 'src', 'record_type': 'Source',
            'epistemic_status': 'stated', 'payload': {'text': 'hello'}}]}
    body = args if path.endswith('ingest') else {'tool': 'ingest', 'arguments': args}
    response = TestClient(tool_app, raise_server_exceptions=False).post(path, json=body, headers={'Authorization': 'Bearer prefix-very-secret'})
    assert response.status_code == 200, response.text
    sub = Substrate(db, ScopeEngine(known_scopes={'coast'}))
    try:
        rec = sub.get('src', ScopeContext(granted_scopes=frozenset({'coast'})))
        assert rec.attribution == 'agent'
    finally:
        sub.close()


@pytest.mark.parametrize('module', ['aletheia.server', 'aletheia.mcp_server'])
def test_http_context_has_no_key_material_or_user_attribution(monkeypatch, module):
    import importlib
    monkeypatch.setenv('ALETHEIA_API_KEY', 'prefix-very-secret')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES', 'coast')
    _, ctx = importlib.import_module(module)._ctx_for('coast')
    assert 'prefix-' not in repr(ctx)
    assert not ctx.user_authored


@pytest.mark.parametrize('path', ['/mcp/ingest', '/mcp/call'])
def test_invalid_ingest_is_generic_refusal_and_batch_is_atomic(tmp_path, monkeypatch, path):
    monkeypatch.setenv('ALETHEIA_API_KEY', 'test-key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES', 'coast')
    monkeypatch.setenv('ALETHEIA_DB_PATH', str(tmp_path / 'store.db'))
    args = {'scope': 'coast', 'records': [
        {'record_id': 'src', 'record_type': 'Source', 'epistemic_status': 'stated', 'payload': {'text': 'hello'}},
        {'record_id': 'decision', 'record_type': 'Decision', 'epistemic_status': 'stated', 'attribution': 'user', 'payload': {'text': 'not user authored'}}]}
    body = args if path.endswith('ingest') else {'tool': 'ingest', 'arguments': args}
    client = TestClient(tool_app, raise_server_exceptions=False)
    headers = {'Authorization': 'Bearer test-key'}
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 403
    status = client.post('/mcp/status', json={'scope': 'coast'}, headers=headers)
    assert status.json()['record_count'] == 0
