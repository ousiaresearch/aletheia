"""Run full benchmarks and save results incrementally."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from benchmarks.full.locomo import run as run_locomo
from benchmarks.full.longmemeval import run as run_longmemeval
from benchmarks.full.beam import run as run_beam

BENCH_DIR = Path(__file__).resolve().parents[1]
OUT_FILE = BENCH_DIR / "full-results.json"

# Try BGE-M3 embedder
embedder = None
try:
    from aletheia.adapters import BgeM3Embedder
    from pathlib import Path
    model_dir = (
        Path.home()
        / ".cache/huggingface/hub/models--mlx-community--bge-m3-mlx-4bit/snapshots/0a93c12b0684953bdaed14865dc270bb1894a749"
    )
    embedder = BgeM3Embedder(str(model_dir))
    print(f"Using embedder: {embedder.model_id}")
except Exception as exc:
    print(f"Vector mode disabled: {exc}")

results = {
    "generated": "2026-10-29",
    "embedder": embedder.model_id if embedder else None,
    "suites": {},
}

print("=== LoCoMo ===")
results["suites"]["locomo"] = run_locomo(embedder=embedder)
OUT_FILE.write_text(json.dumps(results, indent=2) + "\n")
print(f"Saved LoCoMo results to {OUT_FILE}")

print("\n=== LongMemEval-S ===")
results["suites"]["longmemeval-s"] = run_longmemeval(embedder=embedder)
OUT_FILE.write_text(json.dumps(results, indent=2) + "\n")
print(f"Saved LongMemEval results to {OUT_FILE}")

print("\n=== BEAM 100K ===")
results["suites"]["beam-100k"] = run_beam(splits=["100K"], embedder=embedder)
OUT_FILE.write_text(json.dumps(results, indent=2) + "\n")
print(f"Saved BEAM 100K results to {OUT_FILE}")

print("\nDone!")
print(json.dumps(results, indent=2))
