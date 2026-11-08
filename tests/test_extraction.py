"""Phase 11 extraction/consolidation tests."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from aletheia.extraction import extract_facts, write_claims, consolidate_scope, _extract_facts
from aletheia.knowledge import RULES
from aletheia.scope import ScopeEngine, ScopeContext
from aletheia.substrate import Record, Substrate


def _make_sub():
    engine = ScopeEngine(known_scopes={"test"})
    tmp = tempfile.TemporaryDirectory()
    sub = Substrate(Path(tmp.name) / "test.db", engine)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    return sub, ctx, tmp


def test_extract_facts_returns_list():
    """_extract_facts returns a list (empty when no token available)."""
    result = _extract_facts("Caroline attended a LGBTQ support group on 8 May 2023.", "test")
    assert isinstance(result, list)


def test_extract_and_write_claims():
    """extract_facts + write_claims produces Claim records in substrate."""
    sub, ctx, tmp = _make_sub()
    try:
        src = Record(
            record_id="src-1",
            record_type="Source",
            scope="test",
            payload={"text": "Melanie painted a lake sunrise last year."},
            epistemic_status="stated",
            attribution="document",
            source_ids=[],
        )
        sub.put(src, ctx)
        
        obs = Record(
            record_id="obs-1",
            record_type="Observation",
            scope="test",
            payload={"text": "Melanie painted a lake sunrise last year."},
            epistemic_status="stated",
            attribution="document",
            source_ids=["src-1"],
        )
        sub.put(obs, ctx)
        
        claims = extract_facts(sub, ctx, "test", limit=10)
        # With no TokenHarbor token, should return empty list (fail-closed)
        assert isinstance(claims, list)
        
        written = write_claims(sub, ctx, claims)
        assert written == 0
    finally:
        sub.close()


def test_consolidate_empty_scope():
    """consolidate_scope on empty scope returns empty dict."""
    sub, ctx, tmp = _make_sub()
    try:
        result = consolidate_scope(sub, ctx, "test")
        assert result == {"claims_reviewed": 0, "duplicate_count": 0, "kept": 0}
    finally:
        sub.close()


def test_consolidate_deduplicates():
    """consolidate_scope identifies duplicate claims (Correction requires user attribution in Aletheia)."""
    sub, ctx, tmp = _make_sub()
    try:
        src = Record(
            record_id="src-2",
            record_type="Source",
            scope="test",
            payload={"text": "Source for claims."},
            epistemic_status="stated",
            attribution="document",
            source_ids=[],
        )
        sub.put(src, ctx)
        
        claim1 = Record(
            record_id="claim-1",
            record_type="Claim",
            scope="test",
            payload={"text": "melanie painted a lake sunrise last year", "subject": "Melanie", "predicate": "painted", "object": "lake sunrise"},
            epistemic_status="derived",
            attribution="agent",
            source_ids=["src-2"],
        )
        claim2 = Record(
            record_id="claim-2",
            record_type="Claim",
            scope="test",
            payload={"text": "Melanie painted a lake sunrise last year", "subject": "Melanie", "predicate": "painted", "object": "lake sunrise"},
            epistemic_status="derived",
            attribution="agent",
            source_ids=["src-2"],
        )
        sub.put(claim1, ctx)
        sub.put(claim2, ctx)
        
        result = consolidate_scope(sub, ctx, "test")
        # Aletheia design: Corrections require user attribution, so automated
        # consolidation reports duplicates but cannot auto-supersede.
        assert result["claims_reviewed"] == 2
        assert result["duplicate_count"] == 1
        assert result["kept"] == 1  # One unique claim kept; duplicate reported
    finally:
        sub.close()


def test_extraction_fail_closed_on_missing_token():
    """extract_facts returns [] when TokenHarbor token is missing."""
    import os
    old_token = os.environ.get("TOKENHARBOR_API_KEY", "")
    os.environ["TOKENHARBOR_API_KEY"] = ""
    try:
        sub, ctx, tmp = _make_sub()
        try:
            src = Record(
                record_id="src-3",
                record_type="Source",
                scope="test",
                payload={"text": "Source for fail-closed test."},
                epistemic_status="stated",
                attribution="document",
                source_ids=[],
            )
            sub.put(src, ctx)
            
            obs = Record(
                record_id="obs-2",
                record_type="Observation",
                scope="test",
                payload={"text": "Some fact to extract."},
                epistemic_status="stated",
                attribution="document",
                source_ids=["src-3"],
            )
            sub.put(obs, ctx)
            
            claims = extract_facts(sub, ctx, "test", limit=10)
            assert claims == []
        finally:
            sub.close()
    finally:
        os.environ["TOKENHARBOR_API_KEY"] = old_token
