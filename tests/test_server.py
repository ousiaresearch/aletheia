import json
import os

import pytest
from fastapi.testclient import TestClient

from aletheia.server import app
from aletheia.substrate import Substrate, Record
from aletheia.scope import ScopeContext, ScopeEngine

KNOWN = {"s"}


def _seed(sub, scope):
    sub._conn.execute(
        "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
        "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("src-1-s", "Source", scope, "stated", '{"text": "seed"}', 0.0, None, None, "[]", "{}", "document"),
    )
    sub._conn.commit()


def _env_with_known_scopes(monkeypatch, tmp_path):
    monkeypatch.setenv("ALETHEIA_API_KEY", "test-key")
    monkeypatch.setenv("ALETHEIA_KNOWN_SCOPES", "s")
    monkeypatch.setenv("HOME", str(tmp_path))
    os.makedirs(tmp_path / ".aletheia", exist_ok=True)


def test_health_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("ALETHEIA_API_KEY", "test-key")
    monkeypatch.setenv("ALETHEIA_KNOWN_SCOPES", "s")
    client = TestClient(app)
    resp = client.get("/health", headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_query_endpoint_returns_matches(tmp_path, monkeypatch):
    db_path = tmp_path / "aletheia.db"
    engine = ScopeEngine(known_scopes=KNOWN)
    sub = Substrate(db_path, engine)
    try:
        _seed(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "hello world"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
    finally:
        sub.close()
    _env_with_known_scopes(monkeypatch, tmp_path)
    os.replace(db_path, tmp_path / ".aletheia" / "aletheia.sqlite3")
    client = TestClient(app)
    resp = client.post("/query", json={"scope": "s", "query": "hello", "limit": 5}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["matches"]) == 1
    assert data["matches"][0]["record_id"] == "r1"


def test_query_endpoint_rejects_unknown_scope(tmp_path, monkeypatch):
    _env_with_known_scopes(monkeypatch, tmp_path)
    client = TestClient(app)
    resp = client.post("/query", json={"scope": "unknown", "query": "hello"}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "scope not permitted"


def test_get_record_returns_detail(tmp_path, monkeypatch):
    db_path = tmp_path / "aletheia.db"
    engine = ScopeEngine(known_scopes=KNOWN)
    sub = Substrate(db_path, engine)
    try:
        _seed(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "detail"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
    finally:
        sub.close()
    _env_with_known_scopes(monkeypatch, tmp_path)
    os.replace(db_path, tmp_path / ".aletheia" / "aletheia.sqlite3")
    client = TestClient(app)
    resp = client.get("/record/r1", params={"scope": "s"}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200
    assert resp.json()["payload"]["text"] == "detail"


def test_get_record_missing_returns_404(tmp_path, monkeypatch):
    db_path = tmp_path / "aletheia.db"
    engine = ScopeEngine(known_scopes=KNOWN)
    Substrate(db_path, engine).close()
    _env_with_known_scopes(monkeypatch, tmp_path)
    os.replace(db_path, tmp_path / ".aletheia" / "aletheia.sqlite3")
    client = TestClient(app)
    resp = client.get("/record/nonexistent", params={"scope": "s"}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 404


def test_export_endpoint_returns_payload(tmp_path, monkeypatch):
    db_path = tmp_path / "aletheia.db"
    engine = ScopeEngine(known_scopes=KNOWN)
    sub = Substrate(db_path, engine)
    try:
        _seed(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "v1"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
    finally:
        sub.close()
    _env_with_known_scopes(monkeypatch, tmp_path)
    os.replace(db_path, tmp_path / ".aletheia" / "aletheia.sqlite3")
    client = TestClient(app)
    resp = client.post("/export", json={"scope": "s"}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "s"
    assert len(data["records"]) == 2


def test_shadow_recall_returns_matches(tmp_path, monkeypatch):
    db_path = tmp_path / "aletheia.db"
    engine = ScopeEngine(known_scopes=KNOWN)
    sub = Substrate(db_path, engine)
    try:
        _seed(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "hello world"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
    finally:
        sub.close()
    _env_with_known_scopes(monkeypatch, tmp_path)
    os.replace(db_path, tmp_path / ".aletheia" / "aletheia.sqlite3")
    client = TestClient(app)
    resp = client.post("/shadow/recall", json={"scope": "s", "query": "hello"}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["actor"] == "shadow"
    assert data["user_authored"] is False
    assert len(data["matches"]) == 1
    assert data["matches"][0]["record_id"] == "r1"


def test_shadow_reason_returns_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "aletheia.db"
    engine = ScopeEngine(known_scopes=KNOWN)
    sub = Substrate(db_path, engine)
    try:
        _seed(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "lighthouse evidence"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
    finally:
        sub.close()
    _env_with_known_scopes(monkeypatch, tmp_path)
    os.replace(db_path, tmp_path / ".aletheia" / "aletheia.sqlite3")
    client = TestClient(app)
    resp = client.post("/shadow/reason", json={"scope": "s", "query": "lighthouse"}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["actor"] == "shadow"
    assert len(data["evidence"]) == 1
    assert data["evidence"][0]["record_id"] == "r1"


def test_shadow_compare_diffs_active_and_shadow(tmp_path, monkeypatch):
    db_path = tmp_path / "aletheia.db"
    engine = ScopeEngine(known_scopes=KNOWN)
    sub = Substrate(db_path, engine)
    try:
        _seed(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "visible"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
    finally:
        sub.close()
    _env_with_known_scopes(monkeypatch, tmp_path)
    os.replace(db_path, tmp_path / ".aletheia" / "aletheia.sqlite3")
    client = TestClient(app)
    resp = client.post("/shadow/compare", json={"scope": "s", "query": "visible"}, headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert "diff" in data
    assert data["diff"]["common"] == ["r1"]
    assert data["diff"]["added"] == []
    assert data["diff"]["removed"] == []
