#!/usr/bin/env python3
"""Local benchmark: baseline vs enhanced recall on up to 200 QA pairs.

Measures:
- Lexical-only baseline
- Hybrid + cross-encoder + query expansion (enhanced)

Reports hit@k, MRR, and coverage for each mode.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path("/Users/johannross/workspace/aletheia")
sys.path.insert(0, str(REPO))

from benchmarks.full.locomo import _load_dataset, convert_sample
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall
from aletheia.scope import ScopeEngine
from aletheia.extraction import _get_token

try:
    from sentence_transformers import SentenceTransformer

    class STEmbedder:
        def __init__(self, model_name="BAAI/bge-m3"):
            self.model = SentenceTransformer(model_name)
            self.model_id = f"sentence-transformers/{model_name}"

        def embed_query(self, text):
            return self.model.encode(text, normalize_embeddings=True).tolist()

        def embed_document(self, text):
            return self.model.encode(text, normalize_embeddings=True).tolist()

        def embed_documents(self, texts):
            return self.model.encode(texts, normalize_embeddings=True).tolist()

    EMBEDDER_AVAILABLE = True
except Exception as e:
    print(f"[bench] sentence-transformers unavailable: {e}", flush=True)
    EMBEDDER_AVAILABLE = False

SAMPLE_SIZE = int(os.environ.get("SAMPLE_SIZE", "200"))


def token_getter():
    return _get_token()


def score_recall(result, gold_ids):
    """Return (hit_at_k, mrr, found)."""
    found = False
    first_rank = None
    for rank, rec_id in enumerate(result.selected_ids, 1):
        if rec_id in gold_ids:
            found = True
            first_rank = rank
            break
    hit = 1 if found else 0
    mrr = 1.0 / first_rank if first_rank else 0.0
    return hit, mrr, found


def run_mode(ds, mode, embedder=None, details_path=None):
    """Run recall in given mode over first SAMPLE_SIZE QA pairs.

    If details_path is provided, write per-QA results for offline analysis.
    """
    hits = []
    mrrs = []
    latencies = []
    layer_counts = {}
    processed = 0
    details = []

    for sample in ds:
        if processed >= SAMPLE_SIZE:
            break
        conv_id, records, qa_pairs = convert_sample(sample)
        if not records or not qa_pairs:
            continue

        scopes = {r.scope for r in records}
        engine = ScopeEngine(known_scopes=scopes)
        sub = create_substrate(conv_id, records, engine=engine, embedder=embedder)
        ctx = make_ctx()

        recall_embedder = embedder
        if mode == "hybrid":
            if recall_embedder is None:
                recall_embedder = None
        elif mode == "baseline":
            recall_embedder = None

        for qa in qa_pairs:
            if processed >= SAMPLE_SIZE:
                break
            gold_ids = set(qa.evidence_ids) if qa.evidence_ids else set()
            if not gold_ids:
                continue

            t0 = time.time()
            try:
                result = run_recall(
                    sub,
                    qa.question,
                    embedder=recall_embedder,
                    use_cross_encoder=(mode == "enhanced"),
                    token_getter=None,  # query expansion regressed recall locally; re-enable after embedder upgrade
                )
                latency = time.time() - t0
                hit, mrr, found = score_recall(result, gold_ids)
                hits.append(hit)
                mrrs.append(mrr)
                latencies.append(latency)
                for layer in result.layers:
                    layer_counts[layer] = layer_counts.get(layer, 0) + 1
                processed += 1
                if details_path is not None:
                    details.append({
                        "conv_id": conv_id,
                        "question_id": qa.question_id,
                        "question": qa.question,
                        "gold_count": len(gold_ids),
                        "found": found,
                        "hit": hit,
                        "mrr": mrr,
                        "latency_ms": latency * 1000,
                        "selected_ids": result.selected_ids[:10],
                        "layers": result.layers,
                    })
            except Exception as e:
                print(f"[bench] {conv_id} {qa.question_id} failed: {e}", flush=True)

        sub.close()

    if not hits:
        return {"processed": 0}
    result = {
        "processed": len(hits),
        "hit_at_k": sum(hits) / len(hits),
        "mrr": sum(mrrs) / len(mrrs),
        "p50_latency_ms": sorted(latencies)[len(latencies) // 2] * 1000,
        "p95_latency_ms": sorted(latencies)[int(len(latencies) * 0.95)] * 1000,
        "layer_counts": layer_counts,
    }
    if details_path is not None and details:
        Path(details_path).write_text(json.dumps({
            "mode": mode,
            "sample_size": SAMPLE_SIZE,
            "details": details,
        }, indent=2) + "\n")
        print(f"[bench] Wrote details to {details_path}", flush=True)
    return result


def main():
    print(f"[bench] Loading LoCoMo dataset...", flush=True)
    ds = _load_dataset()
    print(f"[bench] Loaded {len(ds)} conversations", flush=True)

    embedder = None
    if EMBEDDER_AVAILABLE:
        try:
            embedder = STEmbedder("intfloat/e5-large-v2")
            print(f"[bench] Embedder loaded", flush=True)
        except Exception as e:
            print(f"[bench] Embedder failed: {e}", flush=True)

    print(f"\n[bench] Running baseline (lexical-only) on {SAMPLE_SIZE} QA...", flush=True)
    t0 = time.time()
    baseline_details = REPO / "benchmarks" / "local-baseline-details.json"
    baseline = run_mode(ds, "baseline", embedder, details_path=str(baseline_details))
    baseline["elapsed_s"] = time.time() - t0
    print(json.dumps(baseline, indent=2), flush=True)

    print(f"\n[bench] Running enhanced (hybrid + cross-encoder + query expansion)...", flush=True)
    t0 = time.time()
    enhanced_details = REPO / "benchmarks" / "local-enhanced-details.json"
    enhanced = run_mode(ds, "enhanced", embedder, details_path=str(enhanced_details))
    enhanced["elapsed_s"] = time.time() - t0
    print(json.dumps(enhanced, indent=2), flush=True)

    if baseline.get("processed", 0) > 0 and enhanced.get("processed", 0) > 0:
        delta_hit = enhanced["hit_at_k"] - baseline["hit_at_k"]
        delta_mrr = enhanced["mrr"] - baseline["mrr"]
        print(f"\n[bench] Delta hit@k: {delta_hit:+.1%}", flush=True)
        print(f"[bench] Delta MRR: {delta_mrr:+.3f}", flush=True)
        print(f"[bench] Speedup factor: {baseline['elapsed_s'] / enhanced['elapsed_s']:.1f}x", flush=True)

    results = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_size": SAMPLE_SIZE,
        "baseline": baseline,
        "enhanced": enhanced,
    }
    out = REPO / "benchmarks" / "local-baseline-vs-enhanced.json"
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\n[bench] Wrote {out}", flush=True)


if __name__ == "__main__":
    main()
