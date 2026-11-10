#!/usr/bin/env python3
"""The benchmark suite, on one command.

Phase 9's acceptance criteria ask for a harness that runs from a single documented command, with gold
answers and reproduction steps committed and results published under `docs/benchmarks/`. This is that
command. It replaces the three overlapping runners that disagreed with each other:

- `benchmarks/full/run_all.py` scored LongMemEval-S and BEAM **without** their gold ids, so both fell
  back to "the gold answer string appears in the top-k" — the proxy that read 44.2% where real hit@10
  was 97.0%. Its output could not be compared to `sample.py`'s.
- `benchmarks/full/beam.py` has no evidence ids in the dataset's probing questions, so BEAM can only
  ever be an answer-contains measure. That is now stated in the output rather than implied.
- `benchmarks/sample.py` was the only runner scoring against gold record ids, and it does not cover
  BEAM.

Every rate is reported with the N it was computed over and a 95% Wilson interval, because a rate
without those is a rumour — three disjoint 50-question slices of LongMemEval-S once spread 88%, 34%
and 62%.

    python benchmarks/run.py --dataset locomo                  # whole LoCoMo, lexical
    python benchmarks/run.py --dataset longmem --n 100
    python benchmarks/run.py --dataset beam --splits 100K --n 40
    python benchmarks/run.py --dataset all --n 200 --sample random --seed 1 --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.full.common import create_substrate, make_ctx, score_answer  # noqa: E402


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


# --------------------------------------------------------------------------------------------
# Dataset iterators. Each yields (conv_id, records, qa) and grouping happens by conv_id, so one
# substrate and (in hybrid mode) one embedding pass is built per conversation rather than per
# question. Building per question was the original sin of this harness: it made a hybrid run over a
# full set cost hours of embedding for vectors it had already computed.
# --------------------------------------------------------------------------------------------

def _iter_locomo(limit: int | None, sample: str, seed: int):
    from benchmarks.full.locomo import _load_dataset, convert_sample

    rows = []
    for conversation in _load_dataset():
        conv_id, records, qas = convert_sample(dict(conversation))
        if not records or not qas:
            continue
        for qa in qas:
            rows.append((conv_id, records, qa))
    if sample == "random" and limit is not None:
        rows = random.Random(seed).sample(rows, min(limit, len(rows)))
    elif limit is not None:
        rows = rows[:limit]
    return rows


def _iter_longmem(limit: int | None, sample: str, seed: int):
    from benchmarks.full.longmemeval import _convert_question, _load_questions

    rows = [_convert_question(entry) for entry in _load_questions()]
    if sample == "random" and limit is not None:
        rows = random.Random(seed).sample(rows, min(limit, len(rows)))
    elif limit is not None:
        rows = rows[:limit]
    return rows


def _iter_beam(limit: int | None, sample: str, seed: int, splits: list[str]):
    from benchmarks.full.beam import _convert_sample, _load_split

    rows = []
    for split in splits:
        for conversation in _load_split(split):
            conv_id, records, qas = _convert_sample(dict(conversation))
            if not records or not qas:
                continue
            for qa in qas:
                qa_with_split = qa
                rows.append((f"{conv_id}-{split}", records, qa_with_split))
    if sample == "random" and limit is not None:
        rows = random.Random(seed).sample(rows, min(limit, len(rows)))
    elif limit is not None:
        rows = rows[:limit]
    return rows


DATASETS = {"locomo": _iter_locomo, "longmem": _iter_longmem}

#: What one gold id refers to. Coverage — the fraction of a question's gold ids that came back — only
#: means something when an id is a single record. LongMemEval's evidence ids name *sessions*, each of
#: which converts to hundreds of message records, so retrieving one message of a session is a
#: legitimate hit while "coverage of that session" is a meaningless near-zero. BEAM ships no evidence
#: ids at all. Reporting one coverage column across all three would be the same category error as
#: comparing hit@10 to an LLM-judged answer score.
GOLD_UNIT = {"locomo": "record", "longmem": "session", "beam": "none"}


def _gold_ids(qa) -> list[str] | None:
    return list(qa.evidence_ids) if getattr(qa, "evidence_ids", None) else None


def _is_abstention(qa) -> bool:
    """A question the correct answer is to decline.

    BEAM and LongMemEval flag these explicitly. LoCoMo does not: its 446 category-5 questions carry
    no answer field at all (two answer "No"), and the converter hardcodes `abstain=False`, so they
    have been graded as ordinary evidence-retrieval questions. Treating an absent gold answer as an
    abstention question is the honest reading, and it is why this suite can report abstention at all
    on LoCoMo.
    """
    if getattr(qa, "abstain", False):
        return True
    return not str(getattr(qa, "gold_answer", "") or "").strip()


def run(dataset: str, *, limit: int = 10, n: int | None = None, sample: str = "prefix",
        seed: int = 1, splits: list[str] | None = None, embedder=None) -> dict:
    started = time.time()
    if dataset == "beam":
        rows = _iter_beam(n, sample, seed, splits or ["100K"])
    else:
        rows = DATASETS[dataset](n, sample, seed)

    by_conv: dict[str, list] = {}
    for conv_id, records, qa in rows:
        by_conv.setdefault(conv_id, []).append((records, qa))

    cases = []
    for conv_id, items in by_conv.items():
        records = items[0][0]
        sub = create_substrate(f"run-{dataset}-{conv_id}", records, embedder=embedder)
        ctx = make_ctx()
        try:
            for _records, qa in items:
                try:
                    from aletheia.retrieval import recall

                    items_out, _receipt = recall(sub, qa.question, ctx, limit=limit,
                                                 mode="hybrid" if embedder else "lexical",
                                                 embedder=embedder)
                    from benchmarks.full.common import RecallResult

                    result = RecallResult(
                        question_id=qa.question_id, mode="hybrid" if embedder else "lexical",
                        selected_ids=[it.record_id for it in items_out],
                        selected_texts=[(it.text or "").lower() for it in items_out],
                        latency_ms=0.0, abstained=not items_out,
                    )
                    gold = _gold_ids(qa)
                    score = score_answer(qa.question_id, result.mode, qa.gold_answer, result,
                                         _is_abstention(qa), gold)
                    selected = set(result.selected_ids[:limit])
                    gold_set = set(gold or [])
                    cases.append({
                        "question_id": qa.question_id,
                        "category": getattr(qa, "category", None) or "unknown",
                        "abstention": _is_abstention(qa),
                        "abstention_correct": score.abstention_correct,
                        "gold_scored": bool(gold),
                        "hit": bool(score.exact) if gold else None,
                        "answer_in_context": score.substring,
                        "mrr": score.mrr,
                        "coverage": (len(gold_set & selected) / len(gold_set)) if gold_set else None,
                        "full_recovery": bool(gold_set) and gold_set <= selected,
                        "gold_n": len(gold_set),
                    })
                except Exception as exc:
                    cases.append({"question_id": qa.question_id,
                                  "category": getattr(qa, "category", None) or "unknown",
                                  "error": f"{exc.__class__.__name__}: {exc}"})
        finally:
            sub.close()

    return _summarise(dataset, cases, limit=limit, elapsed=time.time() - started,
                      sample=sample, seed=seed if sample == "random" else None)


def _gold_unit(dataset: str) -> str:
    return GOLD_UNIT.get(dataset, "record")


def _summarise(dataset: str, cases: list, *, limit: int, elapsed: float,
               sample: str, seed: int | None) -> dict:
    errors = [c for c in cases if "error" in c]
    usable = [c for c in cases if "error" not in c]
    abstention = [c for c in usable if c["abstention"]]
    graded = [c for c in usable if not c["abstention"]]
    gold = [c for c in graded if c["gold_scored"]]
    answer_only = [c for c in graded if not c["gold_scored"]]

    def rate(rows, key):
        if not rows:
            return None
        return sum(1 for r in rows if r[key]) / len(rows)

    hit = sum(1 for c in gold if c["hit"])
    low, high = wilson(hit, len(gold))
    aic = sum(1 for c in graded if c["answer_in_context"])
    aic_low, aic_high = wilson(aic, len(graded))
    abstain_ok = sum(1 for c in abstention if c["abstention_correct"])

    unit = _gold_unit(dataset)
    coverage_meaningful = unit == "record"
    by_category = {}
    for c in gold:
        b = by_category.setdefault(str(c["category"]), {"n": 0, "hits": 0, "cov": 0.0, "full": 0})
        b["n"] += 1
        b["hits"] += 1 if c["hit"] else 0
        b["cov"] += c["coverage"] or 0.0
        b["full"] += 1 if c["full_recovery"] else 0
    for b in by_category.values():
        b["hit_at_k"] = round(b["hits"] / b["n"], 3)
        if coverage_meaningful:
            b["coverage"] = round(b["cov"] / b["n"], 3)
            b["full_recovery"] = round(b["full"] / b["n"], 3)
        b.pop("cov")

    return {
        "dataset": dataset,
        "mode": "hybrid" if any(c.get("mode") == "hybrid" for c in cases) else "lexical",
        "k": limit,
        "questions": len(cases),
        "errors": len(errors),
        "error_sample": errors[0]["error"] if errors else None,
        "graded": len(graded),
        "gold_scored": len(gold),
        "hits": hit,
        "hit_at_k": hit / len(gold) if gold else None,
        "hit_at_k_ci95": [round(low, 3), round(high, 3)] if gold else None,
        "mrr": round(sum(c["mrr"] for c in gold) / len(gold), 3) if gold else None,
        "gold_unit": unit,
        "mean_coverage": (round(sum((c["coverage"] or 0.0) for c in gold) / len(gold), 3)
                          if gold and coverage_meaningful else None),
        "full_recovery": (round(sum(1 for c in gold if c["full_recovery"]) / len(gold), 3)
                          if gold and coverage_meaningful else None),
        "answer_in_context": aic / len(graded) if graded else None,
        "answer_in_context_ci95": [round(aic_low, 3), round(aic_high, 3)] if graded else None,
        "answer_only_questions": len(answer_only),
        "abstention_questions": len(abstention),
        "abstention_correct": abstain_ok,
        "abstention_rate": round(abstain_ok / len(abstention), 3) if abstention else None,
        "by_category": dict(sorted(by_category.items())),
        "selection": {"sample": sample, "seed": seed},
        "elapsed_s": round(elapsed, 1),
        "note": ("hit_at_k is scored against gold evidence ids and is retrieval quality. "
                 "answer_in_context is whether the gold answer string appears in the context and is "
                 "a proxy; where a dataset carries no evidence ids it is the only measure available, "
                 "and it is labelled as such rather than reported as retrieval."),
    }


def report(results: list[dict]) -> None:
    for r in results:
        print()
        head = (f"{r['dataset']}  {r['mode']}  k={r['k']}  "
                f"{r['questions']} questions, {r['errors']} errors")
        print(head)
        print("-" * len(head))
        if r["gold_scored"]:
            print(f"  hit@{r['k']} (gold evidence)  {r['hits']}/{r['gold_scored']} = "
                  f"{r['hit_at_k']:.1%}  95% CI {r['hit_at_k_ci95'][0]:.1%}-{r['hit_at_k_ci95'][1]:.1%}")
            print(f"  MRR {r['mrr']:.3f}")
            if r["mean_coverage"] is not None:
                print(f"  mean coverage {r['mean_coverage']:.3f}   "
                      f"full recovery {r['full_recovery']:.1%}   (gold unit: record)")
            else:
                print(f"  coverage not reported: gold ids are {r['gold_unit']}s, not records")
        else:
            print("  hit@k: not available — this dataset carries no evidence ids")
        if r["graded"]:
            print(f"  answer-in-context (proxy)  {r['answer_in_context']:.1%}  "
                  f"95% CI {r['answer_in_context_ci95'][0]:.1%}-{r['answer_in_context_ci95'][1]:.1%}"
                  f"  over {r['graded']} graded")
        if r["answer_only_questions"]:
            print(f"  {r['answer_only_questions']} questions scored by answer string only "
                  f"(no evidence ids)")
        if r["abstention_questions"]:
            print(f"  abstention  {r['abstention_correct']}/{r['abstention_questions']} correct "
                  f"({r['abstention_rate']:.1%})")
        if r["by_category"]:
            print("  by category:")
            for cat, b in r["by_category"].items():
                extra = (f"coverage {b['coverage']:.3f}  full {b['full_recovery']:.1%}"
                         if "coverage" in b else "")
                print(f"    {str(cat):24} n={b['n']:<5} hit@{r['k']} {b['hit_at_k']:.1%}  {extra}")
        print(f"  elapsed {r['elapsed_s']}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", choices=["locomo", "longmem", "beam", "all"], default="locomo")
    parser.add_argument("--n", type=int, default=None, help="question limit (default: all)")
    parser.add_argument("--limit", type=int, default=10, help="recall depth k")
    parser.add_argument("--mode", choices=["lexical", "hybrid"], default="lexical")
    parser.add_argument("--sample", choices=["prefix", "random"], default="prefix")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--splits", nargs="*", default=["100K"], help="BEAM splits")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    embedder = None
    if args.mode == "hybrid":
        from aletheia.adapters import default_text_embedder
        try:
            embedder = default_text_embedder()
        except Exception as exc:
            print(f"hybrid requested but no embedder: {exc}", file=sys.stderr)
            return 2

    names = ["locomo", "longmem", "beam"] if args.dataset == "all" else [args.dataset]
    results = []
    for name in names:
        print(f"running {name} ...", file=sys.stderr)
        results.append(run(name, limit=args.limit, n=args.n, sample=args.sample,
                           seed=args.seed, splits=args.splits, embedder=embedder))
    report(results)

    if args.json:
        Path(args.json).write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())