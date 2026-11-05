import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weft.server import app


ROOT = Path(__file__).resolve().parents[1]


def test_docker_context_excludes_large_artifacts_and_nested_python_cache():
    patterns = set((ROOT / '.dockerignore').read_text().splitlines())
    assert {'models', 'dist', 'build', 'eval', '.pytest_cache', ':memory:',
            '**/__pycache__', '**/*.pyc'} <= patterns


def test_dockerfile_does_not_claim_source_independent_install_cache():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'leverages Docker layer cache' not in dockerfile


def test_documented_curl_examples_use_configured_key():
    document = (ROOT / 'docs/turnkey-deployment.md').read_text()
    curl_lines = [line for line in document.splitlines() if line.startswith('curl ')]
    assert len(curl_lines) >= 2
    assert all('Authorization: Bearer $WEFT_API_KEY' in line for line in curl_lines)


@pytest.mark.parametrize('path', ['/mcp/ingest', '/mcp/call'])
@pytest.mark.parametrize('bad_record', [
    {'record_id': 'bad', 'payload': {'nested': [float('nan')]}},
    {'record_id': 'bad', 'provenance': {'nested': [float('inf')]}},
    {'record_id': 'bad', 'payload': {'nested': {'key': '\ud800'}}},
    {'record_id': 'bad', 'payload': {'\udfff': 'bad'}},
    {'record_id': '\ud800', 'payload': {'text': 'bad'}},
])
def test_invalid_json_values_rejected_atomically_without_poisoning_export(tmp_path, monkeypatch, path, bad_record):
    monkeypatch.setenv('WEFT_API_KEY', 'key')
    monkeypatch.setenv('WEFT_KNOWN_SCOPES', 's')
    monkeypatch.setenv('WEFT_DB_PATH', str(tmp_path / 'db'))
    client = TestClient(app, raise_server_exceptions=False)
    headers = {'Authorization': 'Bearer key', 'Content-Type': 'application/json'}
    good = {'record_id': 'good', 'payload': {'text': 'ok'}}
    assert client.post('/mcp/ingest', json={'scope': 's', 'records': [good]}, headers=headers).status_code == 200
    args = {'scope': 's', 'records': [{'record_id': 'rolled-back'}, bad_record]}
    body = args if path == '/mcp/ingest' else {'tool': 'ingest', 'arguments': args}
    response = client.post(path, content=json.dumps(body), headers=headers)
    assert 400 <= response.status_code < 500, response.text
    exported = client.post('/export', json={'scope': 's'}, headers=headers)
    assert exported.status_code == 200, exported.text
    assert [r['record_id'] for r in exported.json()['records']] == ['good']
    assert client.get('/record/good?scope=s', headers=headers).status_code == 200
    assert client.post('/mcp/call', json={'tool': 'export', 'arguments': {'scope': 's'}}, headers=headers).status_code == 200
