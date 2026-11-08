#!/usr/bin/env python3
"""Full Phase 9 benchmark with hybrid vector recall on Colab A100."""
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
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vecs = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [[float(x) for x in v] for v in vecs]
    def embed_document(self, text: str) -> list:
        return self.embed_query(text)

embedder = STEmbedder(model)
print(f"[setup] Wrapper: {embedder.model_id}")

from benchmarks.full.locomo import run as run_locomo  # noqa: E402
from benchmarks.full.longmemeval import run as run_longmemeval  # noqa: E402
from benchmarks.full.beam import run as run_beam  # noqa: E402

results = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "embedder": embedder.model_id,
    "hardware": {
        "gpu": torch.cuda.get_device_name(0),
        "vram_gb": torch.cuda.get_device_properties(0).total_memory / 1e9,
    },
    "suites": {},
}

print("\n[run] === LoCoMo hybrid ===")
t0 = time.time()
results["suites"]["locomo-hybrid"] = run_locomo(embedder=embedder)
print(f"[run] LoCoMo hybrid done in {time.time()-t0:.1f}s")

print("\n[run] === LongMemEval-S hybrid ===")
t0 = time.time()
results["suites"]["longmemeval-s-hybrid"] = run_longmemeval(embedder=embedder)
print(f"[run] LongMemEval-S hybrid done in {time.time()-t0:.1f}s")

print("\n[run] === BEAM 100K hybrid ===")
t0 = time.time()
results["suites"]["beam-100k-hybrid"] = run_beam(splits=["100K"], embedder=embedder)
print(f"[run] BEAM 100K hybrid done in {time.time()-t0:.1f}s")

(REPO / "benchmarks" / "hybrid-results.json").write_text(json.dumps(results, indent=2) + "\n")
print("\n[setup] hybrid-results.json written")
