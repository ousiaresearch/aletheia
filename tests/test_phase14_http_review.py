import pytest
from fastapi.testclient import TestClient
from aletheia.server import app

@pytest.mark.parametrize('path', ['/mcp/ingest', '/mcp/call'])
@pytest.mark.parametrize('field', ['payload', 'provenance'])
@pytest.mark.parametrize('value', [None, 'bad', []])
def test_bad_record_is_rejected_atomically(tmp_path, monkeypatch, path, field, value):
    monkeypatch.setenv('ALETHEIA_API_KEY', 'key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES', 's')
    monkeypatch.setenv('ALETHEIA_DB_PATH', str(tmp_path / 'db'))
    client = TestClient(app, raise_server_exceptions=False)
    headers = {'Authorization': 'Bearer key'}
    args = {'scope': 's', 'records': [{'record_id':'good','payload':{'text':'ok'}}, {'record_id':'bad',field:value}]}
    body = args if path.endswith('ingest') else {'tool':'ingest','arguments':args}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code in (400, 403, 422)
    assert client.post('/mcp/status', json={'scope':'s'}, headers=headers).json()['record_count'] == 0

@pytest.mark.parametrize('path', ['/mcp/ingest', '/mcp/call'])
def test_duplicate_batch_returns_generic_client_error_and_rolls_back(tmp_path, monkeypatch, path):
    monkeypatch.setenv('ALETHEIA_API_KEY', 'key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES', 's')
    monkeypatch.setenv('ALETHEIA_DB_PATH', str(tmp_path / 'db'))
    client = TestClient(app, raise_server_exceptions=False)
    headers = {'Authorization':'Bearer key'}
    args = {'scope':'s','records':[{'record_id':'same'}, {'record_id':'same'}]}
    body = args if path.endswith('ingest') else {'tool':'ingest','arguments':args}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 403
    assert response.json()['detail'] == 'write not permitted'
    assert client.post('/mcp/status', json={'scope':'s'}, headers=headers).json()['record_count'] == 0

@pytest.mark.parametrize('args', [{'scope':['s']}, {'scope':'s','records':[1,2]}, {'scope':'s','records':'x'}, {'scope':'s','query':'q','limit':'abc'}])
def test_malformed_router_arguments_are_client_errors(tmp_path, monkeypatch, args):
    monkeypatch.setenv('ALETHEIA_API_KEY','key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES','s')
    monkeypatch.setenv('ALETHEIA_DB_PATH',str(tmp_path/'db'))
    tool = 'recall' if 'limit' in args else 'ingest'
    response = TestClient(app, raise_server_exceptions=False).post('/mcp/call', json={'tool':tool,'arguments':args}, headers={'Authorization':'Bearer key'})
    assert 400 <= response.status_code < 500

@pytest.mark.parametrize('path', ['/mcp/recall', '/mcp/call'])
def test_recall_after_ingest_with_available_embedder(tmp_path, monkeypatch, path):
    import aletheia.mcp_server as m
    monkeypatch.setenv('ALETHEIA_API_KEY','key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES','s')
    monkeypatch.setenv('ALETHEIA_DB_PATH',str(tmp_path/'db'))
    # Round 3: HTTP recall is lexical-only; the dead embedder hook was removed.
    assert not hasattr(m, '_load_embedder')
    c = TestClient(app, raise_server_exceptions=False)
    h = {'Authorization':'Bearer key'}
    assert c.post('/mcp/ingest', json={'scope':'s','records':[{'record_id':'one','payload':{'text':'lighthouse'}}]}, headers=h).status_code == 200
    args = {'scope':'s','query':'lighthouse'}
    body = args if path.endswith('recall') else {'tool':'recall','arguments':args}
    r = c.post(path, json=body, headers=h)
    assert r.status_code == 200, r.text
    assert [x['record_id'] for x in r.json()['results']] == ['one']

def test_shadow_active_context_has_no_user_authority(monkeypatch):
    from aletheia.shadow import _ctx_for
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES','s')
    _, ctx = _ctx_for('s')
    assert not ctx.user_authored
    assert ctx.actor != 'local-user'

@pytest.mark.parametrize('module', ['aletheia.server','aletheia.mcp_server'])
def test_invalid_utf8_auth_is_unauthorized(monkeypatch, module):
    import asyncio, importlib
    monkeypatch.setenv('ALETHEIA_API_KEY','\ufffd\ufffd')
    middleware = importlib.import_module(module)._AuthMiddleware(None)
    messages = []
    async def receive(): return {'type':'http.request','body':b''}
    async def send(msg): messages.append(msg)
    asyncio.run(middleware({'type':'http','headers':[(b'authorization',b'Bearer \xff\xfe')]}, receive, send))
    assert messages[0]['status'] == 401

def test_compose_binds_loopback_by_default():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / 'docker-compose.yml').read_text()
    assert '"127.0.0.1:8765:8765"' in text

@pytest.mark.parametrize('path', ['/mcp/ingest','/mcp/call'])
def test_non_string_text_cannot_poison_query(tmp_path, monkeypatch, path):
    monkeypatch.setenv('ALETHEIA_API_KEY','key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES','s')
    monkeypatch.setenv('ALETHEIA_DB_PATH',str(tmp_path/'db'))
    c=TestClient(app,raise_server_exceptions=False); h={'Authorization':'Bearer key'}
    args={'scope':'s','records':[{'record_id':'bad','payload':{'text':['bad']}}]}
    body=args if path.endswith('ingest') else {'tool':'ingest','arguments':args}
    assert 400 <= c.post(path,json=body,headers=h).status_code < 500
    assert c.post('/query',json={'scope':'s','query':'a'},headers=h).status_code == 200

@pytest.mark.parametrize('path', ['/mcp/recall','/mcp/call'])
def test_recall_does_not_bypass_stale_fts(tmp_path, monkeypatch, path):
    import sqlite3
    monkeypatch.setenv('ALETHEIA_API_KEY','key')
    monkeypatch.setenv('ALETHEIA_KNOWN_SCOPES','s')
    db=tmp_path/'db'; monkeypatch.setenv('ALETHEIA_DB_PATH',str(db))
    c=TestClient(app,raise_server_exceptions=False); h={'Authorization':'Bearer key'}
    assert c.post('/mcp/ingest',json={'scope':'s','records':[{'record_id':'one','payload':{'text':'lighthouse'}}]},headers=h).status_code==200
    args={'scope':'s','query':'lighthouse'}
    body=args if path.endswith('recall') else {'tool':'recall','arguments':args}
    assert c.post(path,json=body,headers=h).json()['results'][0]['record_id']=='one'
    with sqlite3.connect(db) as conn: conn.execute('DELETE FROM fts WHERE record_id=?',('one',))
    assert c.post(path,json=body,headers=h).status_code != 200
