"""Extract facts for every LoCoMo record, in parallel, resumably.

`_extract_facts` is one LLM call per record at ~21.8 s/record measured, so all ten conversations (~4,500
records) are ~27 hours serial. This runs them concurrently and caches each result, so an interrupted run
resumes instead of restarting.

The cache is append-only JSONL keyed by record_id, written under a lock. Nothing here writes to a
substrate — it only produces the fact cache that `benchmarks/facts.py` then turns into claim records.

    ALETHEIA_EXTRACT_ALLOW_EXTERNAL=1 .venv/bin/python -u benchmarks/extract_facts_all.py --workers 10

Interrupting is safe: rerunning skips every record_id already in the cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402

CACHE_DIR = Path("benchmarks") / "facts-cache"


def _record_text(record) -> str:
    """Substrate records keep their text in `payload`; there is no `.text` attribute."""
    payload = getattr(record, "payload", None)
    if isinstance(payload, dict):
        text = str(payload.get("text") or "")
        if text:
            return text
    return getattr(record, "text", "") or ""


def _load_done(cache_path: Path) -> set[str]:
    done: set[str] = set()
    if not cache_path.exists():
        return done
    with cache_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rid = row.get("record_id")
            if rid:
                done.add(str(rid))
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--limit", type=int, default=None, help="cap total records (for a dry run)")
    p.add_argument("--conv", type=int, default=None, help="only this conversation index")
    args = p.parse_args(argv)

    from aletheia.extraction import _extract_facts, external_allowed

    if not external_allowed():
        print("refusing to run: ALETHEIA_EXTRACT_ALLOW_EXTERNAL=1 is not set. Without it every call "
              "returns [] silently and the cache would fill with zero-fact records.", file=sys.stderr)
        return 2

    conversations = list(_load_dataset())
    if args.conv is not None:
        conversations = [conversations[args.conv]]

    # Build the work list across all conversations first, so progress is one number.
    work: list[tuple[str, str, str]] = []  # (record_id, text, conv_id)
    for conversation in conversations:
        conv_id, records, _ = convert_sample(dict(conversation))
        for record in records:
            text = _record_text(record)
            rid = str(getattr(record, "record_id", "") or "")
            if text and rid:
                work.append((rid, text, conv_id))
    if args.limit:
        work = work[: args.limit]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "facts.jsonl"
    done = _load_done(cache_path)
    todo = [w for w in work if w[0] not in done]

    print(f"records total {len(work)}, already cached {len(done & {w[0] for w in work})}, "
          f"to extract {len(todo)}, workers {args.workers}", flush=True)
    if not todo:
        print("nothing to do", flush=True)
        return 0

    lock = threading.Lock()
    errors = 0
    zero = 0
    claims = 0
    started = time.time()
    fh = cache_path.open("a", encoding="utf-8")

    def _one(item):
        rid, text, conv_id = item
        try:
            facts = _extract_facts(text, conv_id)
        except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
            return rid, conv_id, None, f"{type(exc).__name__}: {exc}"
        return rid, conv_id, facts, None

    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(_one, item) for item in todo]
            for fut in as_completed(futures):
                rid, conv_id, facts, err = fut.result()
                with lock:
                    completed += 1
                    if err:
                        errors += 1
                        row = {"record_id": rid, "conversation": conv_id, "facts": [], "error": err}
                    else:
                        if not facts:
                            zero += 1
                        claims += len(facts or [])
                        row = {"record_id": rid, "conversation": conv_id, "facts": facts or []}
                    fh.write(json.dumps(row) + "\n")
                    fh.flush()
                    if completed % 100 == 0:
                        elapsed = time.time() - started
                        rate = elapsed / completed
                        remaining = len(todo) - completed
                        print(f"  {completed}/{len(todo)}  {claims} claims  {zero} empty  "
                              f"{errors} errors  {rate:.1f}s/record  "
                              f"~{remaining * rate / 60:.0f} min left", flush=True)
    finally:
        fh.close()

    elapsed = time.time() - started
    print(f"\ndone: {completed} records in {elapsed / 60:.1f} min, {claims} claims, "
          f"{zero} records with none, {errors} errors")
    print(f"cache: {cache_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())