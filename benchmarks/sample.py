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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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


def _locomo(n: int):
    from benchmarks.full.locomo import _convert_conversation, _load_dataset

    dataset = _load_dataset()
    for index, conversation in enumerate(dataset):
        if index >= n:
            break
        yield from _convert_conversation(conversation)


def run(dataset: str, n: int, *, offset: int = 0, embedder=None,
        use_cross_encoder: bool = False) -> dict:
    from benchmarks.full.common import create_substrate, run_recall, score_answer

    cases = []
    started = time.time()
    pending = _longmem if dataset == "longmem" else _locomo
    cases_iter = pending(n, offset) if pending is _longmem else pending(n)
    for index, (conv_id, records, qa) in enumerate(cases_iter):
        sub = create_substrate(f"{conv_id}-{index}", records, embedder=embedder)
        try:
            result = run_recall(sub, qa.question, embedder=embedder, limit=10,
                                use_cross_encoder=use_cross_encoder)
            cases.append(score_answer(qa.question_id, "hybrid" if embedder else "lexical",
                                      qa.gold_answer, result, qa.abstain, None))
        finally:
            sub.close()
        if (index + 1) % 10 == 0:
            print(f"  ... {index + 1}/{n} ({time.time() - started:.0f}s)", file=sys.stderr)

    graded = [c for c in cases if c.abstention_correct is None]
    exact = sum(1 for c in graded if c.exact)
    substring = sum(1 for c in graded if c.substring)
    f1 = sum(c.f1 for c in graded) / len(graded) if graded else 0.0
    mrr = sum(c.mrr for c in graded) / len(graded) if graded else 0.0
    abstentions = [c for c in cases if c.abstention_correct is not None]
    abstain_ok = sum(1 for c in abstentions if c.abstention_correct)

    exact_low, exact_high = wilson(exact, len(graded))
    sub_low, sub_high = wilson(substring, len(graded))
    latencies = sorted(c.latency_ms for c in cases)
    return {
        "dataset": dataset,
        "mode": "hybrid" if embedder is not None else "lexical",
        "cross_encoder": use_cross_encoder,
        "questions": len(cases),
        "graded": len(graded),
        "exact": exact,
        "exact_rate": exact / len(graded) if graded else 0.0,
        "exact_ci95": [round(exact_low, 3), round(exact_high, 3)],
        "substring": substring,
        "substring_rate": substring / len(graded) if graded else 0.0,
        "substring_ci95": [round(sub_low, 3), round(sub_high, 3)],
        "f1": round(f1, 3),
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
          f"EM {report['exact']}/{report['graded']} ({report['exact_rate']:.1%}, "
          f"95% CI {report['exact_ci95'][0]:.0%}-{report['exact_ci95'][1]:.0%})  "
          f"substring {report['substring_rate']:.1%}  F1 {report['f1']:.3f}  MRR {report['mrr']:.3f}  "
          f"abstain {report['abstention_correct']}/{report['abstentions']}  "
          f"p50 {report['latency_p50_ms']}ms  {report['elapsed_s']}s")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())