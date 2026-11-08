#!/usr/bin/env python3
"""Quick LoCoMo hybrid test on Colab A100 with progress logging."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/content/aletheia")

def run(cmd: str, check: bool = True) -> str:
    print(f"[setup] {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"[setup] FAILED: {cmd}\n{r.stderr}")
        sys.exit(r.returncode)
    out = r.stdout.strip()
    if out:
        print(out[:500])
    return out

if not REPO.exists():
    run("git clone https://github.com/ousiaresearch/aletheia.git /content/aletheia")
os.chdir(REPO)
sys.path.insert(0, str(REPO))

run("pip install -e . -q")
run("pip install datasets pytest -q")
run("pip install sentence-transformers torch --index-url https://download.pytorch.org/whl/cu124 -q")

import torch  # noqa: E402
print(f"[setup] GPU: {torch.cuda.get_device_name(0)}")
print(f"[setup] VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

from sentence_transformers import SentenceTransformer  # noqa: E402
print("[setup] Loading BGE-M3 via sentence-transformers...")
model = SentenceTransformer('BAAI/bge-m3', device='cuda')
print(f"[setup] Model loaded, dim={model.get_embedding_dimension()}")

class STEmbedder:
    def __init__(self, model):
        self.model = model
        self.model_id = f"SentenceTransformer({model.tokenizer.name_or_path})"
    def embed_query(self, text: str) -> list:
        vec = self.model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
        return [float(x) for x in vec]
    def embed_document(self, text: str) -> list:
        return self.embed_query(text)

embedder = STEmbedder(model)
print(f"[setup] Wrapper: {embedder.model_id}")

# Test embed
t0 = time.time()
v = embedder.embed_query("test")
print(f"[setup] Embed test: {time.time()-t0:.3f}s, dim={len(v)}")

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall, score_answer  # noqa: E402
from aletheia.scope import ScopeEngine  # noqa: E402

print("\n[run] Loading LoCoMo dataset...")
ds = _load_dataset()
print(f"[run] Loaded {len(ds)} conversations")

# Run only first 2 conversations as POC
scores = []
for i, sample in enumerate(ds):
    if i >= 2:
        break
    conv_id, records, qa_pairs = convert_sample(sample)
    if not records or not qa_pairs:
        print(f"[run] Skipping conv {i}: no records or qa")
        continue
    print(f"[run] Conv {i}: {len(records)} records, {len(qa_pairs)} QA pairs")
    
    scopes = {r.scope for r in records}
    engine = ScopeEngine(known_scopes=scopes)
    sub = create_substrate(conv_id, records, engine=engine, embedder=embedder)
    ctx = make_ctx()
    try:
        for qa in qa_pairs:
            lex_res = run_recall(sub, qa.question, embedder=None)
            lex_score = score_answer(qa.question_id, "lexical", qa.gold_answer, lex_res, qa.abstain,
                                     gold_record_ids=qa.evidence_ids)
            scores.append(lex_score)
            if embedder is not None:
                hyb_res = run_recall(sub, qa.question, embedder=embedder)
                hyb_score = score_answer(qa.question_id, "hybrid", qa.gold_answer, hyb_res, qa.abstain,
                                         gold_record_ids=qa.evidence_ids)
                scores.append(hyb_score)
    finally:
        sub.close()
    print(f"[run] Conv {i} done")

print(f"\n[run] Total scores: {len(scores)}")
# Convert ScoreResult to dict for JSON
import dataclasses
score_dicts = [dataclasses.asdict(s) for s in scores[:3]]
print(json.dumps(score_dicts, indent=2))
