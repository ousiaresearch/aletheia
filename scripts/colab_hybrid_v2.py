#!/usr/bin/env python3
"""Full hybrid benchmark on Colab A100 — conversation-by-conversation with checkpoints."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/content/aletheia")

def run(cmd: str, check: bool = True) -> str:
    print(f"[setup] {cmd}", flush=True)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"[setup] FAILED: {cmd}\n{r.stderr}", flush=True)
        sys.exit(r.returncode)
    out = r.stdout.strip()
    if out:
        print(out[:500], flush=True)
    return out

if not REPO.exists():
    run("git clone https://github.com/ousiaresearch/aletheia.git /content/aletheia")
os.chdir(REPO)
sys.path.insert(0, str(REPO))

run("pip install -e . -q")
run("pip install datasets pytest -q")
run("pip install sentence-transformers torch --index-url https://download.pytorch.org/whl/cu124 -q")

import torch  # noqa: E402
print(f"[setup] GPU: {torch.cuda.get_device_name(0)}", flush=True)
print(f"[setup] VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB", flush=True)

from sentence_transformers import SentenceTransformer  # noqa: E402
print("[setup] Loading BGE-M3...", flush=True)
model = SentenceTransformer('BAAI/bge-m3', device='cuda')
print(f"[setup] Model loaded, dim={model.get_embedding_dimension()}", flush=True)

class STEmbedder:
    def __init__(self, model):
        self.model = model
        self.model_id = f"SentenceTransformer({model.tokenizer.name_or_path})"
    def embed_query(self, text: str) -> list:
        vec = self.model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
        return [float(x) for x in vec]
    def embed_documents(self, texts: list[str], batch_size: int = 256) -> list[list[float]]:
        all_vecs = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            vecs = self.model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
            all_vecs.extend([[float(x) for x in v] for v in vecs])
        return all_vecs
    def embed_document(self, text: str) -> list:
        return self.embed_query(text)

embedder = STEmbedder(model)
print(f"[setup] Wrapper: {embedder.model_id}", flush=True)

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall, score_answer  # noqa: E402
from aletheia.scope import ScopeEngine  # noqa: E402

print("\n[run] Loading LoCoMo dataset...", flush=True)
ds = _load_dataset()
print(f"[run] Loaded {len(ds)} conversations", flush=True)

results = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "embedder": embedder.model_id,
    "suites": {},
}

# Run each conversation individually
all_scores = []
for i, sample in enumerate(ds):
    conv_id, records, qa_pairs = convert_sample(sample)
    if not records or not qa_pairs:
        print(f"[run] Conv {i}: skipping (no records/qa)", flush=True)
        continue
    
    print(f"[run] Conv {i}: {len(records)} records, {len(qa_pairs)} QA pairs", flush=True)
    
    scopes = {r.scope for r in records}
    engine = ScopeEngine(known_scopes=scopes)
    sub = create_substrate(conv_id, records, engine=engine, embedder=embedder)
    ctx = make_ctx()
    
    conv_scores = []
    try:
        for qa in qa_pairs:
            lex_res = run_recall(sub, qa.question, embedder=None)
            lex_score = score_answer(qa.question_id, "lexical", qa.gold_answer, lex_res, qa.abstain,
                                     gold_record_ids=qa.evidence_ids)
            conv_scores.append(lex_score)
            
            hyb_res = run_recall(sub, qa.question, embedder=embedder)
            hyb_score = score_answer(qa.question_id, "hybrid", qa.gold_answer, hyb_res, qa.abstain,
                                     gold_record_ids=qa.evidence_ids)
            conv_scores.append(hyb_score)
    finally:
        sub.close()
    
    all_scores.extend(conv_scores)
    print(f"[run] Conv {i} done — {len(conv_scores)} scores", flush=True)
    
    # Checkpoint after each conversation
    ckpt = {
        "conversations_done": i + 1,
        "total_scores": len(all_scores),
        "last_conv_id": conv_id,
    }
    (REPO / "benchmarks" / "hybrid-checkpoint.json").write_text(json.dumps(ckpt, indent=2) + "\n")

print(f"\n[run] Total scores: {len(all_scores)}")
import dataclasses
score_dicts = [dataclasses.asdict(s) for s in all_scores[:3]]
print(json.dumps(score_dicts, indent=2))
