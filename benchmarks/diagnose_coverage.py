#!/usr/bin/env python3
"""Why is complete-evidence recovery only ~12% on multi-record questions?

Two candidate causes, and they need different fixes:

  * **diversity** — the missed gold record sits in a session the top-k already covers, so the slots
    went to near-duplicates of evidence we already had. Fix: coverage-aware selection (spread slots
    across regions instead of returning ten variations of the best match).
  * **depth/ranking** — the missed gold record is in a session the top-k never touched at all. Fix:
    a better or deeper candidate pool, not diversification.

Writes a per-question JSON plus a summary, so the next change is chosen from evidence rather than from
which fix sounds more sophisticated.

    python benchmarks/diagnose_coverage.py --n 1986 --limit 10 --json coverage-analysis.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.full.common import create_substrate, make_ctx  # noqa: E402
from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402


def _session_of(records) -> dict[str, str]:
    """{record_id: session} — the region a record belongs to, from the converter's provenance."""
    out = {}
    for rec in records:
        prov = rec.provenance if isinstance(rec.provenance, dict) else {}
        out[rec.record_id] = str(prov.get("session") or "?")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=None, help="QA pairs (default: all)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    from aletheia.retrieval import recall

    ctx = make_ctx()
    rows = []
    started = time.time()
    for conversation in _load_dataset():
        conv_id, records, qas = convert_sample(dict(conversation))
        if not records or not qas:
            continue
        session_of = _session_of(records)
        sub = create_substrate(f"cov-{conv_id}", records)
        try:
            for qa in qas:
                gold = list(qa.evidence_ids or [])
                if len(gold) < 2:
                    continue  # single-record questions cannot lose coverage this way
                items, _receipt = recall(sub, qa.question, ctx, limit=args.limit, mode="lexical")
                got = [it.record_id for it in items]
                got_set, gold_set = set(got), set(gold)
                sessions_seen = {session_of.get(r, "?") for r in got}
                missed = [g for g in gold if g not in got_set]
                rows.append({
                    "question_id": qa.question_id,
                    "category": qa.category,
                    "gold_n": len(gold),
                    "gold": gold,
                    "retrieved": got,
                    "recovered": len(gold_set & got_set),
                    "missed": missed,
                    "missed_in_seen_session": sum(
                        1 for m in missed if session_of.get(m, "?") in sessions_seen),
                    "missed_in_new_session": sum(
                        1 for m in missed if session_of.get(m, "?") not in sessions_seen),
                    "distinct_sessions_in_topk": len(sessions_seen),
                })
        finally:
            sub.close()
        if args.n is not None and len(rows) >= args.n:
            break

    if args.n is not None:
        rows = rows[: args.n]

    full = [r for r in rows if r["recovered"] == r["gold_n"]]
    missed_total = sum(len(r["missed"]) for r in rows)
    in_seen = sum(r["missed_in_seen_session"] for r in rows)
    in_new = sum(r["missed_in_new_session"] for r in rows)
    by_cat = defaultdict(lambda: {"n": 0, "full": 0, "missed": 0, "seen": 0, "new": 0})
    for r in rows:
        b = by_cat[str(r["category"])]
        b["n"] += 1
        b["full"] += 1 if r["recovered"] == r["gold_n"] else 0
        b["missed"] += len(r["missed"])
        b["seen"] += r["missed_in_seen_session"]
        b["new"] += r["missed_in_new_session"]

    summary = {
        "questions": len(rows),
        "limit": args.limit,
        "full_recovery": round(len(full) / len(rows), 3) if rows else None,
        "missed_gold_records": missed_total,
        "missed_in_session_already_in_topk": in_seen,
        "missed_in_session_never_reached": in_new,
        "share_diversity_fixable": round(in_seen / missed_total, 3) if missed_total else None,
        "mean_distinct_sessions_in_topk": round(
            sum(r["distinct_sessions_in_topk"] for r in rows) / len(rows), 2) if rows else None,
        "by_category": {k: dict(v) for k, v in sorted(by_cat.items())},
        "elapsed_s": round(time.time() - started, 1),
        "interpretation": ("A large share_diversity_fixable means the slots go to near-duplicates and "
                           "coverage-aware selection is the fix; a small one means the missed evidence "
                           "is in regions the candidate pool never reached, so depth or ranking is."),
    }

    print(f"{len(rows)} multi-record questions at depth {args.limit}")
    print(f"  full recovery            {len(full)}/{len(rows)} = "
          f"{len(full)/len(rows):.1%}" if rows else "")
    print(f"  missed gold records      {missed_total}")
    print(f"    in a session already in the top-{args.limit}   {in_seen}"
          f"  ({in_seen/missed_total:.1%} of misses)" if missed_total else "")
    print(f"    in a session never reached     {in_new}"
          f"  ({in_new/missed_total:.1%} of misses)" if missed_total else "")
    print(f"  distinct sessions per top-{args.limit}: mean {summary['mean_distinct_sessions_in_topk']}")
    print("\n  by category: n  full  missed  seen  new")
    for cat, b in summary["by_category"].items():
        print(f"    {cat:4} {b['n']:5} {b['full']:5} {b['missed']:7} {b['seen']:5} {b['new']:4}")

    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())