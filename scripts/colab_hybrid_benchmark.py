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
CACHE = REPO / "benchmarks" / "full" / "cache"

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

# Clone repo
if not (REPO / ".git").exists():
    run("git clone https://github.com/ousiaresearch/aletheia.git /content/aletheia")
    os.chdir(REPO)
else:
    os.chdir(REPO)
    run("git pull --rebase")

# Install
run("pip install -e . -q")
run("pip install datasets pytest -q")

# Verify imports
print("[setup] Verifying imports...")
import aletheia  # noqa: E402
from benchmarks.full.locomo import run as run_locomo  # noqa: E402
from benchmarks.full.longmemeval import run as run_longmemeval  # noqa: E402
from benchmarks.full.beam import run as run_beam  # noqa: E402
print("[setup] imports OK")

# Hardware audit
try:
    import torch  # noqa: E402
    print(f"[setup] GPU: {torch.cuda.get_device_name(0)}")
    print(f"[setup] VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
    print(f"[setup] CUDA: {torch.version.cuda}")
except Exception as e:
    print(f"[setup] torch not available: {e}")

# Init embedder for hybrid
from aletheia.adapters import BgeM3Embedder  # noqa: E402
print("[setup] Loading BGE-M3 embedder...")
model_dir = (
    Path.home()
    / ".cache/huggingface/hub/models--mlx-community--bge-m3-mlx-4bit/snapshots/0a93c12b0684953bdaed14865dc270bb1894a749"
)
embedder = BgeM3Embedder(str(model_dir))
print(f"[setup] Embedder loaded: {embedder.model_id}")

# Ensure cache dir exists
CACHE.mkdir(parents=True, exist_ok=True)

results = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "embedder": embedder.model_id,
    "hardware": {
        "gpu": torch.cuda.get_device_name(0),
        "vram_gb": torch.cuda.get_device_properties(0).total_memory / 1e9,
        "cuda": torch.version.cuda,
    },
    "suites": {},
}

# Run lexical-only first (fast baseline)
print("\n[run] === lexical baseline: LoCoMo ===")
t0 = time.time()
results["suites"]["locomo-lexical"] = run_locomo(embedder=None)
print(f"[run] locomo-lexical done in {time.time()-t0:.1f}s")

print("\n[run] === lexical baseline: LongMemEval-S ===")
t0 = time.time()
results["suites"]["longmemeval-s-lexical"] = run_longmemeval()
print(f"[run] longmemeval-s-lexical done in {time.time()-t0:.1f}s")

print("\n[run] === lexical baseline: BEAM 100K ===")
t0 = time.time()
results["suites"]["beam-100k-lexical"] = run_beam(splits=["100K"])
print(f"[run] beam-100k-lexical done in {time.time()-t0:.1f}s")

# Run hybrid (vector+lexical)
print("\n[run] === hybrid: LoCoMo ===")
t0 = time.time()
results["suites"]["locomo-hybrid"] = run_locomo(embedder=embedder)
print(f"[run] locomo-hybrid done in {time.time()-t0:.1f}s")

print("\n[run] === hybrid: LongMemEval-S ===")
t0 = time.time()
results["suites"]["longmemeval-s-hybrid"] = run_longmemeval(embedder=embedder)
print(f"[run] longmemeval-s-hybrid done in {time.time()-t0:.1f}s")

print("\n[run] === hybrid: BEAM 100K ===")
t0 = time.time()
results["suites"]["beam-100k-hybrid"] = run_beam(splits=["100K"], embedder=embedder)
print(f"[run] beam-100k-hybrid done in {time.time()-t0:.1f}s")

# Write artifacts
(REPO / "benchmarks" / "hybrid-results.json").write_text(json.dumps(results, indent=2) + "\n")

lines = [
    "# Aletheia Benchmark Results (hybrid run — Colab A100)",
    "",
    f"**Generated:** {results['generated']}",
    f"**Embedder:** {results['embedder']}",
    f"**Hardware:** {results['hardware']['gpu']}, {results['hardware']['vram_gb']:.0f}GB VRAM",
    "",
]
for name, res in results["suites"].items():
    lines.append(f"## {name}")
    lines.append("")
    if "error" in res:
        lines.append(f"**ERROR:** {res['error']}")
    else:
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        for k in [
            "total", "exact_rate", "f1_mean", "mrr_mean",
            "abstention_count", "abstention_accuracy", "p50_ms", "p95_ms",
        ]:
            if k in res:
                v = res[k]
                if isinstance(v, float):
                    v = f"{v:.3f}"
                lines.append(f"| {k} | {v} |")
    lines.append("")

(REPO / "benchmarks" / "hybrid-results.md").write_text("\n".join(lines))
(REPO / "benchmarks" / "hybrid-complete.txt").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
print("\n[setup] Done. Artifacts in /content/aletheia/benchmarks/")
