import time
import pytest
from weft.substrate import Substrate, Record
from weft.scope import ScopeContext, ScopeEngine

KNOWN = {"s"}


def _seed_source(sub, scope):
    sub._conn.execute(
        "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
        "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("src-1-s", "Source", scope, "stated", '{"text": "seed"}', 0.0, None, None, "[]", "{}", "document"),
    )
    sub._conn.commit()


def test_prune_derived_deletes_old_derived_records_with_sources(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        claim = Record(record_id="c1", record_type="Claim", scope="s", attribution="agent",
                       source_ids=["src-1-s"], payload={"text": "old claim"}, epistemic_status="derived")
        sub._write(claim, ctx, validate_schema=True, overwrite=False)
        # Move created_at to the past.
        sub._conn.execute("UPDATE records SET created_at = ? WHERE record_id = ?", (time.time() - 86400, "c1"))
        sub._conn.commit()
        pruned = sub.prune_derived("s", time.time() - 3600, ctx)
        assert pruned == 1
        assert sub.get("c1", ctx) is None
    finally:
        sub.close()


def test_prune_derived_keeps_records_with_missing_sources(tmp_path):
    sub = Substrate(tmp_path / "b.db", ScopeEngine(known_scopes=KNOWN))
    try:
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        claim = Record(record_id="c1", record_type="Claim", scope="s", attribution="agent",
                       source_ids=["missing"], payload={"text": "orphan claim"}, epistemic_status="derived")
        _seed_source(sub, "s")
        claim.source_ids = ["src-1-s"]
        sub._write(claim, ctx, validate_schema=True, overwrite=False)
        sub._conn.execute("UPDATE records SET source_ids='[\"missing\"]' WHERE record_id='c1'")
        sub._conn.execute("UPDATE records SET created_at = ? WHERE record_id = ?", (time.time() - 86400, "c1"))
        sub._conn.commit()
        pruned = sub.prune_derived("s", time.time() - 3600, ctx)
        assert pruned == 0
        assert sub.get("c1", ctx) is not None
    finally:
        sub.close()


def test_prune_derived_no_context_returns_zero(tmp_path):
    sub = Substrate(tmp_path / "c.db", ScopeEngine(known_scopes=KNOWN))
    try:
        assert sub.prune_derived("s", time.time() - 3600, None) == 0
    finally:
        sub.close()
