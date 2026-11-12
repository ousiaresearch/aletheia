#!/usr/bin/env python3
"""Why did hybrid lose a hit lexical found? One embedding pass, several recall configurations.

Building the vector index is the expensive part (~7 min/question at haystack scale), so it is done
once per question and then every mode runs against the same substrate. That makes the comparison
paired: same records, same vectors, same query, different pipeline.

    python benchmarks/diagnose_recall.py --offset 200 --n 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.full.common import create_substrate, make_ctx  # noqa: E402
from benchmarks.full.longmemeval import _convert_question, _load_questions  # noqa: E402
from aletheia.retrieval import recall  # noqa: E402


def _contains(texts, answer: str) -> bool:
    needle = (answer or "").strip().lower()
    if not needle:
        return False
    return any(needle in (t or "").lower() for t in texts)


def _recipes(embedder):
    """(label, kwargs) for every recall configuration worth telling apart."""
    recipes = [
        ("lexical", dict(mode="lexical")),
        ("lexical+rerank", dict(mode="lexical", embedder=embedder, rerank=True)),
        ("vector", dict(mode="vector", embedder=embedder, rerank=False)),
        ("vector+rerank", dict(mode="vector", embedder=embedder, rerank=True)),
        ("hybrid-noRerank", dict(mode="hybrid", embedder=embedder, rerank=False)),
        ("hybrid+rerank", dict(mode="hybrid", embedder=embedder, rerank=True)),
    ]
    # The cross-encoder is opt-in through recall's own flag; if its weights are not on this machine
    # the recall raises inside the recipe and the table shows `err` rather than pretending to score.
    recipes.append(("hybrid+crossencoder",
                    dict(mode="hybrid", embedder=embedder, use_cross_encoder=True)))
    return recipes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offset", type=int, default=200)
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    from aletheia.adapters import default_text_embedder

    embedder = default_text_embedder()
    recipes = _recipes(embedder)
    ctx = make_ctx()
    entries = _load_questions()[args.offset:args.offset + args.n]

    rows = []
    for index, entry in enumerate(entries):
        conv_id, records, qa = _convert_question(entry)
        started = time.time()
        sub = create_substrate(f"diag-{args.offset}-{index}", records, embedder=embedder)
        print(f"[{index}] built {len(records)} records with vectors in {time.time() - started:.0f}s",
              file=sys.stderr)
        try:
            row = {"question": qa.question, "answer": qa.gold_answer,
                   "answer_type": getattr(qa, "answer_type", None), "configs": {}}
            for label, kwargs in recipes:
                try:
                    started_recall = time.perf_counter()
                    items, receipt = recall(sub, qa.question, ctx, limit=args.limit, **kwargs)
                    elapsed_ms = round((time.perf_counter() - started_recall) * 1000, 1)
                    texts = [it.text for it in items]
                    row["configs"][label] = {
                        "hit": _contains(texts, qa.gold_answer),
                        "latency_ms": elapsed_ms,
                        "layers": list(getattr(receipt, "layers", []) or []),
                        "top3": [t[:120] for t in texts[:3]],
                        "n_items": len(items),
                    }
                except Exception as exc:
                    row["configs"][label] = {"error": f"{exc.__class__.__name__}: {exc}"}
            rows.append(row)
        finally:
            sub.close()

    print()
    labels = [label for label, _ in recipes]
    header = "config".ljust(20) + "".join(f"q{i + args.offset:<4}" for i in range(len(rows)))
    print(header)
    for label in labels:
        marks = []
        for row in rows:
            got = row["configs"].get(label, {})
            marks.append("HIT " if got.get("hit") else ("err " if "error" in got else "miss"))
        print(label.ljust(20) + "".join(m.ljust(5) for m in marks))

    if args.json:
        Path(args.json).write_text(json.dumps({"offset": args.offset, "rows": rows}, indent=2),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())