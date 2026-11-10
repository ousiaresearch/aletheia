"""Pilot: does the fact layer populate usefully from LoCoMo dialogue, and what does it cost?

`extract_facts` is LLM-based (one call per record, fails closed without a provider), and the benchmark
has never called it. Ten conversations hold ~4,500 records, so full extraction is a real job. This
extracts one conversation first and reports what comes back so the decision is made on evidence:

  - how many claims per record the extractor returns (and how many records yield none)
  - a sample of claims, to judge whether they are facts or noise
  - the wall-clock cost per record, which extrapolates to the full set

Run with the same provider environment as the benchmark:

    .venv/bin/python -u benchmarks/pilot_facts.py --conv 0 --limit 25
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--conv", type=int, default=0, help="which conversation index to pilot")
    p.add_argument("--limit", type=int, default=25, help="how many records to extract from it")
    p.add_argument("--json", default=str(Path("benchmarks") / "pilot-facts.json"))
    args = p.parse_args(argv)

    conversations = list(_load_dataset())
    conversation = conversations[args.conv]
    conv_id, records, qas = convert_sample(dict(conversation))
    print(f"conversation {conv_id}: {len(records)} records, {len(qas)} questions")
    print(f"extracting from the first {args.limit} records\n")

    from aletheia.extraction import _extract_facts, external_allowed  # the LLM extractor, fail-closed

    # `_extract_facts` returns [] both when the operator has not opted in and when it genuinely finds
    # nothing. Reporting the gate separately is the difference between "the extractor is useless here"
    # and "the extractor never ran".
    if not external_allowed():
        print("!! extraction is gated off: set ALETHEIA_EXTRACT_ALLOW_EXTERNAL=1 (scope text goes to the "
              "configured endpoint). Everything below would read as 'no facts found'.\n")

    started = time.time()
    per_record: list[dict] = []
    empty = 0
    total_claims = 0
    for i, record in enumerate(records[: args.limit]):
        # Substrate records carry their text inside `payload`; there is no `.text` attribute. Reading
        # `getattr(record, "text", "")` yields '' for every record, and `_extract_facts('')` returns []
        # silently — which reads as "the extractor finds nothing" rather than "the probe fed it nothing".
        text = ""
        if isinstance(getattr(record, "payload", None), dict):
            text = str(record.payload.get("text") or "")
        if not text:
            text = getattr(record, "text", "") or ""
        facts = _extract_facts(text, "pilot")
        if not facts:
            empty += 1
        total_claims += len(facts)
        per_record.append({"record_id": getattr(record, "record_id", None),
                           "text": text[:160], "n_facts": len(facts),
                           "facts": facts[:6]})
        if (i + 1) % 5 == 0:
            elapsed = time.time() - started
            print(f"  {i + 1}/{args.limit} records, {total_claims} claims, "
                  f"{elapsed / (i + 1):.2f}s/record")

    elapsed = time.time() - started
    rate = elapsed / max(len(per_record), 1)
    summary = {
        "conversation": conv_id,
        "records_in_conversation": len(records),
        "records_extracted": len(per_record),
        "claims_total": total_claims,
        "claims_per_record": round(total_claims / max(len(per_record), 1), 2),
        "records_with_no_claims": empty,
        "empty_rate": round(empty / max(len(per_record), 1), 3),
        "seconds_per_record": round(rate, 2),
        "projected_seconds_all_records": round(rate * len(records)),
        "projected_minutes_ten_conversations": round(rate * 4500 / 60, 1),
        "samples": per_record[:12],
    }
    Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nclaims: {total_claims} over {len(per_record)} records "
          f"({summary['claims_per_record']}/record), {empty} records yielded nothing")
    print(f"cost: {rate:.2f}s/record -> this conversation ~{rate * len(records) / 60:.1f} min, "
          f"all ten ~{summary['projected_minutes_ten_conversations']} min")
    print("\nsample claims:")
    for row in per_record[:8]:
        if row["facts"]:
            f = row["facts"][0]
            print(f"  {row['record_id']}: ({f.get('subject')}) --{f.get('predicate')}--> "
                  f"({str(f.get('object'))[:50]})")
        else:
            print(f"  {row['record_id']}: no claims from {row['text'][:70]!r}")
    print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())