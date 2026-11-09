"""Copying memory between stores, and renaming a scope without breaking the indexes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from aletheia.ingest import Ingestor
from aletheia.merge import merge_store, scope_counts, upgrade_scope
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


def _build(path: Path, scope: str, texts: list[str]) -> list[str]:
    engine = ScopeEngine(known_scopes={scope})
    ctx = ScopeContext(granted_scopes=frozenset({scope}), user_authored=True, actor="test")
    sub = Substrate(path, engine)
    ids = []
    try:
        ingestor = Ingestor(sub, embedder=None)
        for text in texts:
            ids.append(ingestor.ingest_turn(text, "user", ctx, scope, "conv", attribution="user"))
    finally:
        sub.close()
    return ids


def _ctx(scope: str) -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset({scope}), user_authored=True, actor="test")


def test_merge_copies_records_and_leaves_the_source_byte_identical(tmp_path):
    source = tmp_path / "profile-a.sqlite3"
    target = tmp_path / "host.sqlite3"
    ids = _build(source, "profile-old", ["first memory", "second memory"])
    source_bytes = source.read_bytes()

    result = merge_store(source, target, "profile-old", "profile:new", embedder=None)

    assert result["exported"] == 2 and result["imported"] == 2, json.dumps(result)
    assert result["balanced"] is True
    # The source is opened read-only: no PRAGMA writes, no schema migration, no chmod.
    assert source.read_bytes() == source_bytes, "the source store must not be modified"
    assert scope_counts(target) == {"profile:new": 2}

    sub = Substrate(target, ScopeEngine(known_scopes={"profile:new"}))
    try:
        stored = {rec.record_id for rec in sub.all_records(_ctx("profile:new"))}
    finally:
        sub.close()
    assert stored == set(ids), "record ids survive so lineage still points somewhere"


def test_merge_is_idempotent(tmp_path):
    source = tmp_path / "profile-a.sqlite3"
    target = tmp_path / "host.sqlite3"
    _build(source, "profile-old", ["first memory", "second memory"])

    first = merge_store(source, target, "profile-old", "profile:new", embedder=None)
    second = merge_store(source, target, "profile-old", "profile:new", embedder=None)

    assert first["imported"] == 2
    assert second["imported"] == 0 and second["already_present"] == 2
    assert second["balanced"] is True
    assert scope_counts(target) == {"profile:new": 2}


def test_merge_never_overwrites_a_divergent_record(tmp_path):
    source = tmp_path / "profile-a.sqlite3"
    target = tmp_path / "host.sqlite3"
    ids = _build(source, "profile-old", ["alpha memory one"])
    _build(target, "profile:new", ["target memory of its own"])

    # Re-use the source id with different content in the target: the merge must refuse, not REPLACE.
    engine = ScopeEngine(known_scopes={"profile:new"})
    sub = Substrate(target, engine)
    try:
        from aletheia.substrate import Record

        sub.put(Record(record_id=ids[0], record_type="Source", scope="profile:new",
                       payload={"text": "REWRITTEN IN TARGET"}, attribution="user"), _ctx("profile:new"))
    finally:
        sub.close()

    result = merge_store(source, target, "profile-old", "profile:new", embedder=None)

    assert result["imported"] == 0
    assert ids[0] in result["skipped_divergent"]
    sub = Substrate(target, engine)
    try:
        rec = sub.get(ids[0], _ctx("profile:new"))
        assert rec is not None and rec.payload["text"] == "REWRITTEN IN TARGET"
    finally:
        sub.close()


def test_merge_of_an_empty_scope_is_reported_not_silent(tmp_path):
    source = tmp_path / "empty.sqlite3"
    target = tmp_path / "host.sqlite3"
    _build(source, "profile-old", [])
    result = merge_store(source, target, "profile-old", "profile:new", embedder=None)
    assert result["exported"] == 0
    assert "holds no records" in result["error"], "a typo'd scope must not look like a no-op success"


def test_missing_source_is_reported(tmp_path):
    result = merge_store(tmp_path / "nope.sqlite3", tmp_path / "host.sqlite3",
                         "profile-old", "profile:new", embedder=None)
    assert result["error"] == "source store does not exist"


def test_upgrade_scope_moves_records_and_journal_but_not_receipts(tmp_path):
    db = tmp_path / "host.sqlite3"
    _build(db, "profile-.hermes", ["old memory"])
    # Produce a receipt under the old scope: recall is what writes one.
    from aletheia.retrieval import recall as _recall

    sub = Substrate(db, ScopeEngine(known_scopes={"profile-.hermes"}))
    try:
        _recall(sub, "old memory", _ctx("profile-.hermes"), limit=5, mode="lexical")
    finally:
        sub.close()

    result = upgrade_scope(db, "profile-.hermes", "profile:default")

    assert result["changed"].get("records") == 1
    assert result["changed"].get("fts") == 1
    assert scope_counts(db) == {"profile:default": 1}
    conn = sqlite3.connect(db)
    try:
        journal_scopes = {row[0] for row in conn.execute("SELECT DISTINCT scope FROM journal")}
        receipt_scopes = [json.loads(row[0]) for row in conn.execute("SELECT scope_consulted FROM receipts")]
        rename_notes = conn.execute(
            "SELECT COUNT(*) FROM journal WHERE op='rename_scope'").fetchone()[0]
    finally:
        conn.close()
    assert journal_scopes == {"profile:default"}
    # A receipt records what was consulted at the time; a rename must not edit the audit trail.
    assert any("profile-.hermes" in scopes for scopes in receipt_scopes)
    assert rename_notes == 1


def test_upgrade_scope_refuses_to_union_two_live_scopes(tmp_path):
    db = tmp_path / "host.sqlite3"
    _build(db, "profile-.hermes", ["old memory"])
    _build(db, "profile:default", ["already here"])

    result = upgrade_scope(db, "profile-.hermes", "profile:default")

    assert "already holds" in result["refused"]
    assert scope_counts(db) == {"profile-.hermes": 1, "profile:default": 1}


def test_upgrade_scope_is_idempotent(tmp_path):
    db = tmp_path / "host.sqlite3"
    _build(db, "profile-.hermes", ["old memory"])
    upgrade_scope(db, "profile-.hermes", "profile:default")
    second = upgrade_scope(db, "profile-.hermes", "profile:default")
    assert second["changed"] == {}
    assert "no records" in second["refused"]
    assert scope_counts(db) == {"profile:default": 1}