"""Phase 12 — Temporal validity tests."""
from __future__ import annotations

import time

import pytest

from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate
from aletheia.temporal import effective_at, set_validity, close_window


@pytest.fixture
def engine():
    return ScopeEngine(known_scopes={"test"})


@pytest.fixture
def substrate(tmp_path, engine):
    sub = Substrate(tmp_path / "aletheia.db", scope_engine=engine)
    yield sub
    sub.close()


def _seed(sub):
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    # Seed source via raw insert to bypass validation (same pattern as test_substrate.py)
    sub._conn.execute(
        "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
        "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("src-1-test", "Source", "test", "stated", '{"text": "seed source"}', 0.0, None, None, "[]", "{}", "document"),
    )
    sub._conn.commit()
    r1 = Record(record_id="r1", record_type="Observation", scope="test",
                payload={"text": "Melanie painted a lake sunrise."}, attribution="document",
                source_ids=["src-1-test"])
    sub.put(r1, ctx)
    r2 = Record(record_id="r2", record_type="Observation", scope="test",
                payload={"text": "Caroline attended a support group."}, attribution="document",
                source_ids=["src-1-test"])
    sub.put(r2, ctx)


def test_set_validity_returns_copy():
    rec = Record(record_id="r1", record_type="Observation", scope="test",
                 payload={"text": "x"})
    updated = set_validity(rec, valid_from=1000.0, valid_to=2000.0)
    assert updated.valid_from == 1000.0
    assert updated.valid_to == 2000.0
    assert rec.valid_from is None  # original unchanged


def test_close_window(substrate):
    _seed(substrate)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    result = close_window(substrate, ctx, "r1", close_at=500.0)
    assert result is True
    rec = substrate.get("r1", ctx)
    assert rec.valid_to == 500.0


def test_close_window_missing(substrate):
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    result = close_window(substrate, ctx, "nonexistent")
    assert result is False


def test_effective_at_filters_by_window(substrate):
    _seed(substrate)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    # Set r2 to open at t=600
    r2 = substrate.get("r2", ctx)
    updated_r2 = set_validity(r2, valid_from=600.0, valid_to=None)
    substrate.put(updated_r2, ctx, overwrite=True)
    # Close r1 at t=500
    r1 = substrate.get("r1", ctx)
    updated = set_validity(r1, valid_from=0.0, valid_to=500.0)
    substrate.put(updated, ctx, overwrite=True)
    # At t=300: r1 and src-1-test are valid (source and r1 have open/valid windows)
    at_300 = effective_at(substrate, ctx, "test", 300.0)
    assert "r1" in {r.record_id for r in at_300}
    assert "r2" not in {r.record_id for r in at_300}
    # At t=700: r2 and src-1-test are valid
    at_700 = effective_at(substrate, ctx, "test", 700.0)
    assert "r2" in {r.record_id for r in at_700}
    assert "r1" not in {r.record_id for r in at_700}


def test_effective_at_includes_open_windows(substrate):
    _seed(substrate)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    # Open window for r1: valid_from=0, valid_to=None
    r1 = substrate.get("r1", ctx)
    updated = set_validity(r1, valid_from=0.0, valid_to=None)
    substrate.put(updated, ctx, overwrite=True)
    at_9999 = effective_at(substrate, ctx, "test", 9999.0)
    assert any(r.record_id == "r1" for r in at_9999)
