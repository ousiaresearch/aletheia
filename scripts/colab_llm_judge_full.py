#!/usr/bin/env python3
"""Full LLM judge scoring for LoCoMo benchmark via TokenHarbor.

Uses generative QA: generates answers from retrieved context, then judges
correctness against gold answers. This matches competitor evaluation methods.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

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
run("pip install requests -q")
run("pip install sentence-transformers -q")

import requests  # noqa: E402

# TokenHarbor config
TOKENS = [
    os.environ.get("TOKENHARBOR_API_KEY_1", ""),
    os.environ.get("TOKENHARBOR_API_KEY_2", ""),
]
TOKENS = [t for t in TOKENS if t]
if not TOKENS:
    token_path = Path.home() / ".config/vireth/th_token"
    if token_path.exists():
        TOKENS = [token_path.read_text().strip()]

if not TOKENS:
    print("[ERROR] No TokenHarbor tokens found", flush=True)
    sys.exit(1)

_token_idx = 0
_token_lock = Lock()

def next_token() -> str:
    global _token_idx
    with _token_lock:
        token = TOKENS[_token_idx % len(TOKENS)]
        _token_idx += 1
        return token

HEADERS_FACTORY = lambda: {
    "Authorization": f"Bearer {next_token()}",
    "Content-Type": "application/json"
}
JUDGE_MODEL = os.environ.get("TOKENHARBOR_JUDGE_MODEL", "deepseek-v4.1-flash")
ANSWER_MODEL = os.environ.get("TOKENHARBOR_ANSWER_MODEL", JUDGE_MODEL)
BASE_URL = "https://tokenharbor.ai/v1"

print(f"[setup] Loaded {len(TOKENS)} TokenHarbor token(s)", flush=True)
print(f"[setup] Judge model: {JUDGE_MODEL}", flush=True)
print(f"[setup] Answer model: {ANSWER_MODEL}", flush=True)

# Verify judge model works
print("[setup] Testing judge model...", flush=True)
test_resp = requests.post(
    f"{BASE_URL}/chat/completions",
    headers=HEADERS_FACTORY(),
    json={
        "model": JUDGE_MODEL,
        "messages": [{"role": "user", "content": "Say 'ok'"}],
        "max_tokens": 10
    }
)
print(f"[setup] Judge test: {test_resp.status_code}", flush=True)
if test_resp.status_code != 200:
    print(f"[setup] Judge test failed: {test_resp.text[:200]}", flush=True)
    sys.exit(1)

# Verify answer model works (may be same as judge)
if ANSWER_MODEL != JUDGE_MODEL:
    print(f"[setup] Testing answer model...", flush=True)
    ans_test = requests.post(
        f"{BASE_URL}/chat/completions",
        headers=HEADERS_FACTORY(),
        json={
            "model": ANSWER_MODEL,
            "messages": [{"role": "user", "content": "Say 'ok'"}],
            "max_tokens": 10
        }
    )
    print(f"[setup] Answer model test: {ans_test.status_code}", flush=True)
    if ans_test.status_code != 200:
        print(f"[setup] Answer model test failed: {ans_test.text[:200]}", flush=True)
        print("[setup] Falling back to judge model for answers", flush=True)
        ANSWER_MODEL = JUDGE_MODEL

# Load embedder for hybrid recall
print("[setup] Loading sentence-transformers embedder...", flush=True)
from sentence_transformers import SentenceTransformer  # noqa: E402

class STEmbedder:
    """Wrapper for sentence-transformers to match Aletheia's embedder interface."""
    def __init__(self, model_name: str = "BAAI/bge-m3"):
        self.model = SentenceTransformer(model_name)
        self.model_id = f"sentence-transformers/{model_name}"
        self._dim = self.model.get_sentence_embedding_dimension()
    
    def embed_query(self, text: str) -> list[float]:
        return self.model.encode(f"query: {text}", normalize_embeddings=True).tolist()
    
    def embed_document(self, text: str) -> list[float]:
        return self.model.encode(text, normalize_embeddings=True).tolist()
    
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True).tolist()

try:
    embedder = STEmbedder("BAAI/bge-m3")
    print(f"[setup] Embedder loaded (dim={embedder._dim})", flush=True)
except Exception as e:
    print(f"[setup] Failed to load BAAI/bge-m3: {e}", flush=True)
    print("[setup] Falling back to lexical-only recall", flush=True)
    embedder = None

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall  # noqa: E402
from aletheia.scope import ScopeEngine  # noqa: E402
from aletheia.extraction import extract_facts, write_claims  # noqa: E402

print("\n[run] Loading LoCoMo dataset...", flush=True)
ds = _load_dataset()
print(f"[run] Loaded {len(ds)} conversations", flush=True)

# Generative QA prompt
QA_PROMPT = """You are a precise research assistant. Answer the question using ONLY the provided context excerpts.

Rules:
- If the context contains the answer, provide it directly and concisely.
- If the context does not contain sufficient information, respond with exactly "I don't know."
- Do not use outside knowledge.
- Do not speculate or infer beyond what is explicitly stated.
- If multiple excerpts provide relevant information, synthesize them into one coherent answer.

Context:
{context}

Question: {question}

Answer:"""

# Judge prompt
JUDGE_PROMPT = """You are an expert evaluator assessing whether a generated answer correctly addresses a question based on the provided context.

Question: {question}
Gold Answer: {gold_answer}
Generated Answer: {generated_answer}

Context:
{context}

Evaluation Criteria:
1. Correctness: Does the generated answer match the gold answer given the context?
2. Evidence: Is the answer supported by the context?
3. Precision: Is the answer concise and directly responsive?

Scoring:
- 1.0: Fully correct, matches gold answer precisely or with acceptable paraphrase.
- 0.7: Partially correct - captures key information but missing details or has minor inaccuracies.
- 0.0: Incorrect, unsupported by context, or completely misses the question.

Respond with ONLY a JSON object: {{"correct": true/false, "score": float, "reason": "brief explanation"}}"""

def generate_answer(question: str, context: str) -> str:
    """Generate an answer given the question and context."""
    prompt = QA_PROMPT.format(context=context[:8000], question=question)
    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers=HEADERS_FACTORY(),
            json={
                "model": ANSWER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.0
            },
            timeout=30
        )
        if resp.status_code != 200:
            return ""
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""

def judge_answer(question: str, gold_answer: str, generated_answer: str) -> dict:
    """Judge whether the generated answer is correct."""
    prompt = JUDGE_PROMPT.format(
        question=question,
        gold_answer=str(gold_answer),
        generated_answer=generated_answer
    )
    
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{BASE_URL}/chat/completions",
                headers=HEADERS_FACTORY(),
                json={
                    "model": JUDGE_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.0
                },
                timeout=30
            )
            
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"[judge] Rate limited, waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue
            
            if resp.status_code != 200:
                return {"correct": False, "score": 0.0, "reason": f"API error {resp.status_code}"}
            
            text = resp.json()["choices"][0]["message"]["content"].strip()
            
            # Extract JSON from response
            try:
                result = json.loads(text)
                return result
            except json.JSONDecodeError:
                import re
                json_match = re.search(r'\{[^}]+\}', text)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except Exception:
                        pass
                return {"correct": False, "score": 0.0, "reason": f"Parse error: {text[:100]}"}
        except Exception as e:
            if attempt == 2:
                return {"correct": False, "score": 0.0, "reason": str(e)}
            time.sleep(1)
    return {"correct": False, "score": 0.0, "reason": "Max retries exceeded"}

# Checkpointing for resumability
CHECKPOINT_PATH = REPO / "benchmarks" / "judge-results-checkpoint.json"
FINAL_RESULTS_PATH = REPO / "benchmarks" / "judge-results.json"
PROGRESS_PATH = REPO / "benchmarks" / "judge-progress.json"
TOTAL_QA = 1986

def load_checkpoint() -> tuple[list[dict], set[str]]:
    if CHECKPOINT_PATH.exists():
        try:
            data = json.loads(CHECKPOINT_PATH.read_text())
            scores = data.get("scores", [])
            done = set(data.get("done_ids", []))
            print(f"[checkpoint] Loaded {len(scores)}/{TOTAL_QA} scores ({len(done)} done ids)", flush=True)
            return scores, done
        except Exception:
            pass
    return [], set()

def save_checkpoint(scores: list[dict], done_ids: set[str]) -> None:
    try:
        CHECKPOINT_PATH.write_text(json.dumps({
            "scores": scores,
            "done_ids": list(done_ids),
        }, indent=2) + "\n")
    except Exception as e:
        print(f"[checkpoint] Save failed: {e}", flush=True)

def save_progress(conv_idx: int, scores: list[dict], t0: float) -> None:
    """Write lightweight progress file for polling."""
    try:
        elapsed = time.time() - t0
        correct = sum(1 for s in scores if s.get("judge_correct"))
        PROGRESS_PATH.write_text(json.dumps({
            "conversation": conv_idx,
            "processed": len(scores),
            "total": TOTAL_QA,
            "correct": correct,
            "accuracy": correct / len(scores) if scores else 0.0,
            "elapsed_s": elapsed,
            "rate_per_s": len(scores) / elapsed if elapsed > 0 else 0.0,
        }, indent=2) + "\n")
    except Exception:
        pass

def process_qa_pair(qa, context):
    """Process a single QA pair: generate answer and judge it."""
    generated = generate_answer(qa.question, context)
    judge_result = judge_answer(qa.question, qa.gold_answer, generated)
    return {
        "question_id": qa.question_id,
        "generated_answer": generated,
        "judge_correct": judge_result.get("correct", False),
        "judge_score": judge_result.get("score", 0.0),
        "judge_reason": judge_result.get("reason", ""),
    }

# Run generative QA + judge on ALL QA pairs
print("\n[run] Starting generative QA + judge scoring (FULL: all 1,986 QA pairs)...", flush=True)
print(f"[run] Mode: {'hybrid' if embedder else 'lexical'} + extraction + generative QA", flush=True)

scores, done_ids = load_checkpoint()
t0 = time.time() if not scores else time.time()
scores_lock = Lock()

MAX_WORKERS = 10

for i, sample in enumerate(ds):
    conv_id, records, qa_pairs = convert_sample(sample)
    if not records or not qa_pairs:
        continue
    
    scopes = {r.scope for r in records}
    engine = ScopeEngine(known_scopes=scopes)
    sub = create_substrate(conv_id, records, engine=engine, embedder=embedder)
    ctx = make_ctx()
    
    recall_embedder = embedder
    if recall_embedder is not None:
        # Initial check before extraction
        try:
            incomplete = any(
                not sub.vector_index_complete(r.scope, ctx, model_id=recall_embedder.model_id)
                for r in records
            )
        except Exception:
            incomplete = True
        if incomplete:
            print(f"[run] Conv {conv_id}: vector index incomplete before extraction, falling back to lexical", flush=True)
            recall_embedder = None
    
    try:
        # Run extraction + vectorize new claims
        if recall_embedder is not None:
            try:
                claims = extract_facts(sub, ctx, "benchmark", limit=50)
                written = write_claims(sub, ctx, claims, embedder=recall_embedder)
                if written > 0:
                    print(f"[run] Conv {conv_id}: extracted {written} claims with vectors", flush=True)
            except Exception as e:
                print(f"[run] Conv {conv_id}: extraction failed: {e}", flush=True)
        
        # Pre-fetch recall contexts sequentially to avoid substrate contention
        tasks = []
        for qa in qa_pairs:
            if qa.question_id in done_ids:
                continue
            try:
                result = run_recall(sub, qa.question, embedder=recall_embedder, use_cross_encoder=True, token_getter=lambda: TOKENS[_token_idx % len(TOKENS)])
                context = "\n".join(result.selected_texts[:10])
                tasks.append((qa, context))
            except Exception as e:
                print(f"[run] Conv {conv_id}: recall failed for {qa.question_id}: {e}", flush=True)
        
        # Process QA pairs in parallel
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_qa_pair, qa, context): qa
                for qa, context in tasks
            }
            for future in as_completed(futures):
                qa = futures[future]
                try:
                    result = future.result()
                    with scores_lock:
                        scores.append(result)
                        done_ids.add(qa.question_id)
                except Exception as e:
                    print(f"[run] Conv {conv_id}: QA {qa.question_id} failed: {e}", flush=True)
        
        if len(scores) % 100 == 0:
            save_checkpoint(scores, done_ids)
            elapsed = time.time() - t0
            correct = sum(1 for s in scores[:len(scores)] if s["judge_correct"])
            print(f"[run] Processed {len(scores)}/{TOTAL_QA} QA pairs ({elapsed:.1f}s, ~{len(scores)/elapsed:.1f}/s, accuracy={correct/len(scores):.1%})", flush=True)
        save_progress(i + 1, scores, t0)
    finally:
        sub.close()

elapsed = time.time() - t0
print(f"\n[run] Processed {len(scores)} QA pairs in {elapsed:.1f}s", flush=True)

# Aggregate
correct = sum(1 for s in scores if s["judge_correct"])
avg_score = sum(s["judge_score"] for s in scores) / len(scores) if scores else 0.0

results = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "judge_model": JUDGE_MODEL,
    "answer_model": ANSWER_MODEL,
    "recall_mode": "hybrid" if embedder else "lexical",
    "eval_method": "generative_qa",
    "total_judged": len(scores),
    "correct": correct,
    "accuracy": correct / len(scores) if scores else 0.0,
    "avg_score": avg_score,
    "elapsed_s": elapsed,
    "samples": scores[:10]
}

FINAL_RESULTS_PATH.write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2), flush=True)
print("\n[Done] judge-results.json written", flush=True)

# Clean up checkpoint on success
try:
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
except Exception:
    pass
