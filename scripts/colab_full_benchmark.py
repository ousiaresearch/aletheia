#!/usr/bin/env python3
"""Setup + run full Phase 9 benchmark on Colab."""
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

# Clone repo if missing
if not (REPO / ".git").exists():
    run("git clone https://github.com/ousiaresearch/aletheia.git /content/aletheia")
    os.chdir(REPO)
else:
    os.chdir(REPO)
    run("git pull --rebase")

# Install
run("pip install -e . -q")
run("pip install datasets pytest -q")

# Verify
import aletheia  # noqa: E402
from benchmarks.full.locomo import run as run_locomo  # noqa: E402
from benchmarks.full.longmemeval import run as run_longmemeval  # noqa: E402
from benchmarks.full.beam import run as run_beam  # noqa: E402
print("[setup] imports OK")

# Ensure cache dir exists
CACHE.mkdir(parents=True, exist_ok=True)

results = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "embedder": None,
    "suites": {},
}

suites = [
    ("locomo", lambda: run_locomo(embedder=None)),
    ("longmemeval-s", lambda: run_longmemeval()),
    ("beam-100k", lambda: run_beam(splits=["100K"])),
    ("beam-500k", lambda: run_beam(splits=["500K"])),
]

for name, runner in suites:
    print(f"\n[run] === {name} ===")
    t0 = time.time()
    try:
        res = runner()
        results["suites"][name] = res
        print(f"[run] {name} done in {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[run] {name} FAILED: {e}")
        results["suites"][name] = {"error": str(e)}

# Write artifacts
(REPO / "benchmarks" / "full-results.json").write_text(json.dumps(results, indent=2) + "\n")

lines = [
    "# Aletheia Benchmark Results (full run — Colab)",
    "",
    f"**Generated:** {results['generated']}",
    "**Methodology:** Retrieval hit@k + MRR when evidence_ids available; token F1 fallback.",
    f"**Embedder:** {results['embedder']} — lexical only",
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

(REPO / "benchmarks" / "full-results.md").write_text("\n".join(lines))
(REPO / "benchmarks" / "run-complete.txt").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
print("\n[setup] Done. Artifacts in /content/aletheia/benchmarks/")
