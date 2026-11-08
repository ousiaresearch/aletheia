"""Run all full benchmark suites."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from benchmarks.full.locomo import run as run_locomo
from benchmarks.full.longmemeval import run as run_longmemeval
from benchmarks.full.beam import run as run_beam

BENCH_DIR = Path(__file__).resolve().parents[1]
OUT_FILE = BENCH_DIR / "full-results.json"


def main():
    embedder = None
    # Try BGE-M3 embedder for hybrid mode
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
    print(json.dumps(results["suites"]["locomo"], indent=2))

    print("\n=== LongMemEval-S ===")
    results["suites"]["longmemeval-s"] = run_longmemeval(embedder=embedder)
    print(json.dumps(results["suites"]["longmemeval-s"], indent=2))

    print("\n=== BEAM (100K + 500K) ===")
    results["suites"]["beam"] = run_beam(embedder=embedder)
    print(json.dumps(results["suites"]["beam"], indent=2))

    OUT_FILE.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nWrote {OUT_FILE}")


if __name__ == "__main__":
    main()
