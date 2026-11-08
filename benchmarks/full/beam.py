"""BEAM full-benchmark runner.

Dataset: 100 conversations (100K/500K/1M/10M splits), 2,000 probing questions.
We run 100K (20 conversations) and 500K (35 conversations) splits.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from benchmarks.full.common import (
    BENCH_DIR,
    CACHE_DIR,
    QAPair,
    RecallResult,
    ScoreResult,
    create_substrate,
    make_ctx,
    run_recall,
    score_answer,
)
from aletheia.substrate import Record


def _load_split(split: str):
    from datasets import load_dataset

    return load_dataset("Mohammadta/BEAM", split=split)


def _convert_sample(sample: dict) -> tuple[str, list[Record], list[QAPair]]:
    conv_id = f"beam-{sample['conversation_id']}"
    records: list[Record] = []
    qa_pairs: list[QAPair] = []

    chat = sample.get("chat", [])
    if isinstance(chat, str):
        chat = json.loads(chat)
    for turn_list in chat:
        if not isinstance(turn_list, list):
            continue
        for turn in turn_list:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role", "")
            content = turn.get("content", "")
            if not content:
                continue
            records.append(
                Record(
                    record_id=f"{conv_id}-turn-{turn.get('id', len(records))}",
                    record_type="Source",
                    scope="benchmark",
                    payload={"text": f"{role}: {content}"},
                    epistemic_status="stated",
                    attribution="document",
                    provenance={},
                )
            )

    pq_str = sample.get("probing_questions", "{}")
    try:
        pq = ast.literal_eval(pq_str) if isinstance(pq_str, str) else pq_str
    except Exception:
        pq = {}

    for category, items in pq.items():
        if not isinstance(items, list):
            continue
        for item in items:
            question = item.get("question", item.get("ideal_response", ""))
            answer = item.get("answer", item.get("ideal_response", item.get("ideal_answer", "")))
            abstain = category == "abstention" or not answer
            qa_pairs.append(
                QAPair(
                    question_id=f"{conv_id}-{category}-{len(qa_pairs)}",
                    question=question,
                    gold_answer=answer,
                    abstain=abstain,
                    category=category,
                    metadata={"difficulty": item.get("difficulty", "")},
                )
            )

    return conv_id, records, qa_pairs


def run(splits: list[str] | None = None, embedder=None) -> dict:
    splits = splits or ["100K", "500K"]
    all_lex: list[ScoreResult] = []
    all_hyb: list[ScoreResult] = []
    total_convs = 0
    for split in splits:
        ds = _load_split(split)
        for sample in ds:
            conv_id, records, qa_pairs = _convert_sample(sample)
            if not records:
                continue
            total_convs += 1
            sub = create_substrate(conv_id, records, embedder=embedder)
            ctx = make_ctx()
            try:
                for qa in qa_pairs:
                    lex_res = run_recall(sub, qa.question, embedder=None)
                    lex_score = score_answer(qa.question_id, "lexical", qa.gold_answer, lex_res, qa.abstain)
                    all_lex.append(lex_score)
                    if embedder is not None:
                        hyb_res = run_recall(sub, qa.question, embedder=embedder)
                        hyb_score = score_answer(qa.question_id, "hybrid", qa.gold_answer, hyb_res, qa.abstain)
                        all_hyb.append(hyb_score)
            finally:
                sub.close()
    return {
        "dataset": "beam",
        "splits": splits,
        "conversations": total_convs,
        "count": len(all_lex),
        "lexical": _aggregate(all_lex),
        "hybrid": _aggregate(all_hyb) if all_hyb else None,
    }


def _aggregate(scores: list[ScoreResult]) -> dict:
    total = len(scores)
    exact = sum(1 for s in scores if s.exact)
    substring = sum(1 for s in scores if s.substring)
    f1_sum = sum(s.f1 for s in scores)
    abstentions = [s for s in scores if s.abstention_correct is not None]
    abstent_correct = sum(1 for s in abstentions if s.abstention_correct)
    latencies = [s.latency_ms for s in scores]
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else 0.0
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else 0.0
    return {
        "total": total,
        "exact": exact,
        "substring": substring,
        "exact_rate": exact / total if total else 0.0,
        "substring_rate": substring / total if total else 0.0,
        "f1_mean": f1_sum / total if total else 0.0,
        "mrr_mean": sum(s.mrr for s in scores) / total if total else 0.0,
        "abstention_count": len(abstentions),
        "abstention_accuracy": abstent_correct / len(abstentions) if abstentions else None,
        "p50_ms": p50,
        "p95_ms": p95,
    }
