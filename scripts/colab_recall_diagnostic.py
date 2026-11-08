#!/usr/bin/env python3
"""Recall accuracy diagnostic using a strong judge model.

For each QA pair:
1. Run Aletheia recall to get top-10 context chunks
2. Ask a strong model to answer from that context only
3. Compare to gold answer
4. Report whether gold answer is retrievable from Aletheia's current recall

This isolates recall quality from generation quality.
"""
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
_token_lock = __import__("threading").Lock()

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

# Use strongest available model for diagnosis
JUDGE_MODEL = os.environ.get("TOKENHARBOR_JUDGE_MODEL", "deepseek-v4.1-flash")
ANSWER_MODEL = os.environ.get("TOKENHARBOR_ANSWER_MODEL", JUDGE_MODEL)
BASE_URL = "https://tokenharbor.ai/v1"

print(f"[setup] Loaded {len(TOKENS)} TokenHarbor token(s)", flush=True)
print(f"[setup] Diagnostic model (answer): {ANSWER_MODEL}", flush=True)
print(f"[setup] Judge model: {JUDGE_MODEL}", flush=True)

# Verify answer model works
print("[setup] Testing models...", flush=True)
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
    sys.exit(1)

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall  # noqa: E402
from aletheia.scope import ScopeEngine  # noqa: E402
from aletheia.extraction import extract_facts, write_claims  # noqa: E402

print("\n[diag] Loading LoCoMo dataset...", flush=True)
ds = _load_dataset()
print(f"[diag] Loaded {len(ds)} conversations", flush=True)

# Strong-model diagnostic prompts
ANSWER_PROMPT = """Answer the question based ONLY on the provided context. If the context does not contain sufficient information to answer the question, respond with "I don't know."

Context:
{context}

Question: {question}

Answer:"""

JUDGE_PROMPT = """You are an impartial judge evaluating whether a generated answer correctly addresses a question based on the provided context and gold answer.

Question: {question}
Gold Answer: {gold_answer}
Generated Answer: {generated_answer}

Instructions:
- Determine if the generated answer is correct given the context and gold answer.
- Consider paraphrases, synonyms, and equivalent expressions as correct.
- Answer with ONLY a JSON object: {{"correct": true/false, "score": 0.0-1.0, "reason": "brief explanation"}}
"""

def generate_answer(question: str, context: str) -> str:
    prompt = ANSWER_PROMPT.format(context=context[:6000], question=question)
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
                    "max_tokens": 150,
                    "temperature": 0.0
                },
                timeout=30
            )
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                return {"correct": False, "score": 0.0, "reason": f"API error {resp.status_code}"}
            text = resp.json()["choices"][0]["message"]["content"].strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                import re
                m = re.search(r'\{[^}]+\}', text)
                if m:
                    try:
                        return json.loads(m.group())
                    except Exception:
                        pass
                return {"correct": False, "score": 0.0, "reason": f"Parse error: {text[:100]}"}
        except Exception as e:
            if attempt == 2:
                return {"correct": False, "score": 0.0, "reason": str(e)}
            time.sleep(1)
    return {"correct": False, "score": 0.0, "reason": "Max retries exceeded"}

# Run diagnostic on a sample of QA pairs
SAMPLE_SIZE = 200
print(f"\n[diag] Starting recall accuracy diagnostic ({SAMPLE_SIZE} QA pairs)...", flush=True)

scores = []
t0 = time.time()

for i, sample in enumerate(ds):
    conv_id, records, qa_pairs = convert_sample(sample)
    if not records or not qa_pairs:
        continue
    
    scopes = {r.scope for r in records}
    engine = ScopeEngine(known_scopes=scopes)
    sub = create_substrate(conv_id, records, engine=engine, embedder=None)
    ctx = make_ctx()
    
    try:
        for qa in qa_pairs:
            if len(scores) >= SAMPLE_SIZE:
                break
            
            # Get recall results
            result = run_recall(sub, qa.question, embedder=None, use_cross_encoder=False, token_getter=lambda: TOKENS[_token_idx % len(TOKENS)])
            context = "\n".join(result.selected_texts[:10])
            
            # Strong model answers from retrieved context
            generated = generate_answer(qa.question, context)
            
            # Judge
            judge_result = judge_answer(qa.question, qa.gold_answer, generated)
            scores.append({
                "question_id": qa.question_id,
                "conv_id": conv_id,
                "context_preview": context[:200],
                "generated_answer": generated,
                "judge_correct": judge_result.get("correct", False),
                "judge_score": judge_result.get("score", 0.0),
                "judge_reason": judge_result.get("reason", ""),
            })
            
            if len(scores) % 50 == 0:
                elapsed = time.time() - t0
                correct = sum(1 for s in scores if s["judge_correct"])
                print(f"[diag] Processed {len(scores)}/{SAMPLE_SIZE} ({elapsed:.1f}s, accuracy={correct/len(scores):.1%})", flush=True)
    finally:
        sub.close()
    
    if len(scores) >= SAMPLE_SIZE:
        break

elapsed = time.time() - t0
correct = sum(1 for s in scores if s["judge_correct"])
avg_score = sum(s["judge_score"] for s in scores) / len(scores) if scores else 0.0

results = {
    "diagnostic": "recall_accuracy_with_strong_model",
    "answer_model": ANSWER_MODEL,
    "judge_model": JUDGE_MODEL,
    "sample_size": len(scores),
    "correct": correct,
    "accuracy": correct / len(scores) if scores else 0.0,
    "avg_score": avg_score,
    "elapsed_s": elapsed,
    "samples": scores
}

out_path = REPO / "benchmarks" / "recall-diagnostic.json"
out_path.write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps({
    "sample_size": len(scores),
    "accuracy": correct / len(scores) if scores else 0.0,
    "avg_score": avg_score,
    "elapsed_s": elapsed
}, indent=2), flush=True)
print(f"\n[Done] recall-diagnostic.json written", flush=True)
