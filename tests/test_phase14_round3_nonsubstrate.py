"""Round-3 non-substrate repairs: ranking bounds, HTTP errors, shared auth, docs."""
import asyncio
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate

ROOT = Path(__file__).resolve().parents[1]
H = {'Authorization': 'Bearer key'}


def _store(tmp_path):
    sub = Substrate(str(tmp_path / 'w.db'), ScopeEngine(known_scopes={'a'}))
    return sub, ScopeContext(granted_scopes={'a'}, user_authored=True)


def _client(tmp_path, monkeypatch):
    from weft.server import app
    monkeypatch.setenv('WEFT_API_KEY', 'key')
    monkeypatch.setenv('WEFT_KNOWN_SCOPES', 's')
    monkeypatch.setenv('WEFT_DB_PATH', str(tmp_path / 'db'))
    return TestClient(app, raise_server_exceptions=False)


# ---- (2) ranking -----------------------------------------------------------

def test_agent_claim_confidence_cannot_displace_rank_one(tmp_path):
    from weft.retrieval import recall
    sub, ctx = _store(tmp_path)
    sub.put(Record('src', 'Source', 'a', {'text': 'seed'}, attribution='user'), ctx)
    sub.put(Record('c1', 'Claim', 'a', {'text': 'needle needle needle needle', 'confidence': 0.0},
                   epistemic_status='derived', source_ids=['src'], attribution='agent'), ctx)
    sub.put(Record('c2', 'Claim', 'a', {'text': 'needle', 'confidence': 1.0},
                   epistemic_status='derived', source_ids=['src'], attribution='agent'), ctx)
    base = sub.fts_candidates('"needle"', ctx, 10, scopes=['a'])
    assert base == ['c1', 'c2']
    items, receipt = recall(sub, 'needle', ctx, scope_hint='a', mode='lexical', limit=1)
    assert [i.record_id for i in items] == ['c1']
    assert receipt.selected == ['c1']


@pytest.mark.parametrize('position', [1, 2])
def test_boosts_cannot_displace_strongest_lexical_hit(tmp_path, monkeypatch, position):
    import weft.retrieval as retrieval
    import weft.temporal as temporal
    sub, ctx = _store(tmp_path)
    for i in range(4):
        sub.put(Record(f'r{i}', 'Source', 'a', {'text': 'needle ' * (8 - i)}, attribution='user'), ctx)
    base = sub.fts_candidates('"needle"', ctx, 10, scopes=['a'])
    target = base[position]
    monkeypatch.setattr(retrieval, 'entity_boost', lambda *a, **k: {target: 1e100})
    monkeypatch.setattr(temporal, 'temporal_boost', lambda *a, **k: {target: 1e100})
    items, _ = retrieval.recall(sub, 'needle', ctx, scope_hint='a', mode='lexical', limit=1)
    assert items[0].record_id == base[0]


# ---- (3) HTTP recall errors ------------------------------------------------

def _ingest(c):
    r = c.post('/mcp/ingest', json={'scope': 's', 'records': [
        {'record_id': 'one', 'payload': {'text': 'lighthouse'}}]}, headers=H)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize('path', ['/mcp/recall', '/mcp/call'])
def test_incomplete_index_is_503_with_recovery_pointer(tmp_path, monkeypatch, path):
    c = _client(tmp_path, monkeypatch)
    _ingest(c)
    with sqlite3.connect(tmp_path / 'db') as conn:
        conn.execute("DELETE FROM fts WHERE record_id='one'")
    args = {'scope': 's', 'query': 'lighthouse'}
    body = args if path.endswith('recall') else {'tool': 'recall', 'arguments': args}
    r = c.post(path, json=body, headers=H)
    assert r.status_code == 503, r.text
    assert 'rebuild-fts' in r.json()['detail']
    assert 'Traceback' not in r.text


@pytest.mark.parametrize('limit', [-1, 0, 10 ** 30, 101])
@pytest.mark.parametrize('path', ['/mcp/recall', '/mcp/call'])
def test_recall_limit_bounds_are_4xx(tmp_path, monkeypatch, path, limit):
    c = _client(tmp_path, monkeypatch)
    _ingest(c)
    args = {'scope': 's', 'query': 'lighthouse', 'limit': limit}
    body = args if path.endswith('recall') else {'tool': 'recall', 'arguments': args}
    assert 400 <= c.post(path, json=body, headers=H).status_code < 500


def test_valid_limit_still_recalls(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    _ingest(c)
    r = c.post('/mcp/recall', json={'scope': 's', 'query': 'lighthouse', 'limit': 100}, headers=H)
    assert r.status_code == 200 and r.json()['total'] == 1


# ---- (4) auth --------------------------------------------------------------

@pytest.mark.parametrize('module', ['weft.server', 'weft.mcp_server'])
@pytest.mark.parametrize('stype', ['websocket', 'bogus'])
def test_non_http_scopes_are_denied(monkeypatch, module, stype):
    monkeypatch.setenv('WEFT_API_KEY', 'key')
    called = []

    async def inner(scope, receive, send):
        called.append(scope)

    mw = importlib.import_module(module)._AuthMiddleware(inner)
    sent = []

    async def send(m): sent.append(m)
    async def receive(): return {'type': 'websocket.connect'}
    asyncio.run(mw({'type': stype, 'headers': [(b'authorization', b'Bearer key')]}, receive, send))
    assert called == []
    if stype == 'websocket':
        assert sent[0]['type'] == 'websocket.close'


def test_lifespan_scope_still_passes(monkeypatch):
    called = []

    async def inner(scope, receive, send): called.append(scope['type'])

    from weft.server import _AuthMiddleware
    asyncio.run(_AuthMiddleware(inner)({'type': 'lifespan'}, None, None))
    assert called == ['lifespan']


def test_auth_logic_has_single_implementation():
    for name in ('server', 'mcp_server', 'shadow'):
        text = (ROOT / 'weft' / f'{name}.py').read_text()
        assert 'compare_digest' not in text, name
    assert 'compare_digest' in (ROOT / 'weft' / 'server_common.py').read_text()


@pytest.mark.parametrize('auth,key,status', [
    (None, 'key', 401), ('Basic key', 'key', 401), ('Bearer nope', 'key', 401),
    ('Bearer key', '', 401), ('Bearer key', 'key', 200),
])
def test_bearer_behaviour_preserved(tmp_path, monkeypatch, auth, key, status):
    c = _client(tmp_path, monkeypatch)
    monkeypatch.setenv('WEFT_API_KEY', key)
    h = {'Authorization': auth} if auth else {}
    for path in ('/health', '/mcp/health'):
        if path == '/mcp/health':
            continue
    assert c.get('/health', headers=h).status_code == status


# ---- (5)/(6) docs and packaging -------------------------------------------

def test_docs_quickstart_uses_real_key_and_documents_restore_policy():
    text = (ROOT / 'docs' / 'turnkey-deployment.md').read_text()
    assert 'Bearer ***' not in text
    assert text.count('Bearer $WEFT_API_KEY') >= 2
    low = text.lower()
    assert 'active rule' in low and 'not' in low and 'restor' in low
    assert 'fresh approval' in low
    assert '503' in text and 'rebuild-fts' in text


def test_server_extra_has_no_sentence_transformers():
    import tomllib
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    extras = data['project']['optional-dependencies']
    assert not any('sentence-transformers' in d for d in extras['server'])
    assert any('sentence-transformers' in d for d in extras['local-recall'])
    assert {'fastapi', 'uvicorn', 'pydantic'} <= {
        d.split('>')[0].split('=')[0].split('<')[0] for d in extras['server']}
