#!/usr/bin/env python3
"""Sample a slice of a full benchmark set and report numbers with their uncertainty.

`benchmarks/smoke.py` runs the 22-case gold sets checked in beside it; they saturate and stop being
informative. This runs a slice of a *full* set — LongMemEval-S from the local cache, LoCoMo if the
download works — so the number is comparable to the ones in `docs/benchmarks/results.md`, just with
a fraction of the compute.

The sample is deterministic (first N questions in file order) and the size is printed with every
rate, because a 50-item slice of 500 questions has a 95% interval about ±12 points wide. Treat it as
a direction, not a score: it can tell you whether a change helped or hurt, not what the leaderboard
number would be.

    python benchmarks/sample.py                            # LongMemEval-S, 50 questions, lexical
    python benchmarks/sample.py --n 25 --mode hybrid --limit 25
    python benchmarks/sample.py --dataset locomo --n 50
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass
class CaseScore:
    """One question's outcome under both readings: gold-evidence hit and answer-in-context."""

    question_id: str
    hit: bool
    mrr: float
    in_context: bool
    abstention_correct: bool | None
    latency_ms: float
    gold: bool
    category: str = "unknown"


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def _longmem(n: int, offset: int = 0):
    """Yield (conv_id, records, qa) for n LongMemEval-S questions, starting at ``offset``.

    The offset exists to answer the obvious objection to a first-N sample: if the first fifty
    questions are unrepresentative, a second slice from elsewhere will say so.
    """
    from benchmarks.full.longmemeval import _convert_question, _load_questions

    for entry in _load_questions()[offset:offset + n]:
        yield _convert_question(entry)


def _locomo(n: int, offset: int = 0):
    """LoCoMo's converter yields (conv_id, records, [QAPair, ...]) per conversation, and its QA pairs
    already carry gold record ids — which is why the LoCoMo numbers in the docs are real hit@k."""
    from benchmarks.full.locomo import _load_dataset, convert_sample

    pairs = []
    for conversation in _load_dataset():
        conv_id, records, qas = convert_sample(dict(conversation))
        for qa in qas:
            pairs.append((conv_id, records, qa))
    for item in pairs[offset:offset + n]:
        yield item


def run(dataset: str, n: int, *, offset: int = 0, embedder=None,
        use_cross_encoder: bool = False) -> dict:
    from benchmarks.full.common import create_substrate, run_recall, score_answer

    cases = []
    started = time.time()
    pending = _longmem if dataset == "longmem" else _locomo
    cases_iter = pending(n, offset)
    for index, (conv_id, records, qa) in enumerate(cases_iter):
        sub = create_substrate(f"{conv_id}-{index}", records, embedder=embedder)
        try:
            result = run_recall(sub, qa.question, embedder=embedder, limit=10,
                                use_cross_encoder=use_cross_encoder)
            label = "hybrid" if embedder else "lexical"
            # Two scores from one recall, because they answer different questions. With gold ids the
            # number is real retrieval quality (hit@10 on evidence sessions, and MRR over it). Without
            # them it degrades to "the answer string appears in the context", which is a proxy — and
            # for a long time it was the only thing this benchmark reported.
            evidence = score_answer(qa.question_id, label, qa.gold_answer, result, qa.abstain,
                                    qa.evidence_ids or None)
            containment = score_answer(qa.question_id, label, qa.gold_answer, result, qa.abstain, None)
            cases.append(CaseScore(
                question_id=qa.question_id,
                hit=evidence.exact,
                mrr=evidence.mrr,
                in_context=containment.substring,
                abstention_correct=evidence.abstention_correct,
                latency_ms=result.latency_ms,
                gold=bool(qa.evidence_ids),
                category=qa.category or "unknown",
            ))
        finally:
            sub.close()
        if (index + 1) % 10 == 0:
            print(f"  ... {index + 1}/{n} ({time.time() - started:.0f}s)", file=sys.stderr)

    graded = [c for c in cases if c.abstention_correct is None]
    with_gold = [c for c in graded if c.gold]
    hit_gold = sum(1 for c in with_gold if c.hit)
    in_context = sum(1 for c in graded if c.in_context)
    mrr = sum(c.mrr for c in with_gold) / len(with_gold) if with_gold else 0.0
    abstentions = [c for c in cases if c.abstention_correct is not None]
    abstain_ok = sum(1 for c in abstentions if c.abstention_correct)

    exact_low, exact_high = wilson(hit_gold, len(with_gold))
    sub_low, sub_high = wilson(in_context, len(graded))
    latencies = sorted(c.latency_ms for c in cases)
    by_category = {}
    for case in with_gold:
        bucket = by_category.setdefault(case.category, {"n": 0, "hits": 0, "mrr_sum": 0.0,
                                                       "in_context": 0})
        bucket["n"] += 1
        bucket["hits"] += 1 if case.hit else 0
        bucket["mrr_sum"] += case.mrr
        bucket["in_context"] += 1 if case.in_context else 0
    for bucket in by_category.values():
        bucket["hit_rate"] = round(bucket["hits"] / bucket["n"], 3)
        bucket["mrr"] = round(bucket["mrr_sum"] / bucket["n"], 3)
        bucket["answer_in_context_rate"] = round(bucket["in_context"] / bucket["n"], 3)
        bucket.pop("mrr_sum")

    return {
        "dataset": dataset,
        "mode": "hybrid" if embedder is not None else "lexical",
        "by_category": by_category,
        "rows": [
            {"question_id": c.question_id, "category": c.category, "hit": c.hit,
             "mrr": round(c.mrr, 4), "answer_in_context": c.in_context}
            for c in cases
        ],
        "cross_encoder": use_cross_encoder,
        "questions": len(cases),
        "graded": len(graded),
        "gold_graded": len(with_gold),
        "evidence_hits": hit_gold,
        "hit_at_10": hit_gold / len(with_gold) if with_gold else 0.0,
        "hit_at_10_ci95": [round(exact_low, 3), round(exact_high, 3)],
        "answer_in_context": in_context,
        "answer_in_context_rate": in_context / len(graded) if graded else 0.0,
        "answer_in_context_ci95": [round(sub_low, 3), round(sub_high, 3)],
        "mrr": round(mrr, 3),
        "abstentions": len(abstentions),
        "abstention_correct": abstain_ok,
        "latency_p50_ms": round(latencies[len(latencies) // 2], 1) if latencies else 0.0,
        "latency_p95_ms": round(latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))], 1) if latencies else 0.0,
        "elapsed_s": round(time.time() - started, 1),
        "note": ("A slice, not a score: rates carry their N and a 95% Wilson interval. "
                 "Deterministic sample (first N in file order), lexical unless --mode hybrid."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", choices=["longmem", "locomo"], default="longmem")
    parser.add_argument("--n", type=int, default=50, help="questions to sample")
    parser.add_argument("--offset", type=int, default=0,
                        help="skip this many questions first (a second slice, to test the first)")
    parser.add_argument("--mode", choices=["lexical", "hybrid"], default="lexical")
    parser.add_argument("--limit", type=int, default=10, help="recall depth (ignored: scoring uses 10)")
    parser.add_argument("--cross-encoder", action="store_true", help="rerank with the local cross-encoder")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    embedder = None
    if args.mode == "hybrid":
        from aletheia.adapters import default_text_embedder
        try:
            embedder = default_text_embedder()
        except Exception as exc:
            print(f"hybrid requested but the embedder is unavailable: {exc}", file=sys.stderr)
            return 2

    try:
        report = run(args.dataset, args.n, offset=args.offset, embedder=embedder,
                     use_cross_encoder=args.cross_encoder)
    except Exception as exc:
        print(f"cannot run {args.dataset}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"{report['dataset']} {report['mode']}{' +ce' if report['cross_encoder'] else ''}  "
          f"N={report['graded']} graded of {report['questions']}  "
          f"hit@10 {report['evidence_hits']}/{report['gold_graded']} ({report['hit_at_10']:.1%}, "
          f"95% CI {report['hit_at_10_ci95'][0]:.0%}-{report['hit_at_10_ci95'][1]:.0%})  "
          f"MRR {report['mrr']:.3f}  answer-in-context {report['answer_in_context_rate']:.1%}  "
          f"abstain {report['abstention_correct']}/{report['abstentions']}  "
          f"p50 {report['latency_p50_ms']}ms  {report['elapsed_s']}s")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())