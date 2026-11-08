"""Phase 14 round 3: shared ASGI auth, recall error contract, packaging and docs."""
import asyncio
import re
import sqlite3
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aletheia import server_common
from aletheia.mcp_server import app as tool_app
from aletheia.server import app as full_app
from aletheia.shadow import _append  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
KEY = "round3-key"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALETHEIA_API_KEY", KEY)
    monkeypatch.setenv("ALETHEIA_KNOWN_SCOPES", "a,b")
    monkeypatch.setenv("ALETHEIA_DB_PATH", str(tmp_path / "s.db"))
    return tmp_path / "s.db"


HDR = {"Authorization": f"Bearer {KEY}"}


def _run_scope(app, scope):
    sent, called = [], []

    async def inner(scope_, receive, send):
        called.append(scope_["type"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        sent.append(msg)

    # Wrap the middleware stack directly around a recording app.
    mw = server_common.AuthMiddleware(inner)
    asyncio.run(mw(scope, receive, send))
    return sent, called


def test_websocket_scope_is_rejected_not_passed_through(env):
    sent, called = _run_scope(full_app, {"type": "websocket", "path": "/x", "headers": []})
    assert called == []
    assert sent and sent[0]["type"] == "websocket.close"


def test_websocket_with_valid_bearer_still_rejected(env):
    scope = {"type": "websocket", "path": "/x", "headers": [(b"authorization", f"Bearer {KEY}".encode())]}
    _, called = _run_scope(full_app, scope)
    assert called == []


def test_lifespan_scope_passes(env):
    _, called = _run_scope(full_app, {"type": "lifespan"})
    assert called == ["lifespan"]


def test_unknown_scope_type_rejected(env):
    _, called = _run_scope(full_app, {"type": "weird", "headers": []})
    assert called == []


def test_apps_use_shared_middleware_and_compare_digest():
    for app in (full_app, tool_app):
        assert any(m.cls is server_common.AuthMiddleware for m in app.user_middleware)
    for name in ("server.py", "mcp_server.py", "shadow.py"):
        src = (ROOT / "aletheia" / name).read_text()
        assert "compare_digest" not in src and "startswith(\"Bearer" not in src
    assert "compare_digest" in (ROOT / "aletheia" / "server_common.py").read_text()


def test_auth_401_before_validation_and_invalid_utf8(env):
    c = TestClient(full_app, raise_server_exceptions=False)
    assert c.post("/mcp/recall", json={"bad": 1}).status_code == 401
    assert c.post("/mcp/recall", json={"scope": "a", "query": "x", "limit": -1}).status_code == 401
    r = c.post("/mcp/recall", json={}, headers={"Authorization": b"Bearer \xff\xfe"})
    assert r.status_code == 401
    assert c.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/health", headers=HDR).status_code == 200


def test_unset_key_fails_closed(env, monkeypatch):
    monkeypatch.delenv("ALETHEIA_API_KEY")
    assert TestClient(full_app).get("/health", headers=HDR).status_code == 401


def _ingest(c, scope, rid, text):
    r = c.post("/mcp/ingest", headers=HDR, json={"scope": scope, "records": [
        {"record_id": rid, "payload": {"text": text}}]})
    assert r.status_code == 200, r.text


def test_stale_fts_index_is_deliberate_503(env):
    c = TestClient(full_app, raise_server_exceptions=False)
    _ingest(c, "a", "r1", "alpha apple")
    db = sqlite3.connect(env)
    db.execute("DELETE FROM fts WHERE record_id='r1'")
    db.commit()
    db.close()
    r = c.post("/mcp/recall", headers=HDR, json={"scope": "a", "query": "apple"})
    assert r.status_code == 503
    assert "aletheia --db <path> rebuild-fts" in r.text
    assert "Traceback" not in r.text and str(env) not in r.text
    # shared by the custom router
    r = c.post("/mcp/call", headers=HDR, json={"tool": "recall", "arguments": {"scope": "a", "query": "apple"}})
    assert r.status_code == 503


@pytest.mark.parametrize("limit", [-1, 0, 101, 10**30, "x", 1.5])
def test_bad_limit_is_4xx(env, limit):
    c = TestClient(full_app, raise_server_exceptions=False)
    r = c.post("/mcp/recall", headers=HDR, json={"scope": "a", "query": "q", "limit": limit})
    assert 400 <= r.status_code < 500


def test_server_extra_has_no_sentence_transformers_and_tests_have_httpx():
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    server = " ".join(extras["server"]).lower()
    assert "sentence-transformers" not in server and "torch" not in server
    for need in ("fastapi", "uvicorn", "pydantic"):
        assert need in server
    assert any("httpx" in d for k, v in extras.items() if k in ("test", "dev") for d in v)


def test_dead_embedder_loader_removed():
    import aletheia.mcp_server as m
    assert not hasattr(m, "_load_embedder")


def test_deployment_doc_covers_r3_10():
    doc = (ROOT / "docs" / "turnkey-deployment.md").read_text()
    low = doc.lower()
    assert "$ALETHEIA_API_KEY" in doc and "substitute" in low
    assert not re.search(r"Bearer \*", doc)
    assert "not fully restorable" in low and "fresh approval" in low
    assert "structured" in low and "affirmative" in low
    assert "decision" in low and "commitment" in low
    assert "lexical-only" in low and "scal" in low
    assert "not the mcp protocol" in low or "not an mcp protocol" in low
    assert "unverified" in low or "not been verified" in low
