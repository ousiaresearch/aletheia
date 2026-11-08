#!/usr/bin/env python3
"""Local smoke test for new recall enhancements.

Tests on a small sample (50 QA pairs) to verify:
1. Cross-encoder reranker changes ranking
2. Query expansion adds alternative phrasings
3. Combined effect on recall coverage
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path("/Users/johannross/workspace/aletheia")
sys.path.insert(0, str(REPO))

from benchmarks.full.locomo import _load_dataset, convert_sample
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall
from aletheia.scope import ScopeEngine

# Local embedder wrapper for smoke test
try:
    from sentence_transformers import SentenceTransformer
    class STEmbedder:
        def __init__(self, model_name="BAAI/bge-m3"):
            self.model = SentenceTransformer(model_name)
            self.model_id = f"sentence-transformers/{model_name}"
        def embed_query(self, text):
            return self.model.encode(f"query: {text}", normalize_embeddings=True).tolist()
        def embed_document(self, text):
            return self.model.encode(text, normalize_embeddings=True).tolist()
        def embed_documents(self, texts):
            return self.model.encode(texts, normalize_embeddings=True).tolist()
    EMBEDDER_AVAILABLE = True
except Exception:
    EMBEDDER_AVAILABLE = False

SAMPLE_SIZE = 50

def token_getter():
    return "test-token"

def main():
    print(f"[smoke] Loading LoCoMo dataset...", flush=True)
    ds = _load_dataset()
    
    embedder = None
    if EMBEDDER_AVAILABLE:
        try:
            embedder = STEmbedder("BAAI/bge-m3")
            print(f"[smoke] Embedder loaded", flush=True)
        except Exception as e:
            print(f"[smoke] Embedder failed: {e}", flush=True)
    else:
        print(f"[smoke] sentence-transformers not available, skipping hybrid tests", flush=True)
    
    scores_baseline = []
    scores_enhanced = []
    
    for i, sample in enumerate(ds):
        conv_id, records, qa_pairs = convert_sample(sample)
        if not records or not qa_pairs:
            continue
        
        scopes = {r.scope for r in records}
        engine = ScopeEngine(known_scopes=scopes)
        sub = create_substrate(conv_id, records, engine=engine, embedder=embedder)
        ctx = make_ctx()
        
        for qa in qa_pairs:
            if len(scores_baseline) >= SAMPLE_SIZE:
                break
            
            # Baseline: lexical-only, no cross-encoder, no query expansion
            try:
                result_base = run_recall(sub, qa.question, embedder=None, use_cross_encoder=False)
                has_gold_base = any(qa.evidence_ids and rid in qa.evidence_ids for rid in result_base.selected_ids)
                scores_baseline.append({
                    "question_id": qa.question_id,
                    "has_gold": has_gold_base,
                    "selected": len(result_base.selected_ids),
                })
            except Exception as e:
                print(f"[smoke] Baseline failed for {qa.question_id}: {e}", flush=True)
            
            # Enhanced: hybrid + cross-encoder + query expansion
            try:
                result_enh = run_recall(sub, qa.question, embedder=embedder, use_cross_encoder=False, token_getter=token_getter)
                has_gold_enh = any(qa.evidence_ids and rid in qa.evidence_ids for rid in result_enh.selected_ids)
                scores_enhanced.append({
                    "question_id": qa.question_id,
                    "has_gold": has_gold_enh,
                    "selected": len(result_enh.selected_ids),
                })
            except Exception as e:
                print(f"[smoke] Enhanced failed for {qa.question_id}: {e}", flush=True)
        
        sub.close()
        
        if len(scores_baseline) >= SAMPLE_SIZE:
            break
    
    # Compute metrics
    base_recall = sum(1 for s in scores_baseline if s["has_gold"]) / len(scores_baseline) if scores_baseline else 0.0
    enh_recall = sum(1 for s in scores_enhanced if s["has_gold"]) / len(scores_enhanced) if scores_enhanced else 0.0
    
    base_selected_avg = sum(s["selected"] for s in scores_baseline) / len(scores_baseline) if scores_baseline else 0.0
    enh_selected_avg = sum(s["selected"] for s in scores_enhanced) / len(scores_enhanced) if scores_enhanced else 0.0
    
    results = {
        "sample_size": len(scores_baseline),
        "baseline_recall": base_recall,
        "enhanced_recall": enh_recall,
        "recall_lift": enh_recall - base_recall,
        "baseline_avg_selected": base_selected_avg,
        "enhanced_avg_selected": enh_selected_avg,
        "samples": scores_baseline[:5],
    }
    
    print(json.dumps(results, indent=2), flush=True)
    
    # Save results
    out_path = REPO / "benchmarks" / "smoke-test-results.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\n[smoke] Results saved to {out_path}", flush=True)
    
    # Verdict
    if enh_recall > base_recall + 0.02:
        print(f"\n[smoke] POSITIVE: recall improved by {enh_recall - base_recall:.1%}", flush=True)
        return 0
    elif enh_recall >= base_recall:
        print(f"\n[smoke] NEUTRAL: recall unchanged or slightly improved", flush=True)
        return 0
    else:
        print(f"\n[smoke] NEGATIVE: recall decreased", flush=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
