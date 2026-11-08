#!/usr/bin/env python3
"""Run only BEAM 500K on Colab with detailed progress."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path("/content/aletheia")
os.chdir(REPO)
sys.path.insert(0, str(REPO))

from benchmarks.full.beam import run as run_beam

print("[beam500k] Starting BEAM 500K...")
t0 = time.time()
try:
    res = run_beam(splits=["500K"])
    print(f"[beam500k] Done in {time.time()-t0:.1f}s")
    print(json.dumps(res, indent=2))
    (REPO / "benchmarks" / "beam-500k-results.json").write_text(json.dumps(res, indent=2) + "\n")
except Exception as e:
    print(f"[beam500k] FAILED: {e}")
    import traceback; traceback.print_exc()
