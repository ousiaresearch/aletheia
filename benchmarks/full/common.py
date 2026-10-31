"""Shared utilities for full-benchmark runners."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from weft.retrieval import recall
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate

BENCH_DIR = Path(__file__).resolve().parents[1]
FULL_DIR = BENCH_DIR / "full"
CACHE_DIR = FULL_DIR / "cache"
KNOWN = frozenset({"benchmark"})


@dataclass(frozen=True)
class QAPair:
    question_id: str
    question: str
    gold_answer: str
    abstain: bool = False
    category: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Conversation:
    conv_id: str
    records: list[Record]
    qa_pairs: list[QAPair]
    source_data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RecallResult:
    question_id: str
    mode: str
    selected_ids: list[str]
    selected_texts: list[str]
    latency_ms: float
    abstained: bool
    layers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreResult:
    question_id: str
    mode: str
    exact: bool
    substring: bool
    f1: float = 0.0
    mrr: float = 0.0
    abstention_correct: bool | None = None
    latency_ms: float = 0.0


def make_engine() -> ScopeEngine:
    return ScopeEngine(known_scopes=KNOWN)


def make_ctx() -> ScopeContext:
    return ScopeContext(granted_scopes=KNOWN, user_authored=True)


def create_substrate(conv_id: str, records: list[Record], engine: ScopeEngine | None = None, embedder=None) -> Substrate:
    engine = engine or make_engine()
    tmp = tempfile.TemporaryDirectory(prefix=f"weft-full-{conv_id}-")
    db_path = Path(tmp.name) / f"{conv_id}.db"
    sub = Substrate(db_path, engine)
    ctx = make_ctx()
    for rec in records:
        sub.put(rec, ctx)
    # P2: apply temporal metadata (date extraction) to all records
    try:
        from weft.temporal import apply_temporal_metadata
        updated = [apply_temporal_metadata(r) for r in records]
        for rec in updated:
            sub.put(rec, ctx, overwrite=True)
    except Exception:
        pass
    if embedder is not None:
        try:
            texts = [
                rec.payload.get("text", "") if isinstance(rec.payload, dict) else str(rec.payload)
                for rec in records
            ]
            vectors = embedder.embed_documents(texts)
            for rec, vector in zip(records, vectors):
                try:
                    sub.vector_put(rec.record_id, rec.scope, vector, ctx, model_id=embedder.model_id)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        sub.rebuild_fts("benchmark", ctx)
    except Exception:
        pass
    try:
        from weft.entity_link import link_entities
        link_entities(sub, ctx, "benchmark", limit=500)
    except Exception:
        pass
    return sub


def run_recall(
    sub: Substrate,
    question: str,
    embedder=None,
    limit: int = 10,
    use_cross_encoder: bool = False,
    token_getter=None,
) -> RecallResult:
    ctx = make_ctx()
    start = time.perf_counter()
    items, receipt = recall(
        sub,
        question,
        ctx,
        limit=limit,
        mode="hybrid" if embedder is not None else "lexical",
        embedder=embedder,
        rerank=None,  # auto: on for hybrid/vector when embedder present
        use_cross_encoder=use_cross_encoder,
        token_getter=token_getter,
    )
    latency_ms = (time.perf_counter() - start) * 1000.0
    selected_ids = [item.record_id for item in items]
    selected_texts = []
    for item in items:
        text = item.text or ""
        selected_texts.append(text.lower())
    abstained = len(selected_ids) == 0
    return RecallResult(
        question_id="",
        mode="hybrid" if embedder is not None else "lexical",
        selected_ids=selected_ids,
        selected_texts=selected_texts,
        latency_ms=latency_ms,
        abstained=abstained,
        layers=receipt.layers,
    )


def score_answer(
    question_id: str,
    mode: str,
    gold_answer: str,
    result: RecallResult,
    abstain: bool,
    gold_record_ids: list[str] | None = None,
) -> ScoreResult:
    gold_answer = str(gold_answer or "")
    if abstain:
        return ScoreResult(
            question_id=question_id,
            mode=mode,
            exact=False,
            substring=False,
            f1=0.0,
            mrr=0.0,
            abstention_correct=result.abstained,
            latency_ms=result.latency_ms,
        )

    # Retrieval-based scoring when evidence IDs are available.
    if gold_record_ids:
        gold_set = set(gold_record_ids)
        selected_set = set(result.selected_ids)
        hit = bool(gold_set & selected_set)
        # MRR: 1/rank of first gold hit
        mrr = 0.0
        for rank, rid in enumerate(result.selected_ids, start=1):
            if rid in gold_set:
                mrr = 1.0 / rank
                break
        return ScoreResult(
            question_id=question_id,
            mode=mode,
            exact=hit,
            substring=hit,
            f1=mrr,
            mrr=mrr,
            abstention_correct=None,
            latency_ms=result.latency_ms,
        )

    # Fallback: text-based scoring for datasets without evidence IDs.
    gold_lower = gold_answer.lower()
    combined = " ".join(result.selected_texts)
    exact = gold_lower in combined
    substring = any(gold_lower in t for t in result.selected_texts)
    f1 = _token_f1(gold_answer, combined)
    return ScoreResult(
        question_id=question_id,
        mode=mode,
        exact=exact,
        substring=substring,
        f1=f1,
        mrr=0.0,
        abstention_correct=None,
        latency_ms=result.latency_ms,
    )


def _token_f1(gold: str, predicted: str) -> float:
    """Token-level F1 between gold and predicted text."""
    from collections import Counter
    g = Counter(gold.lower().split())
    p = Counter(predicted.lower().split())
    common = sum(min(g[k], p[k]) for k in g.keys() & p.keys())
    if not common:
        return 0.0
    precision = common / max(sum(p.values()), 1)
    recall = common / max(sum(g.values()), 1)
    return 2 * precision * recall / (precision + recall)
