#!/usr/bin/env python3
"""Run a small, honest slice of the gold sets and report numbers with their uncertainty.

The full sets live in `benchmarks/full` and were run on Colab. This runs what is checked in under
`benchmarks/datasets` — 22 answerable cases and 2 abstention cases per set — so a local run takes
minutes instead of hours, and it can be repeated after every change.

What this is not: a leaderboard claim. At N=24 a pass rate has a 95% interval roughly ±18 points
wide, and the abstention cases are two. Every number below is printed with its N and interval, and
`--limit` exists so a partial slice is visibly partial rather than quietly quoted as a result.

    python benchmarks/smoke.py                        # lexical, all sets
    python benchmarks/smoke.py --mode hybrid --embed-docs
    python benchmarks/smoke.py --datasets locoomo --limit 25 --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.harness import _load_dataset, _run_case  # noqa: E402

DATASETS = ("locoomo", "longmem", "beam")


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval: honest for small N, unlike a normal approximation near 0 or 1."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def run(dataset: str, *, limit: int | None, embedder=None, embed_docs: bool = False) -> dict:
    cases = _load_dataset(dataset)
    if limit is not None:
        cases = cases[:limit]

    results = []
    for case in cases:
        # Document vectors are the difference between "hybrid" and "lexical with extra steps": the
        # harness inserts records without them, so a hybrid run would embed only the query and fuse
        # against an empty index.
        vectors = None
        if embed_docs and embedder is not None:
            vectors = {}
            for record in case.setup_records:
                text = record.get("text") or ""
                if text:
                    try:
                        vectors[record["record_id"]] = embedder.embed_document(text)
                    except Exception:
                        vectors = {}
                        break
        results.append(_run_case(case, embedder=embedder, vectors=vectors))

    answerable = [r for r in results if not r.case.abstain]
    abstentions = [r for r in results if r.case.abstain]
    passed = sum(1 for r in results if r.passed)
    hits5 = sum(1 for r in answerable if r.recall_at_5)
    hits10 = sum(1 for r in answerable if r.recall_at_10)
    mrr = sum(r.mrr for r in answerable) / len(answerable) if answerable else 0.0
    abstain_ok = sum(1 for r in abstentions if r.abstention_correct)

    low, high = wilson(passed, len(results))
    r5_low, r5_high = wilson(hits5, len(answerable))
    return {
        "dataset": dataset,
        "cases": len(results),
        "answerable": len(answerable),
        "abstentions": len(abstentions),
        "passed": passed,
        "pass_rate": passed / len(results) if results else 0.0,
        "pass_rate_ci95": [round(low, 3), round(high, 3)],
        "recall_at_5": hits5 / len(answerable) if answerable else 0.0,
        "recall_at_5_ci95": [round(r5_low, 3), round(r5_high, 3)],
        "recall_at_10": hits10 / len(answerable) if answerable else 0.0,
        "mrr": mrr,
        "abstention_correct": abstain_ok,
        "abstention_accuracy": abstain_ok / len(abstentions) if abstentions else 0.0,
        "latency_p50_ms": round(percentile([r.latency_ms for r in results], 0.50), 1),
        "latency_p95_ms": round(percentile([r.latency_ms for r in results], 0.95), 1),
        "failures": [
            {"case": r.case_id, "why": r.failure_reason}
            for r in results if not r.passed
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datasets", default="all", help="comma-separated: locoomo,longmem,beam or all")
    parser.add_argument("--limit", type=int, default=None, help="cases per dataset (a partial slice)")
    parser.add_argument("--mode", choices=["lexical", "hybrid"], default="lexical")
    parser.add_argument("--embed-docs", action="store_true",
                        help="embed setup records so the hybrid vector arm can actually find them")
    parser.add_argument("--json", default=None, help="write the full report here")
    args = parser.parse_args(argv)

    names = DATASETS if args.datasets == "all" else tuple(d.strip() for d in args.datasets.split(","))
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        parser.error(f"unknown dataset(s): {unknown}")

    embedder = None
    if args.mode == "hybrid":
        from aletheia.adapters import default_text_embedder
        try:
            embedder = default_text_embedder()
        except Exception as exc:
            print(f"hybrid requested but the embedder is unavailable: {exc}", file=sys.stderr)
            return 2

    started = time.time()
    reports = []
    for name in names:
        report = run(name, limit=args.limit, embedder=embedder, embed_docs=args.embed_docs)
        reports.append(report)
        print(f"{name:<10} N={report['cases']:<3} pass {report['passed']}/{report['cases']} "
              f"({report['pass_rate']:.0%}, 95% CI {report['pass_rate_ci95'][0]:.0%}-{report['pass_rate_ci95'][1]:.0%})  "
              f"R@5 {report['recall_at_5']:.3f}  R@10 {report['recall_at_10']:.3f}  "
              f"MRR {report['mrr']:.3f}  abstain {report['abstention_correct']}/{report['abstentions']}  "
              f"p50 {report['latency_p50_ms']}ms")

    combined_cases = sum(r["cases"] for r in reports)
    combined_pass = sum(r["passed"] for r in reports)
    low, high = wilson(combined_pass, combined_cases)
    print(f"{'combined':<10} N={combined_cases:<3} pass {combined_pass}/{combined_cases} "
          f"({combined_pass / combined_cases:.0%}, 95% CI {low:.0%}-{high:.0%})  "
          f"[{args.mode}{' + doc vectors' if args.embed_docs and embedder else ''}]  "
          f"{time.time() - started:.1f}s")

    payload = {
        "mode": args.mode,
        "embed_docs": bool(args.embed_docs and embedder is not None),
        "embedder": getattr(embedder, "model_id", None),
        "limit": args.limit,
        "combined": {"cases": combined_cases, "passed": combined_pass,
                     "pass_rate": combined_pass / combined_cases if combined_cases else 0.0,
                     "pass_rate_ci95": [round(low, 3), round(high, 3)]},
        "datasets": reports,
        "note": ("Small slice of the checked-in gold sets. Every rate carries its N and a 95% Wilson "
                 "interval; none of it is a leaderboard claim, and the abstention cases number two."),
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())