#!/usr/bin/env python3
"""Would entity co-mention reach the evidence that lexical and vector retrieval both miss?

The coverage diagnostics established a wall: ~75% of missed gold records sit in sessions the top-k never
reached, and adding the vector arm moved that share not at all (74.2% -> 74.9%). The hypothesis behind
structured retrieval is that those records are reachable through a shared *entity* — a person, a place,
a topic — rather than through similarity to the question.

This measures the hypothesis before anything is built on it. For every multi-record question it takes
the top-k, collects the entities those records mention (from `entity_links`), and asks how many missed
gold records mention an entity the retrieved set already mentions. A low rate means co-mention traversal
is the wrong mechanism and the graph work should stop here.

Entities are filtered at query time, because `extract_entities` keeps any capitalised word longer than
two characters: without a filter the bridge would be "that" or "thanks". Two filters are applied — a
common-word list, and a document-frequency ceiling, since an entity mentioned in most records carries no
selectivity.

    python benchmarks/diagnose_hops.py --limit 10 --json hop-analysis.json
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

from benchmarks.full.common import create_substrate, make_ctx  # noqa: E402
from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402

#: Words a capitalised-token extractor mistakes for entities. Kept short deliberately: the point is to
#: remove discourse noise ("thanks", "wow", "that"), not to build a lexicon.
_COMMON = frozenset("""
a about after again all also am an and any are as at be because been before being but by can could did
do does doing don down during each even for from further had has have having he her here hers herself him
himself his how i if in into is it its itself just me more most my myself no nor not now of off on once
only or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your yours yourself thanks wow yes yeah okay ok
hi hey hello oh ah um well really think thought know know right sure good great nice
""".split())


def _entities_by_record(sub, *, df_ceiling: float, readable) -> tuple[dict, dict]:
    """{record_id: set(entity)} and {entity: document frequency}, from the substrate's entity links."""
    rows = sub._conn.execute(
        "SELECT source_id, target_id FROM entity_links WHERE scope IN (%s)"
        % ",".join("?" for _ in readable), tuple(readable)).fetchall()
    by_record: dict[str, set] = defaultdict(set)
    df: Counter = Counter()
    for record_id, entity in rows:
        if entity in _COMMON or len(entity) < 3:
            continue
        by_record[record_id].add(entity)
        df[entity] += 1
    total = max(len(by_record), 1)
    ceiling = max(2, int(df_ceiling * total))
    for record_id, entities in list(by_record.items()):
        by_record[record_id] = {e for e in entities if df[e] <= ceiling}
    return by_record, dict(df)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--df-ceiling", type=float, default=0.6,
                        help="drop entities mentioned in more than this share of records")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    from aletheia.retrieval import recall

    ctx = make_ctx()
    started = time.time()
    rows = []
    for conversation in _load_dataset():
        conv_id, records, qas = convert_sample(dict(conversation))
        if not records or not qas:
            continue
        readable = sorted(make_ctx().granted_scopes)
        sub = create_substrate(f"hop-{conv_id}", records)
        try:
            by_record, _df = _entities_by_record(sub, df_ceiling=args.df_ceiling,
                                                 readable=readable)
            for qa in qas:
                gold = list(qa.evidence_ids or [])
                if len(gold) < 2:
                    continue
                items, _receipt = recall(sub, qa.question, ctx, limit=args.limit, mode="lexical")
                got = [it.record_id for it in items]
                got_set = set(got)
                missed = [g for g in gold if g not in got_set]
                if not missed:
                    rows.append({"question_id": qa.question_id, "gold_n": len(gold),
                                 "missed": 0, "reachable": 0})
                    continue
                seen_entities: set = set()
                for record_id in got:
                    seen_entities |= by_record.get(record_id, set())
                reachable = [m for m in missed if by_record.get(m, set()) & seen_entities]
                rows.append({
                    "question_id": qa.question_id,
                    "category": qa.category,
                    "gold_n": len(gold),
                    "missed": len(missed),
                    "reachable": len(reachable),
                    "bridge_entities": sorted(
                        {e for m in reachable for e in (by_record.get(m, set()) & seen_entities)},
                        key=str)[:6],
                })
        finally:
            sub.close()

    missed_total = sum(r["missed"] for r in rows)
    reachable_total = sum(r["reachable"] for r in rows)
    partial = [r for r in rows if r["missed"] and r["reachable"]]
    by_cat = defaultdict(lambda: {"n": 0, "missed": 0, "reachable": 0})
    for r in rows:
        b = by_cat[str(r.get("category"))]
        b["n"] += 1
        b["missed"] += r["missed"]
        b["reachable"] += r["reachable"]

    summary = {
        "questions": len(rows),
        "limit": args.limit,
        "df_ceiling": args.df_ceiling,
        "missed_gold_records": missed_total,
        "reachable_by_comention": reachable_total,
        "share_reachable": round(reachable_total / missed_total, 3) if missed_total else None,
        "questions_with_any_reachable_miss": len(partial),
        "by_category": {k: dict(v) for k, v in sorted(by_cat.items())},
        "elapsed_s": round(time.time() - started, 1),
        "interpretation": ("share_reachable is the fraction of missed gold records that mention an "
                           "entity the top-k already mentions. That is the ceiling for co-mention "
                           "traversal: if it is small, structured retrieval through entities cannot "
                           "close the coverage gap and should not be built on this evidence."),
    }

    print(f"{len(rows)} multi-record questions, depth {args.limit}, "
          f"entity df ceiling {args.df_ceiling:.0%}")
    print(f"  missed gold records            {missed_total}")
    print(f"  reachable via a shared entity  {reachable_total}"
          f"  ({summary['share_reachable']:.1%})" if missed_total else "")
    print(f"  questions with >=1 reachable miss: {len(partial)}/{len(rows)}")
    print("\n  by category:      n  missed  reachable")
    for cat, b in summary["by_category"].items():
        print(f"    {cat:6} {b['n']:7} {b['missed']:7} {b['reachable']:10}")
    examples = [r for r in rows if r["reachable"]][:5]
    for r in examples:
        print(f"  e.g. {r['question_id'][-18:]}: bridge entities {r['bridge_entities']}")

    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())