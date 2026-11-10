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
        seed: int = 1, splits: list[str] | None = None, embedder=None,
        with_reader: bool = False) -> dict:
    started = time.time()
    if dataset == "beam":
        rows = _iter_beam(n, sample, seed, splits or ["100K"])
    else:
        rows = DATASETS[dataset](n, sample, seed)

    by_conv: dict[str, list] = {}
    for conv_id, records, qa in rows:
        by_conv.setdefault(conv_id, []).append((records, qa))

    cases = []
    gold_validation = {"questions_with_gold": 0, "questions_with_unreachable_gold": 0,
                       "unreachable_ids": [], "gold_ids_total": 0}
    for conv_id, items in by_conv.items():
        records = items[0][0]
        # Gold-token validation: an evidence id that no record carries can never be retrieved, so a
        # question citing one has an unachievable ceiling. LoCoMo has five such questions (malformed
        # ids like `...-d` and `...-d-11-26`) and the audit that found them found nothing checking.
        present = {rec.record_id for rec in records}
        for _records, qa in items:
            gold_ids = _gold_ids(qa)
            if not gold_ids:
                continue
            gold_validation["questions_with_gold"] += 1
            gold_validation["gold_ids_total"] += len(gold_ids)
            missing = [g for g in gold_ids if g not in present]
            if missing:
                gold_validation["questions_with_unreachable_gold"] += 1
                gold_validation["unreachable_ids"].append(
                    {"question_id": qa.question_id, "missing": missing[:4]})
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
                    reading = None
                    if with_reader:
                        from aletheia.reader import read as read_answer

                        reading = read_answer(qa.question, items_out)
                    cases.append({
                        "question_id": qa.question_id,
                        "reading": None if reading is None else {
                            "text": reading.text,
                            "abstained": reading.abstained,
                            "cited": reading.cited,
                            "model": reading.model,
                            "prompt_version": reading.prompt_version,
                            "dropped_citations": reading.dropped_citations,
                            "gold_ids": list(gold or []),
                            "gold_answer": str(qa.gold_answer or ""),
                        },
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

    summary = _summarise(dataset, cases, limit=limit, elapsed=time.time() - started,
                         sample=sample, seed=seed if sample == "random" else None)
    if with_reader:
        summary["reading"] = _reading_summary(cases)
        # Per-question outcomes are kept because a reading number is only auditable if its failures
        # are inspectable: parity-measurement-spec.md §3 requires publishing judged-wrong examples,
        # and an aggregate alone makes a systematic leniency or a guessing habit invisible.
        summary["reading_cases"] = [
            {"question_id": c["question_id"], "category": c.get("category"),
             **{k: v for k, v in (c.get("reading") or {}).items() if k != "gold_ids"},
             "cited_gold": bool(set((c.get("reading") or {}).get("cited") or [])
                                & set((c.get("reading") or {}).get("gold_ids") or [])),
             "retrieval_hit": c.get("hit")}
            for c in cases if "error" not in c and c.get("reading")
        ]
    summary["gold_validation"] = {
        **gold_validation,
        "unreachable_ids": gold_validation["unreachable_ids"][:10],
    }
    return summary


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


def _reading_summary(cases: list) -> dict:
    """Stage 2: answer accuracy with retrieval held constant, plus what the citations actually name.

    There is no LLM judge here on purpose. A judged number without a declared, versioned, repeatability
    -checked judge is exactly the kind of figure parity-measurement-spec.md exists to prevent, so this
    reports exact match and token F1 as an interim reading measure and says so. It is not comparable to
    a published answer-level score.
    """
    usable = [c for c in cases if "error" not in c and c.get("reading")]
    if not usable:
        return {"measured": False,
                "reason": "no reader configured — set ALETHEIA_LLM_PROVIDER/MODEL/KEY, or point "
                          "ALETHEIA_LLM_BASE_URL at a local OpenAI-compatible endpoint"}
    answered = [c for c in usable if not c["reading"]["abstained"]]
    graded = [c for c in usable if str(c["reading"].get("gold_answer") or "").strip()]
    exact = 0
    f1_total = 0.0
    cited_gold = 0
    for c in graded:
        gold = str(c["reading"]["gold_answer"]).strip().lower()
        text = str(c["reading"]["text"] or "").strip().lower()
        if gold and gold == text:
            exact += 1
        if gold:
            g, p = gold.split(), text.split()
            common = sum(min(g.count(w), p.count(w)) for w in set(g))
            if common:
                precision, recall = common / max(len(p), 1), common / max(len(g), 1)
                f1_total += 2 * precision * recall / (precision + recall)
        if set(c["reading"]["cited"]) & set(c["reading"]["gold_ids"] or []):
            cited_gold += 1
    low, high = wilson(exact, len(graded))
    return {
        "measured": True,
        "n": len(usable),
        "answered": len(answered),
        "abstentions": len(usable) - len(answered),
        "graded": len(graded),
        "exact_match": exact / len(graded) if graded else None,
        "exact_match_ci95": [round(low, 3), round(high, 3)] if graded else None,
        "token_f1": round(f1_total / len(graded), 3) if graded else None,
        "cited_a_gold_record": cited_gold / len(graded) if graded else None,
        "model": usable[0]["reading"]["model"],
        "prompt_version": usable[0]["reading"]["prompt_version"],
        "judge": None,
        "comparable_to_published_answer_scores": False,
        "note": ("Stage 2 — reading over the evidence this system actually retrieved, so it is "
                 "end-to-end in practice. Scored by exact match and token F1, NOT an LLM judge; no "
                 "comparison to Mem0/Zep/MemHQ is licensed from these numbers."),
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
        reading = r.get("reading")
        if reading:
            print()
            if reading.get("measured"):
                print(f"  reading (stage 2, {reading['model']}, {reading['prompt_version']})")
                print(f"    answered {reading['answered']}/{reading['n']}, "
                      f"abstained {reading['abstentions']}")
                print(f"    exact match {reading['exact_match']:.1%} "
                      f"(95% CI {reading['exact_match_ci95'][0]:.1%}-"
                      f"{reading['exact_match_ci95'][1]:.1%})  token F1 {reading['token_f1']:.3f}")
                print(f"    cited a gold record {reading['cited_a_gold_record']:.1%}")
                print(f"    no LLM judge: not comparable to published answer-level scores")
            else:
                print(f"  reading (stage 2): UNMEASURED — {reading['reason']}")
        gv = r.get("gold_validation") or {}
        if gv.get("questions_with_unreachable_gold"):
            print(f"  gold validation: {gv['questions_with_unreachable_gold']} of "
                  f"{gv['questions_with_gold']} questions cite evidence ids no record carries "
                  f"(full recovery impossible for them)")
            for miss in gv["unreachable_ids"][:3]:
                print(f"    {miss['question_id']}: missing {miss['missing']}")
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
    parser.add_argument("--reader", action="store_true",
                        help="also run the reading stage (stage 2); reports UNMEASURED when no "
                             "reader is configured rather than reporting zero")
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
                           seed=args.seed, splits=args.splits, embedder=embedder,
                           with_reader=args.reader))
    report(results)

    if args.json:
        Path(args.json).write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())