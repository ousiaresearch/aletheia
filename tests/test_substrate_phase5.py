import pytest
import time
from aletheia.substrate import Substrate, Record
from aletheia.scope import ScopeContext, ScopeEngine

KNOWN = {"s", "s1", "s2"}


def _seed_source(sub, scope):
    sub._conn.execute(
        "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
        "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"src-1-{scope}", "Source", scope, "stated", '{"text": "seed"}', 0.0, None, None, "[]", "{}", "document"),
    )
    sub._conn.commit()


def test_get_at_returns_original_before_supersession(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        rec = Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                     source_ids=["src-1-s"], payload={"text": "v1"}, effective_at=0.0)
        sub._write(rec, ctx, validate_schema=True, overwrite=False)
        assert sub.get_at("r1", 50.0, ctx).payload["text"] == "v1"
        assert sub.get_at("r1", 200.0, ctx).payload["text"] == "v1"
    finally:
        sub.close()


def test_get_at_denies_future_effective_at(tmp_path):
    sub = Substrate(tmp_path / "f.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        rec = Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                     source_ids=["src-1-s"], payload={"text": "v1"}, effective_at=100.0)
        sub._write(rec, ctx, validate_schema=True, overwrite=False)
        assert sub.get_at("r1", 50.0, ctx) is None
    finally:
        sub.close()


def test_get_at_follows_supersedes_chain(tmp_path):
    sub = Substrate(tmp_path / "b.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        r1 = Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                    source_ids=["src-1-s"], payload={"text": "v1"}, effective_at=0.0)
        sub._write(r1, ctx, validate_schema=True, overwrite=False)
        r2 = Record(record_id="r2", record_type="Correction", scope="s", attribution="user",
                    source_ids=[], payload={"text": "v2"}, effective_at=200.0, supersedes="r1", epistemic_status="stated")
        sub.correct("r1", r2, ctx, validate_schema=True)
        assert sub.get_at("r1", 50.0, ctx).payload["text"] == "v1"
        assert sub.get_at("r1", 150.0, ctx).payload["text"] == "v1"
        assert sub.get_at("r1", 250.0, ctx).payload["text"] == "v2"
    finally:
        sub.close()


def test_get_at_denies_cross_scope_supersession(tmp_path):
    sub = Substrate(tmp_path / "c.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub, "s1")
        _seed_source(sub, "s2")
        ctx_both = ScopeContext(granted_scopes=frozenset({"s1", "s2"}), user_authored=True)
        ctx1 = ScopeContext(granted_scopes=frozenset({"s1"}), user_authored=True)
        r1 = Record(record_id="r1", record_type="Observation", scope="s1", attribution="document",
                    source_ids=["src-1-s1"], payload={"text": "v1"}, effective_at=100.0)
        sub._write(r1, ctx_both, validate_schema=True, overwrite=False)
        # Insert a cross-scope correction directly (bypassing correct()'s scope check).
        sub._conn.execute(
            "INSERT INTO records (record_id, record_type, scope, epistemic_status, payload, created_at, effective_at, supersedes, source_ids, provenance, attribution, valid_from, valid_to) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("r2", "Correction", "s2", "stated", '{"text": "v2"}', time.time(), 200.0, "r1", "[]", "{}", "user", None, None),
        )
        sub._conn.commit()
        # caller with s1 access sees original, not cross-scope supersession
        assert sub.get_at("r1", 250.0, ctx1).payload["text"] == "v1"
    finally:
        sub.close()


def test_query_as_of_excludes_superseded_records(tmp_path):
    sub = Substrate(tmp_path / "d.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        r1 = Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                    source_ids=["src-1-s"], payload={"text": "v1"}, effective_at=100.0)
        sub._write(r1, ctx, validate_schema=True, overwrite=False)
        r2 = Record(record_id="r2", record_type="Correction", scope="s", attribution="user",
                    source_ids=[], payload={"text": "v2"}, effective_at=200.0, supersedes="r1", epistemic_status="stated")
        sub.correct("r1", r2, ctx, validate_schema=True)
        before = sub.query_as_of(150.0, ctx)
        ids = {r.record_id for r in before}
        assert ids == {"r1"}
        after = sub.query_as_of(250.0, ctx)
        ids = {r.record_id for r in after}
        assert ids == {"r2"}
    finally:
        sub.close()


def test_query_as_of_no_context_returns_empty(tmp_path):
    sub = Substrate(tmp_path / "e.db", ScopeEngine(known_scopes=KNOWN))
    try:
        assert sub.query_as_of(100.0, None) == []
    finally:
        sub.close()
