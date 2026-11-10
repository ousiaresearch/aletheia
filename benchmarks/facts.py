"""Turn cached extracted facts into retrievable records, and resolve claim hits back to evidence.

The scoring unit for LoCoMo is a *record* id. A claim is a derived record with its own id, so a claim
that retrieves where its source record would not must not count as a hit by itself — it has to resolve to
the source. Otherwise "fact-mediated retrieval" would improve hit@10 by renaming the answer, which is the
same defect class as scoring at depth 50 while calling it k=10.

So the contract is:

  ingest      one claim record per extracted fact, carrying `source_ids=[source_record_id]`
  retrieve    recall() sees claims and says so (they are records like any other)
  resolve     a retrieved claim is replaced by its source record, in the claim's rank position
  score       unchanged — still gold record ids

`claim_records()` and `resolve_claims()` are pure functions so both rules can be tested without a model.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def load_facts(cache_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load the extraction cache: record_id -> list of facts. Later rows win (the cache is append-only)."""
    path = Path(cache_path)
    facts: dict[str, list[dict[str, Any]]] = {}
    if not path.exists():
        return facts
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rid = row.get("record_id")
            if rid:
                facts[str(rid)] = row.get("facts") or []
    return facts


def fact_sentence(fact: dict[str, Any]) -> str:
    """One claim as a readable sentence, so lexical and vector retrieval can both reach it."""
    subject = str(fact.get("subject") or "").strip()
    predicate = str(fact.get("predicate") or "").strip()
    obj = str(fact.get("object") or "").strip()
    fact_type = str(fact.get("fact_type") or "").strip()
    parts = " ".join(p for p in (subject, predicate, obj) if p)
    if fact_type:
        return f"{parts} ({fact_type})"
    return parts


def claim_id(source_record_id: str, fact: dict[str, Any], index: int) -> str:
    raw = f"{source_record_id}|{fact.get('subject')}|{fact.get('predicate')}|{fact.get('object')}|{index}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"claim-{digest}"


@dataclass
class ClaimRecord:
    """Minimal record shape the substrate builder accepts: id, text, and where it came from."""

    record_id: str
    text: str
    source_id: str
    confidence: float
    fact_type: str


def claim_records(facts_by_record: dict[str, list[dict[str, Any]]],
                  *, only: Iterable[str] | None = None,
                  min_confidence: float = 0.0) -> list[ClaimRecord]:
    """Build claim records for the given source records (all of them when `only` is None)."""
    wanted = set(only) if only is not None else None
    out: list[ClaimRecord] = []
    for source_id, facts in facts_by_record.items():
        if wanted is not None and source_id not in wanted:
            continue
        for index, fact in enumerate(facts or []):
            if not isinstance(fact, dict):
                continue
            try:
                confidence = float(fact.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < min_confidence:
                continue
            text = fact_sentence(fact)
            if not text:
                continue
            out.append(ClaimRecord(record_id=claim_id(source_id, fact, index), text=text,
                                   source_id=source_id, confidence=confidence,
                                   fact_type=str(fact.get("fact_type") or "")))
    return out


def resolve_claims(items: list[Any],
                   claim_to_source: dict[str, str],
                   by_id: dict[str, Any],
                   limit: int) -> tuple[list[Any], int]:
    """Replace retrieved claims with their source records, keeping the claim's rank position.

    Returns (items, resolved_count). A claim whose source is already present is dropped rather than
    duplicated — otherwise one fact could fill several of the ten slots with the same evidence.
    """
    out: list[Any] = []
    seen: set[str] = set()
    resolved = 0
    for item in items:
        rid = getattr(item, "record_id", None)
        source_id = claim_to_source.get(str(rid))
        if source_id is None:
            if rid is not None and str(rid) not in seen:
                out.append(item)
                seen.add(str(rid))
        else:
            source = by_id.get(source_id)
            if source is not None and source_id not in seen:
                out.append(source)
                seen.add(source_id)
                resolved += 1
        if len(out) >= limit:
            break
    return out[:limit], resolved