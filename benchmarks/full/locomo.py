"""LoCoMo full-benchmark runner.

Dataset: 10 multi-session conversations, ~5,900 turns, 1,986 QA pairs.
"""

from __future__ import annotations

import json
import os
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


def _load_dataset():
    from datasets import load_dataset

    token = os.environ.get("HF_TOKEN")
    if token:
        return load_dataset("CodyTonn/LoCoMo", split="train", token=token)
    return load_dataset("CodyTonn/LoCoMo", split="train")


def _parse_evidence_id(raw: str) -> list[str]:
    """Normalise LoCoMo evidence references like D1:3 or D1:3; D9:17."""
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw)
    text = str(raw).replace(":", "-").replace(";", " ").replace(",", "")
    parts = [p.strip() for p in text.split()]
    ids = []
    for part in parts:
        if not part:
            continue
        part = part.strip("-_")
        if not part:
            continue
        ids.append(part.lower())
    return ids if ids else ["rec"]


def convert_sample(sample: dict) -> tuple[str, list[Record], list[QAPair]]:
    """Return (conv_id, records, qa_pairs) for one LoCoMo conversation."""
    conv_id = f"locomo-{sample['sample_id']}"
    records: list[Record] = []
    qa_pairs: list[QAPair] = []
    seen_ids: set[str] = set()

    def _add_record(rec: Record) -> None:
        if rec.record_id in seen_ids:
            return  # skip duplicate to preserve QA evidence ID mapping
        seen_ids.add(rec.record_id)
        records.append(rec)

    # Observations -> Source records with evidence IDs
    obs_data = json.loads(sample.get("observation", "{}") or "{}")
    if isinstance(obs_data, dict):
        for session_key, speakers in obs_data.items():
            if not isinstance(speakers, dict):
                continue
            for speaker, items in speakers.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, list) and len(item) >= 2:
                        text, ev = item[0], item[1]
                        ev_ids = _parse_evidence_id(ev)
                    elif isinstance(item, str):
                        text = item
                        ev_ids = [f"obs-{len(seen_ids)}"]
                    else:
                        continue
                    if not text:
                        continue
                    for ev_id in ev_ids:
                        _add_record(
                            Record(
                                record_id=f"{conv_id}-{ev_id}",
                                record_type="Source",
                                scope="benchmark",
                                payload={"text": text},
                                epistemic_status="stated",
                                attribution="document",
                                provenance={"session": session_key, "speaker": speaker},
                            )
                        )

    # Event summaries -> Source records
    event_data = json.loads(sample.get("event_summary", "{}") or "{}")
    if isinstance(event_data, dict):
        for session_key, content in event_data.items():
            if not isinstance(content, dict):
                continue
            for speaker, texts in content.items():
                if not isinstance(texts, list):
                    continue
                for text in texts:
                    if not text:
                        continue
                    _add_record(
                        Record(
                            record_id=f"{conv_id}-evt-{session_key}-{speaker}-{len(records)}",
                            record_type="Source",
                            scope="benchmark",
                            payload={"text": text},
                            epistemic_status="stated",
                            attribution="document",
                            provenance={"session": session_key, "speaker": speaker},
                        )
                    )

    # Session summaries -> Source records
    ss_data = json.loads(sample.get("session_summary", "{}") or "{}")
    if isinstance(ss_data, dict):
        for session_key, text in ss_data.items():
            if not text or not isinstance(text, str):
                continue
            _add_record(
                Record(
                    record_id=f"{conv_id}-ss-{session_key}",
                    record_type="Source",
                    scope="benchmark",
                    payload={"text": text},
                    epistemic_status="stated",
                    attribution="document",
                    provenance={"session": session_key},
                )
            )

    # Raw conversation turns -> Source records
    # QA evidence references (e.g. D1:12) map directly to dia_ids here.
    conv_data = json.loads(sample.get("conversation", "{}") or "{}")
    if isinstance(conv_data, dict):
        for session_key, turns in conv_data.items():
            if not isinstance(turns, list):
                continue
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                speaker = turn.get("speaker", "")
                dia_id = turn.get("dia_id", "")
                text = turn.get("text", "")
                if not dia_id or not text:
                    continue
                normalized_dia = dia_id.lower().replace(":", "-")
                _add_record(
                    Record(
                        record_id=f"{conv_id}-{normalized_dia}",
                        record_type="Source",
                        scope="benchmark",
                        payload={"text": text},
                        epistemic_status="stated",
                        attribution="document",
                        provenance={"session": session_key, "speaker": speaker, "dia_id": dia_id},
                    )
                )

    # QA pairs
    qa_data = json.loads(sample.get("qa", "[]") or "[]")
    for qa in qa_data:
        gold_ids = [_parse_evidence_id(e) for e in qa.get("evidence", [])]
        flat_ids = [gid for sublist in gold_ids for gid in sublist]
        qa_pairs.append(
            QAPair(
                question_id=f"{conv_id}-qa-{qa.get('id', len(qa_pairs))}",
                question=qa.get("question", ""),
                gold_answer=qa.get("answer", ""),
                abstain=False,
                category=str(qa.get("category", "")),
                evidence_ids=[f"{conv_id}-{g}" for g in flat_ids],
            )
        )

    return conv_id, records, qa_pairs


def run(embedder=None) -> dict:
    ds = _load_dataset()
    all_lex: list[ScoreResult] = []
    all_hyb: list[ScoreResult] = []
    for sample in ds:
        conv_id, records, qa_pairs = convert_sample(sample)
        if not records or not qa_pairs:
            continue
        sub = create_substrate(conv_id, records, embedder=embedder)
        ctx = make_ctx()
        try:
            for qa in qa_pairs:
                lex_res = run_recall(sub, qa.question, embedder=None)
                lex_score = score_answer(qa.question_id, "lexical", qa.gold_answer, lex_res, qa.abstain,
                                         gold_record_ids=qa.evidence_ids)
                all_lex.append(lex_score)
                if embedder is not None:
                    hyb_res = run_recall(sub, qa.question, embedder=embedder)
                    hyb_score = score_answer(qa.question_id, "hybrid", qa.gold_answer, hyb_res, qa.abstain,
                                             gold_record_ids=qa.evidence_ids)
                    all_hyb.append(hyb_score)
        finally:
            sub.close()
    return {
        "dataset": "locomo",
        "count": len(all_lex),
        "lexical": _aggregate(all_lex),
        "hybrid": _aggregate(all_hyb) if all_hyb else None,
    }


def _aggregate(scores: list[ScoreResult]) -> dict:
    total = len(scores)
    exact = sum(1 for s in scores if s.exact)
    substring = sum(1 for s in scores if s.substring)
    f1_sum = sum(s.f1 for s in scores)
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
        "p50_ms": p50,
        "p95_ms": p95,
    }
