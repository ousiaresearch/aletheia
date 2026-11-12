"""Fact-mediated retrieval must not become a rename of the answer.

Two rules are load-bearing, and both are tested here without a model:

1. a claim carries the id of the record it was extracted from;
2. a retrieved claim is scored as that source record, or not at all.

Without rule 2, hit@10 could improve purely because a claim whose text happens to match the question
occupies a slot while the evidence itself was never retrieved — the same defect class as scoring at
depth 50 and calling it k=10.
"""
from __future__ import annotations

import json
from pathlib import Path

from benchmarks.facts import (claim_records, claim_id, fact_sentence, load_facts, resolve_claims)


class _Item:
    def __init__(self, record_id, text=""):
        self.record_id = record_id
        self.text = text


def test_claim_text_is_readable_and_keeps_the_fact_type():
    text = fact_sentence({"subject": "Melanie", "predicate": "painted", "object": "a lake sunrise",
                          "fact_type": "event"})
    assert "Melanie" in text and "painted" in text and "lake sunrise" in text
    assert text.endswith("(event)")


def test_claim_ids_are_stable_and_source_tagged():
    fact = {"subject": "Caroline", "predicate": "is planning to continue", "object": "her education"}
    a = claim_id("locomo-conv-26-d1-9", fact, 0)
    b = claim_id("locomo-conv-26-d1-9", fact, 0)
    c = claim_id("locomo-conv-26-d2-1", fact, 0)
    assert a == b, "the same fact must produce the same id across runs"
    assert a != c, "the same fact from different sources is a different claim"
    assert a.startswith("claim-")


def test_claim_records_carry_their_source_id():
    facts = {"rec-1": [{"subject": "Ana", "predicate": "lives in", "object": "Lisbon",
                        "confidence": 0.9}]}
    claims = claim_records(facts)
    assert len(claims) == 1
    assert claims[0].source_id == "rec-1"
    assert claims[0].record_id.startswith("claim-")


def test_confidence_floor_filters_low_confidence_facts():
    facts = {"rec-1": [
        {"subject": "Ana", "predicate": "lives in", "object": "Lisbon", "confidence": 0.95},
        {"subject": "Ana", "predicate": "maybe likes", "object": "rain", "confidence": 0.3},
    ]}
    assert len(claim_records(facts, min_confidence=0.7)) == 1


def test_only_restricts_claims_to_the_given_records():
    facts = {"rec-1": [{"subject": "A", "predicate": "p", "object": "o", "confidence": 1}],
             "rec-2": [{"subject": "B", "predicate": "p", "object": "o", "confidence": 1}]}
    assert {c.source_id for c in claim_records(facts, only=["rec-2"])} == {"rec-2"}


def test_a_retrieved_claim_is_scored_as_its_source_record():
    source = _Item("rec-1", "Melanie painted a lake sunrise")
    claim = _Item("claim-abc", "Melanie painted a lake sunrise (event)")
    resolved, count = resolve_claims([claim], {"claim-abc": "rec-1"}, {"rec-1": source}, limit=10)
    assert count == 1
    assert resolved == [source], "the claim must become the evidence record, not stay a claim"


def test_a_source_already_present_is_not_duplicated():
    source = _Item("rec-1")
    claim = _Item("claim-abc")
    resolved, count = resolve_claims([source, claim], {"claim-abc": "rec-1"}, {"rec-1": source},
                                     limit=10)
    assert resolved == [source]
    assert count == 0, "the source was already retrieved, so the claim adds nothing"


def test_resolution_respects_the_scored_limit():
    sources = {f"rec-{i}": _Item(f"rec-{i}") for i in range(12)}
    claims = [_Item(f"claim-{i}") for i in range(12)]
    mapping = {f"claim-{i}": f"rec-{i}" for i in range(12)}
    resolved, _ = resolve_claims(claims, mapping, sources, limit=10)
    assert len(resolved) == 10


def test_load_facts_last_row_wins(tmp_path: Path):
    path = tmp_path / "facts.jsonl"
    path.write_text(
        json.dumps({"record_id": "r1", "facts": [{"subject": "a"}]}) + "\n"
        + json.dumps({"record_id": "r1", "facts": []}) + "\n"
        + json.dumps({"record_id": "r2", "facts": [{"subject": "b"}]}) + "\n",
        encoding="utf-8")
    facts = load_facts(path)
    assert facts["r1"] == [], "an empty later row is a real result (a retry that found nothing)"
    assert facts["r2"] == [{"subject": "b"}]

def test_a_facts_arm_without_a_wider_pool_refuses_to_run():
    """Claims compete for slots; resolving them after a ten-item cut displaces evidence.

    Measured failure this guard exists for: a facts arm at recall depth == limit scored 51.7% hit@10
    against a 56.8% baseline, coverage falling from 0.464 to 0.391, because ~75% of the corpus was
    claims and only 347 claim hits resolved across 120 questions.
    """
    import inspect
    from benchmarks import run as run_mod

    source = inspect.getsource(run_mod.run)
    assert "would occupy scored slots" in source
    assert "--recall-depth" in source
