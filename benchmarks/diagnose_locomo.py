#!/usr/bin/env python3
"""Why is LoCoMo 62.6%? One embedding pass per conversation, every recall configuration,
scored against gold record ids rather than the answer-string proxy.

`benchmarks/diagnose_recall.py` does this for LongMemEval with answer-string scoring, which is
the proxy that read 44.2% while real hit@10 was 97.0%. LoCoMo ships evidence ids per QA pair
and the converter already maps them, so every number here is retrieval quality: the gold
record is or is not in the top `limit`.

The comparison is paired and that is the point. One substrate per conversation, built once
with vectors, then every configuration runs against the same records, the same vectors and
the same query. Two questions come out of it:

  1. does `hybrid` lose hits that `lexical` found (unweighted RRF letting a mid-list-in-both
     candidate outscore a lexical rank-1 hit that appears in one list)?
  2. does `rerank=True` lose hits that its own base mode found (the defect fixed on
     LongMemEval q201, band-limited since)?

    python benchmarks/diagnose_locomo.py --n 198 --json benchmarks/diagnose-locomo.json
    python benchmarks/diagnose_locomo.py --n 60 --configs lexical,hybrid-noRerank
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.full.common import create_substrate, make_ctx, score_answer  # noqa: E402
from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


#: Configurations that need no embedding model. Everything else does — named explicitly, because
#: deriving it from a recipe's kwargs was wrong: `lexical+rerank` carries mode="lexical", so the gate
#: read it as embedder-free and the run executed plain lexical under a rerank label (audit finding,
#: 2026-11-09).
_EMBEDDER_FREE = frozenset({"lexical"})


def _recipes(embedder, names: list[str]):
    """(label, kwargs) for the configurations worth telling apart."""
    every = {
        "lexical": dict(mode="lexical"),
        "lexical+rerank": dict(mode="lexical", embedder=embedder, rerank=True),
        "vector": dict(mode="vector", embedder=embedder, rerank=False),
        "vector+rerank": dict(mode="vector", embedder=embedder, rerank=True),
        "hybrid-noRerank": dict(mode="hybrid", embedder=embedder, rerank=False),
        "hybrid+rerank": dict(mode="hybrid", embedder=embedder, rerank=True),
        "hybrid+crossencoder": dict(mode="hybrid", embedder=embedder, use_cross_encoder=True),
    }
    return [(name, every[name]) for name in names]


def _preflight(sub, embedder, ctx, labels) -> str | None:
    """Exercise every retrieval primitive once and return a failure description, or None.

    ``aletheia.retrieval.recall`` catches every non-ValueError from its own candidate queries and
    substitutes an empty list, so a broken FTS index or a failing embedder comes back as an empty
    result — which the scorer records as an ordinary miss with ``errors=0``. That makes an
    infrastructure failure indistinguishable from a retrieval miss, and the audit of this harness
    showed exactly that: inject a RuntimeError into ``fts_candidates`` and the run reported 12
    questions, 0 hits, 0 errors. The probe is the harness's own check that the stage it is about to
    measure is alive.
    """
    try:
        readable = sorted(sub.scope_engine.known_scopes)
        if any(label.startswith("lexical") or label.startswith("hybrid") for label in labels):
            probe = sub.fts_candidates('"the" OR "a"', ctx, 3, scopes=readable)
            if not probe and sub.fts_index_complete(readable[0], ctx):
                return "lexical probe returned no candidates from a complete index"
        if embedder is not None and any(label not in _EMBEDDER_FREE for label in labels):
            if not sub.vector_index_complete(readable[0], ctx, model_id=embedder.model_id):
                return "vector index incomplete despite an embedder being loaded"
            vector = embedder.embed_query("probe")
            got = sub.vector_candidates(vector, ctx, 3, scopes=readable,
                                        model_id=embedder.model_id)
            if not got:
                return "vector probe returned no candidates from a complete index"
    except Exception as exc:
        return f"preflight raised {exc.__class__.__name__}: {exc}"
    return None


def _load_pairs(limit_pairs: int, offset: int):
    """LoCoMo yields conversations; QA pairs are flattened in dataset order so an offset means the
    same slice `benchmarks/sample.py --dataset locomo` scored."""
    per_conv = []
    for conversation in _load_dataset():
        conv_id, records, qas = convert_sample(dict(conversation))
        per_conv.append((conv_id, records, qas))
    flat = []
    for conv_index, (conv_id, records, qas) in enumerate(per_conv):
        for qa in qas:
            flat.append((conv_index, conv_id, records, qa))
    return flat[offset:offset + limit_pairs], per_conv


def _stratified(per_conversation: int, offset: int):
    """First ``offset + per_conversation`` QA pairs from *every* conversation.

    A contiguous slice is one conversation — LoCoMo's first 199 pairs are all conv-26 — which is how
    the 62.6% in the docs came to be quoted as a LoCoMo number. This keeps coverage across all ten
    conversations, so a rate computed from it can be called a LoCoMo rate.
    """
    chosen = []
    per_conv = []
    for conversation in _load_dataset():
        conv_id, records, qas = convert_sample(dict(conversation))
        per_conv.append((conv_id, records, qas))
        for qa in qas[offset:offset + per_conversation]:
            chosen.append((len(per_conv) - 1, conv_id, records, qa))
    return chosen, per_conv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=198, help="QA pairs from the flattened order")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--per-conversation", type=int, default=None,
                        help="instead of --n, take this many QA pairs from every conversation")
    parser.add_argument("--sample", choices=["prefix", "random"], default="prefix",
                        help="'prefix' takes the first N in dataset order (one conversation for "
                             "small N, and category-skewed for --per-conversation); 'random' draws "
                             "uniformly with a seed, which is the only mode whose rate can be "
                             "reported as a LoCoMo rate")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--configs", default="lexical,vector,hybrid-noRerank,hybrid+rerank,"
                                            "lexical+rerank,hybrid+crossencoder")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.configs.split(",") if n.strip()]

    from aletheia.adapters import default_text_embedder
    from aletheia.retrieval import recall

    started_all = time.time()
    # The embedder is only loaded when a requested configuration needs it. A lexical-only pass used
    # to load a 340 MB MLX model and build a vector index for every conversation it would never
    # query, which on an 8 GB machine is most of the cost of the run.
    needs_embedder = any(name not in _EMBEDDER_FREE for name in names)
    embedder = default_text_embedder() if needs_embedder else None
    recipes = _recipes(embedder, names)
    ctx = make_ctx()

    if args.per_conversation:
        selected, _ = _stratified(args.per_conversation, args.offset)
    else:
        selected, _ = _load_pairs(args.n, args.offset)
    if args.sample == "random":
        import random
        pool, _ = _load_pairs(10 ** 9, 0)  # every QA pair, flattened
        rng = random.Random(args.seed)
        take = args.per_conversation * 10 if args.per_conversation else args.n
        selected = rng.sample(pool, min(take, len(pool)))
    selection_provenance = {
        "mode": args.sample,
        "per_conversation": args.per_conversation,
        "requested": args.n,
        "seed": args.seed if args.sample == "random" else None,
        "selected": len(selected),
        "note": ("A prefix selection is one contiguous run of the file and is category-skewed; a "
                 "random selection with a recorded seed is reproducible and representative."),
    }
    if not selected:
        print("no QA pairs selected", file=sys.stderr)
        return 2

    by_conv: dict[int, list] = defaultdict(list)
    for conv_index, conv_id, records, qa in selected:
        by_conv[conv_index].append((conv_id, records, qa))

    print(f"{len(selected)} QA pairs across {len(by_conv)} conversation(s); "
          f"limit={args.limit}; configs={', '.join(names)}", file=sys.stderr)

    # results[label][question_id] = dict(hit, mrr, latency)
    results: dict[str, dict] = {label: {} for label, _ in recipes}
    categories: dict[str, str] = {}
    convs_used = Counter()

    for conv_index, items in sorted(by_conv.items()):
        conv_id, records = items[0][0], items[0][1]
        build_started = time.time()
        sub = create_substrate(f"diag-locomo-{conv_index}", records, embedder=embedder)
        broke = _preflight(sub, embedder, ctx, names)
        print(f"[{conv_id}] built {len(records)} records"
              f"{' with vectors' if embedder is not None else ' (lexical only)'} in "
              f"{time.time() - build_started:.0f}s, {len(items)} QA pairs"
              f"{'; PREFLIGHT FAILED: ' + broke if broke else ''}", file=sys.stderr)
        try:
            for qa_index, (_, _, qa) in enumerate(items):
                gold = qa.evidence_ids or None
                categories[qa.question_id] = qa.category or "unknown"
                if broke:
                    # A conversation whose retrieval is dead scores nothing. Recording it as misses
                    # would report an infrastructure failure as a retrieval regression.
                    for label, _kwargs in recipes:
                        results[label][qa.question_id] = {
                            "error": f"preflight: {broke}", "gold": bool(gold)}
                    convs_used[conv_id] += 1
                    continue
                for label, kwargs in recipes:
                    try:
                        recall_started = time.perf_counter()
                        recall_items, _receipt = recall(sub, qa.question, ctx,
                                                        limit=args.limit, **kwargs)
                        elapsed_ms = round((time.perf_counter() - recall_started) * 1000, 1)
                        from benchmarks.full.common import RecallResult
                        result = RecallResult(
                            question_id=qa.question_id, mode=label,
                            selected_ids=[it.record_id for it in recall_items],
                            selected_texts=[(it.text or "").lower() for it in recall_items],
                            latency_ms=elapsed_ms, abstained=not recall_items,
                        )
                        score = score_answer(qa.question_id, label, qa.gold_answer, result,
                                             qa.abstain, gold)
                        # hit@10 counts a question as retrieved when ANY gold record is in the top
                        # k. For multi-hop questions (276 of LoCoMo's 282 category-1 pairs carry two
                        # or more gold records) that overstates retrieval: one of the two facts is
                        # not the evidence. Coverage is the fraction of gold records retrieved, and
                        # full_recovery is all of them — the two numbers multi-hop actually wants.
                        retrieved = set(result.selected_ids[:args.limit])
                        gold_set = set(gold or [])
                        coverage = (len(gold_set & retrieved) / len(gold_set)) if gold_set else None
                        results[label][qa.question_id] = {
                            "hit": score.exact, "mrr": round(score.mrr, 4),
                            "latency_ms": elapsed_ms, "gold": bool(gold),
                            "gold_n": len(gold_set),
                            "coverage": None if coverage is None else round(coverage, 4),
                            "full_recovery": bool(gold_set) and gold_set <= retrieved,
                        }
                    except Exception as exc:
                        results[label][qa.question_id] = {
                            "error": f"{exc.__class__.__name__}: {exc}", "gold": bool(gold)}
                convs_used[conv_id] += 1
                if (qa_index + 1) % 25 == 0:
                    print(f"  ... {qa_index + 1}/{len(items)} in {conv_id} "
                          f"({time.time() - started_all:.0f}s total)", file=sys.stderr)
        finally:
            sub.close()

    rows = []
    for label, _ in recipes:
        scored = {qid: v for qid, v in results[label].items() if "error" not in v and v.get("gold")}
        # Failures are counted against the denominator, not dropped from it. An error is a question
        # the instrument could not score; a rate computed over the survivors hides that, and the
        # audit reproduced the hole: a config that failed one of two questions reported 100%.
        attempted = {qid: v for qid, v in results[label].items() if v.get("gold")}
        errors = sum(1 for v in attempted.values() if "error" in v)
        if not scored:
            rows.append({"config": label, "n": 0, "attempted": len(attempted),
                         "errors": errors})
            continue
        hits = sum(1 for v in scored.values() if v["hit"])
        mrr = sum(v["mrr"] for v in scored.values()) / len(scored)
        latencies = sorted(v["latency_ms"] for v in scored.values())
        low, high = wilson(hits, len(scored))
        by_cat = {}
        for qid, v in scored.items():
            bucket = by_cat.setdefault(categories.get(qid, "unknown"),
                                       {"n": 0, "hits": 0, "coverage_sum": 0.0, "full": 0})
            bucket["n"] += 1
            bucket["hits"] += 1 if v["hit"] else 0
            bucket["coverage_sum"] += v.get("coverage") or 0.0
            bucket["full"] += 1 if v.get("full_recovery") else 0
        for bucket in by_cat.values():
            bucket["hit_rate"] = round(bucket["hits"] / bucket["n"], 3)
            bucket["mean_coverage"] = round(bucket["coverage_sum"] / bucket["n"], 3)
            bucket["full_recovery_rate"] = round(bucket["full"] / bucket["n"], 3)
            bucket.pop("coverage_sum")
        coverage_mean = sum((v.get("coverage") or 0.0) for v in scored.values()) / len(scored)
        full_rate = sum(1 for v in scored.values() if v.get("full_recovery")) / len(scored)
        rows.append({
            "config": label, "n": len(scored), "attempted": len(attempted), "hits": hits,
            "k": args.limit,
            "hit_at_k": round(hits / len(scored), 4),
            "ci95": [round(low, 3), round(high, 3)],
            "mrr": round(mrr, 4),
            "p50_ms": latencies[len(latencies) // 2],
            "p95_ms": latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))],
            "mean_coverage": round(coverage_mean, 4),
            "full_recovery_rate": round(full_rate, 4),
            "errors": errors, "by_category": dict(sorted(by_cat.items())),
        })

    # Paired deltas against lexical: this is what the diagnosis is actually for. The pairing runs
    # over every gold-bearing question both configurations attempted, with a failed question counted
    # as a miss for that configuration and disclosed in `pairs_with_errors` — the previous version
    # intersected only the successful ones, so a config could fail the exact questions it lost and
    # report no losses at all.
    baseline = "lexical"
    base_entries = {qid: v for qid, v in results.get(baseline, {}).items() if v.get("gold")}

    def _as_hit(entry):
        return bool(entry.get("hit")) and "error" not in entry

    for row in rows:
        if row["config"] == baseline or not row["attempted"] or not base_entries:
            continue
        current = {qid: v for qid, v in results[row["config"]].items() if v.get("gold")}
        shared = set(base_entries) & set(current)
        failed = sum(1 for q in shared
                     if "error" in current[q] or "error" in base_entries[q])
        row["paired_vs_lexical"] = {
            "n": len(shared),
            "pairs_with_errors": failed,
            "lost": sum(1 for q in shared
                        if _as_hit(base_entries[q]) and not _as_hit(current[q])),
            "gained": sum(1 for q in shared
                          if _as_hit(current[q]) and not _as_hit(base_entries[q])),
        }

    print()
    width = max(len(r["config"]) for r in rows) + 2
    print("config".ljust(width) + f"N    err  hit@{args.limit}  95% CI         MRR     p50ms  "
                                 "coverage  full-recovery  vs lexical (lost/gained)")
    for row in rows:
        if not row["n"]:
            print(row["config"].ljust(width)
                  + f"0    {row.get('errors', 0):<4} —  (nothing scoreable)")
            continue
        paired = row.get("paired_vs_lexical") or {}
        note = ""
        if paired.get("pairs_with_errors"):
            note = f"  [{paired['pairs_with_errors']} pairs involved an error]"
        print(row["config"].ljust(width)
              + f"{row['n']:<5}{row['errors']:<5}{row['hit_at_k']:<9.3f}"
              + f"{row['ci95'][0]:.1%}-{row['ci95'][1]:.1%}   "
              + f"{row['mrr']:<8.3f}{row['p50_ms']:<7.1f}"
              + f"{row['mean_coverage']:<10.3f}{row['full_recovery_rate']:<15.1%}"
              + (f"{paired['lost']}/{paired['gained']} of {paired['n']}" if paired else "—")
              + note)
    total_errors = sum(r.get("errors", 0) for r in rows)
    print(f"\nN is gold-bearing questions actually scored; err is gold-bearing questions that could "
          f"not be scored. Elapsed {time.time() - started_all:.0f}s across "
          f"{sum(convs_used.values())} questions in {len(convs_used)} conversation(s)"
          f"{f'; {total_errors} errors' if total_errors else ''}.")

    if args.json:
        payload = {"offset": args.offset, "requested": args.n, "limit": args.limit,
                   "selection": selection_provenance,
                   "conversations": dict(convs_used), "rows": rows,
                   "categories": categories,
                   "per_question": {label: results[label] for label, _ in recipes}}
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0



if __name__ == "__main__":
    sys.exit(main())
