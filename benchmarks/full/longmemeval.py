"""LongMemEval full-benchmark runner.

Dataset: 500 questions with chat histories. Tests information extraction,
multi-session reasoning, knowledge updates, temporal reasoning, abstention.
"""

from __future__ import annotations

import json
import urllib.request
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

DATA_FILE = CACHE_DIR / "longmemeval_s_cleaned.json"
URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"


def _ensure_data() -> Path:
    if not DATA_FILE.exists():
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(URL, DATA_FILE)
    return DATA_FILE


def _load_questions() -> list[dict]:
    path = _ensure_data()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


STAMP_FORMATS = ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M")


def _parse_stamp(text) -> float | None:
    """LongMemEval stamps read '2023/05/20 (Sat) 01:33'. Naive on purpose: the set carries no zone,
    and only ordering and coarse windows matter for retrieval."""
    if not isinstance(text, str) or not text.strip():
        return None
    from datetime import datetime

    for fmt in STAMP_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def _convert_question(entry: dict) -> tuple[str, list[Record], QAPair]:
    qid = entry["question_id"]
    question = entry["question"]
    answer = entry.get("answer", "")
    qtype = entry.get("question_type", "")
    abstain = qtype == "abstention" or not answer

    # The dataset carries the time axis and the gold evidence; this converter used to drop both, so
    # temporal questions had no dates to reason over and scoring fell back to "does the answer string
    # appear in the context" because there were no gold ids to hit. Both are wired through now.
    sessions = entry.get("haystack_sessions", [])
    dates = entry.get("haystack_dates") or []
    session_ids = entry.get("haystack_session_ids") or []
    session_index = {sid: index for index, sid in enumerate(session_ids)}
    gold_sessions = {session_index[sid] for sid in (entry.get("answer_session_ids") or [])
                     if sid in session_index}

    records: list[Record] = []
    for sidx, session in enumerate(sessions):
        if not isinstance(session, list):
            continue
        effective_at = _parse_stamp(dates[sidx]) if sidx < len(dates) else None
        for midx, msg in enumerate(session):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue
            records.append(
                Record(
                    record_id=f"{qid}-s{sidx}-m{midx}",
                    record_type="Source",
                    scope="benchmark",
                    payload={"text": f"{role}: {content}"},
                    epistemic_status="stated",
                    attribution="document",
                    effective_at=effective_at,
                    provenance={
                        "session": session_ids[sidx] if sidx < len(session_ids) else f"session-{sidx}",
                        "session_index": sidx,
                        "session_date": dates[sidx] if sidx < len(dates) else None,
                        "gold_session": sidx in gold_sessions,
                    },
                )
            )

    evidence_ids = [rec.record_id for rec in records if rec.provenance.get("gold_session")]
    qa = QAPair(
        question_id=qid,
        question=question,
        gold_answer=answer,
        abstain=abstain,
        category=qtype,
        evidence_ids=evidence_ids,
        metadata={
            "question_date": entry.get("question_date"),
            "question_date_ts": _parse_stamp(entry.get("question_date")),
            "session_ids": session_ids,
            "session_dates": list(dates),
            "gold_session_ids": list(entry.get("answer_session_ids") or []),
        },
    )
    return qid, records, qa


def run(embedder=None) -> dict:
    entries = _load_questions()
    all_lex: list[ScoreResult] = []
    all_hyb: list[ScoreResult] = []
    for entry in entries:
        qid, records, qa = _convert_question(entry)
        if not records:
            # abstention with no history
            all_lex.append(
                ScoreResult(question_id=qid, mode="lexical", exact=False, substring=False, f1=0.0, mrr=0.0, abstention_correct=True, latency_ms=0.0)
            )
            if embedder is not None:
                all_hyb.append(
                    ScoreResult(question_id=qid, mode="hybrid", exact=False, substring=False, f1=0.0, mrr=0.0, abstention_correct=True, latency_ms=0.0)
                )
            continue
        sub = create_substrate(qid, records, embedder=embedder)
        ctx = make_ctx()
        try:
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
        "dataset": "longmemeval-s",
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
