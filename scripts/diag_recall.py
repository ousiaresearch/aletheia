#!/usr/bin/env python3
"""Diagnostic: compare lexical vs hybrid recall on a few LoCoMo samples."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path("/Users/johannross/workspace/aletheia")
sys.path.insert(0, str(REPO))

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall  # noqa: E402
from aletheia.scope import ScopeEngine  # noqa: E402

# Load a small sample
ds = _load_dataset()
sample = ds[0]
conv_id, records, qa_pairs = convert_sample(sample)
print(f"Conv {conv_id}: {len(records)} records, {len(qa_pairs)} QA pairs")

scopes = {r.scope for r in records}
engine = ScopeEngine(known_scopes=scopes)

# Create substrate without embedder (lexical only)
sub_lex = create_substrate(conv_id, records, engine=engine, embedder=None)

# Test first 3 QA pairs
for qa in qa_pairs[:3]:
    result_lex = run_recall(sub_lex, qa.question, embedder=None)
    print(f"\nQ: {qa.question[:80]}")
    print(f"Gold: {str(qa.gold_answer)[:80]}")
    print(f"Lexical ({len(result_lex.selected_texts)} docs): {result_lex.selected_texts[0][:100] if result_lex.selected_texts else 'NONE'}")

sub_lex.close()
