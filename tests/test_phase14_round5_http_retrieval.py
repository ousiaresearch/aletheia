"""Phase 14 round 5: true-BM25 tie logic, full known-scope engines, unified auth bytes, docs."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from aletheia import server_common
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate

ROOT = Path(__file__).resolve().parents[1]
KEY = "round5-key"
HDR = {"Authorization": f"Bearer {KEY}"}


def _store(tmp_path):
    sub = Substrate(str(tmp_path / "w.db"), ScopeEngine(known_scopes={"a", "b"}))
    ctx = ScopeContext(granted_scopes={"a", "b"}, user_authored=True)
    return sub, ctx


# ---- (a) ranking -----------------------------------------------------------

def test_stemmed_bm25_difference_is_not_a_tie_for_temporal_payload(tmp_path):
    from aletheia.retrieval import recall
    sub, ctx = _store(tmp_path)
    # Raw-token profiles are identical (no exact 'harbor'), but porter-stemmed BM25 differs.
    sub.put(Record("strong", "Source", "a", {"text": "harbors harbors"}, attribution="user"), ctx)
    sub.put(Record("weak", "Source", "a", {"text": "harbors piers",
                                           "is_temporal": True,
                                           "temporal_dates": ["1", "2", "3", "4", "5"]},
                   attribution="user"), ctx)
    assert sub.fts_candidates('"harbor"', ctx, 10, scopes=["a"])[0] == "strong"
    items, _ = recall(sub, "when harbor", ctx, scope_hint="a", mode="lexical", limit=1)
    assert [i.record_id for i in items] == ["strong"]


def test_stemmed_bm25_difference_is_not_a_tie_for_claim_confidence(tmp_path):
    from aletheia.retrieval import recall
    sub, ctx = _store(tmp_path)
    sub.put(Record("s1", "Source", "a", {"text": "origin"}, attribution="user"), ctx)
    sub.put(Record("strong", "Claim", "a", {"text": "harbors harbors", "confidence": 0.1},
                   epistemic_status="derived", source_ids=["s1"], attribution="agent"), ctx)
    sub.put(Record("weak", "Claim", "a", {"text": "harbors piers", "confidence": 1.0},
                   epistemic_status="derived", source_ids=["s1"], attribution="agent"), ctx)
    items, _ = recall(sub, "harbor", ctx, scope_hint="a", mode="lexical", limit=1)
    assert [i.record_id for i in items] == ["strong"]


def test_true_tie_still_reorders_by_confidence(tmp_path):
    from aletheia.retrieval import recall
    sub, ctx = _store(tmp_path)
    sub.put(Record("s1", "Source", "a", {"text": "origin"}, attribution="user"), ctx)
    # Identical text => identical BM25; ids order low first, confidence must win the tie.
    sub.put(Record("c1", "Claim", "a", {"text": "harbor pier", "confidence": 0.1},
                   epistemic_status="derived", source_ids=["s1"], attribution="agent"), ctx)
    sub.put(Record("c2", "Claim", "a", {"text": "harbor pier", "confidence": 0.9},
                   epistemic_status="derived", source_ids=["s1"], attribution="agent"), ctx)
    items, _ = recall(sub, "harbor", ctx, scope_hint="a", mode="lexical", limit=1)
    assert [i.record_id for i in items] == ["c2"]


def test_ranker_does_not_use_payload_text_token_counts(tmp_path, monkeypatch):
    import aletheia.retrieval as retrieval
    assert not hasattr(retrieval, "_lexical_profile")


# ---- (b) per-request engine knows every configured scope -------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALETHEIA_API_KEY", KEY)
    monkeypatch.setenv("ALETHEIA_KNOWN_SCOPES", "a,b")
    monkeypatch.setenv("ALETHEIA_DB_PATH", str(tmp_path / "s.db"))
    return tmp_path / "s.db"


@pytest.mark.parametrize("module,method,path,body", [
    ("aletheia.mcp_server", "post", "/mcp/status", {"scope": "a"}),
    ("aletheia.mcp_server", "post", "/mcp/recall", {"scope": "a", "query": "x"}),
    ("aletheia.server", "post", "/query", {"scope": "a", "query": "x"}),
    ("aletheia.server", "post", "/export", {"scope": "a"}),
    ("aletheia.shadow", "post", "/shadow/recall", {"scope": "a", "query": "x"}),
    ("aletheia.shadow", "post", "/shadow/compare", {"scope": "a", "query": "x"}),
])
def test_request_engine_has_full_known_scopes_but_grants_only_requested(env, monkeypatch, module, method, path, body):
    import importlib
    from aletheia.server import app
    mod = importlib.import_module(module)
    real = Substrate
    seen = []

    def spy(db, engine, *a, **k):
        seen.append(set(engine.known_scopes))
        return real(db, engine, *a, **k)

    monkeypatch.setattr(mod, "Substrate", spy)
    c = TestClient(app, raise_server_exceptions=False)
    r = getattr(c, method)(path, headers=HDR, json=body)
    assert seen, r.text
    assert all(s == {"a", "b"} for s in seen)
    # still authorizes only the requested scope
    assert c.post("/mcp/status", headers=HDR, json={"scope": "zzz"}).status_code == 403


def test_503_names_full_rebuild_command_for_requested_scope(env):
    import sqlite3
    c = TestClient(__import__("aletheia.server", fromlist=["app"]).app, raise_server_exceptions=False)
    r = c.post("/mcp/ingest", headers=HDR, json={"scope": "b", "records": [
        {"record_id": "r1", "payload": {"text": "alpha apple"}}]})
    assert r.status_code == 200, r.text
    db = sqlite3.connect(env)
    db.execute("DELETE FROM fts WHERE record_id='r1'")
    db.commit()
    db.close()
    r = c.post("/mcp/recall", headers=HDR, json={"scope": "b", "query": "apple"})
    assert r.status_code == 503
    assert "aletheia --db <path> rebuild-fts --scope b" in r.text
    assert str(env) not in r.text


# ---- (c) unified bearer bytes ---------------------------------------------

def _req(raw: bytes) -> Request:
    return Request({"type": "http", "headers": [(b"authorization", raw)]})


@pytest.mark.parametrize("key", ["pässwörd-ключ", "plain-ascii"])
def test_non_ascii_key_authenticates_in_both_paths(monkeypatch, key):
    monkeypatch.setenv("ALETHEIA_API_KEY", key)
    raw = b"Bearer " + key.encode("utf-8")
    assert server_common.check_bearer(raw) is None
    server_common.require_api_key(_req(raw))  # must not raise


def test_wrong_and_invalid_utf8_rejected_in_both_paths(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("ALETHEIA_API_KEY", "pässwörd")
    for raw in (b"Bearer p\xe4ssw\xf6rd", b"Bearer \xff\xfe", b"Bearer wrong", b"Basic x", b""):
        assert server_common.check_bearer(raw)[0] == 401
        with pytest.raises(HTTPException) as exc:
            server_common.require_api_key(_req(raw))
        assert exc.value.status_code == 401


def test_non_ascii_key_over_asgi(env, monkeypatch):
    monkeypatch.setenv("ALETHEIA_API_KEY", "pässwörd")
    from aletheia.server import app
    c = TestClient(app)
    ok = c.get("/health", headers=[(b"authorization", "Bearer pässwörd".encode("utf-8"))])
    assert ok.status_code == 200
    bad = c.get("/health", headers=[(b"authorization", b"Bearer \xff")])
    assert bad.status_code == 401


def test_401_before_validation(env):
    from aletheia.server import app
    r = TestClient(app).post("/mcp/recall", json={"scope": 5, "limit": "x"})
    assert r.status_code == 401


# ---- (d) docs --------------------------------------------------------------

def test_docs_measured_numbers_and_full_command():
    doc = (ROOT / "docs/turnkey-deployment.md").read_text()
    assert "15 s at 5,000" not in doc
    for needle in ("0.03 s", "0.17 s", "0.51 s", "3.1 s", "3.53", "3.40"):
        assert needle in doc
    assert "aletheia --db <path> rebuild-fts --scope <scope>" in doc
    assert "orphan" in doc.lower()
