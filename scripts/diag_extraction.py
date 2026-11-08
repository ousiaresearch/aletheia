#!/usr/bin/env python3
"""Diagnostic: check extraction quality on a few LoCoMo samples."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path("/Users/johannross/workspace/aletheia")
sys.path.insert(0, str(REPO))

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402
from benchmarks.full.common import make_engine, make_ctx, create_substrate  # noqa: E402
from aletheia.scope import ScopeEngine  # noqa: E402
from aletheia.extraction import extract_facts, write_claims  # noqa: E402

# Load a small sample
ds = _load_dataset()
sample = ds[0]
conv_id, records, qa_pairs = convert_sample(sample)
print(f"Conv {conv_id}: {len(records)} records, {len(qa_pairs)} QA pairs")

scopes = {r.scope for r in records}
engine = ScopeEngine(known_scopes=scopes)
sub = create_substrate(conv_id, records, engine=engine, embedder=None)
ctx = make_ctx()

# Count records that can be extracted from
src_count = sub._conn.execute(
    "SELECT COUNT(*) FROM records WHERE scope = ? AND record_type IN ('Observation', 'Source')",
    ("benchmark",)
).fetchone()[0]
print(f"Extractable records in substrate: {src_count}")

# Run extraction
claims = extract_facts(sub, ctx, "benchmark", limit=50)
print(f"Extracted {len(claims)} claims")

# Write claims
written = write_claims(sub, ctx, claims)
print(f"Written {written} claims")

# Show a few claims
if claims:
    print("\nSample claims:")
    for c in claims[:5]:
        print(f"  {c.payload.get('text')} (type: {c.payload.get('fact_type')}, conf: {c.payload.get('confidence')})")

# Now test a QA pair
qa = qa_pairs[0]
print(f"\nQ: {qa.question}")
print(f"Gold: {qa.gold_answer}")

# Count total records after extraction
total = sub._conn.execute("SELECT COUNT(*) FROM records WHERE scope = ?", ("benchmark",)).fetchone()[0]
print(f"Total records after extraction: {total}")

sub.close()
