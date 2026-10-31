"""Phase 12 — Entity link tests."""
from __future__ import annotations

import pytest

from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate
from weft.entity_link import extract_entities, link_entities, entity_boost


@pytest.fixture
def engine():
    return ScopeEngine(known_scopes={"test"})


@pytest.fixture
def substrate(tmp_path, engine):
    sub = Substrate(tmp_path / "weft.db", scope_engine=engine)
    yield sub
    sub.close()


def _seed(sub):
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    # Seed source via raw insert to bypass validation
    sub._conn.execute(
        "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
        "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("src-1-test", "Source", "test", "stated", '{"text": "seed source"}', 0.0, None, None, "[]", "{}", "document"),
    )
    sub._conn.commit()
    r1 = Record(record_id="r1", record_type="Observation", scope="test",
                payload={"text": "Melanie painted a lake sunrise last year."}, attribution="document",
                source_ids=["src-1-test"])
    sub.put(r1, ctx)
    r2 = Record(record_id="r2", record_type="Observation", scope="test",
                payload={"text": "Caroline attended an LGBTQ support group on 8 May 2023."}, attribution="document",
                source_ids=["src-1-test"])
    sub.put(r2, ctx)


def test_extract_entities_basic():
    ents = extract_entities("Melanie painted a lake sunrise last year.")
    assert "melanie" in ents


def test_extract_entities_multiple():
    ents = extract_entities("Melanie met Caroline in May 2023.")
    assert "melanie" in ents
    assert "caroline" in ents


def test_link_entities_creates_links(substrate):
    _seed(substrate)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    result = link_entities(substrate, ctx, "test", limit=10)
    assert result["records_scanned"] == 3  # 2 Observation + 1 Source
    assert result["links_created"] >= 2  # at least melanie and caroline


def test_link_entities_idempotent(substrate):
    _seed(substrate)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    link_entities(substrate, ctx, "test", limit=10)
    result2 = link_entities(substrate, ctx, "test", limit=10)
    assert result2["links_created"] == 0


def test_entity_boost_scores_overlap(substrate):
    _seed(substrate)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    link_entities(substrate, ctx, "test", limit=10)
    scores = entity_boost(substrate, ctx, "Melanie painted", ["r1", "r2"], min_doc_freq=1)
    assert scores.get("r1", 0) > 0
    assert scores.get("r2", 0) == 0


def test_entity_boost_empty_query(substrate):
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    scores = entity_boost(substrate, ctx, "", ["r1"])
    assert scores == {}
