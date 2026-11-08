#!/usr/bin/env python3
"""LLM judge scoring for LoCoMo benchmark via TokenHarbor."""
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

import requests  # noqa: E402

# TokenHarbor config
TOKEN = os.environ.get("TOKENHARBOR_API_KEY", "")
if not TOKEN:
    token_path = Path.home() / ".config/vireth/th_token"
    if token_path.exists():
        TOKEN = token_path.read_text().strip()

if not TOKEN:
    print("[ERROR] No TokenHarbor token found", flush=True)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}
JUDGE_MODEL = "deepseek-v4.1-flash"
BASE_URL = "https://tokenharbor.ai/v1"

print(f"[setup] TokenHarbor token loaded (length {len(TOKEN)})", flush=True)
print(f"[setup] Judge model: {JUDGE_MODEL}", flush=True)

# Verify judge model works
print("[setup] Testing judge model...", flush=True)
test_resp = requests.post(
    f"{BASE_URL}/chat/completions",
    headers=HEADERS,
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

from benchmarks.full.locomo import _load_dataset, convert_sample  # noqa: E402
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall  # noqa: E402
from aletheia.scope import ScopeEngine  # noqa: E402

print("\n[run] Loading LoCoMo dataset...", flush=True)
ds = _load_dataset()
print(f"[run] Loaded {len(ds)} conversations", flush=True)

# Judge prompt template
JUDGE_PROMPT = """You are an impartial judge evaluating whether a retrieved context contains the information needed to answer a question.

Question: {question}

Gold Answer: {gold_answer}

Retrieved Context:
{context}

Instructions:
- Determine if the retrieved context contains sufficient information to answer the question correctly.
- Consider paraphrases, synonyms, and equivalent expressions as correct.
- Answer with ONLY a JSON object: {{"correct": true/false, "score": 0.0-1.0, "reason": "brief explanation"}}
- "correct": true if the context supports the gold answer, false otherwise.
- "score": 1.0 if fully correct, 0.0 if completely incorrect, or a fractional value for partial matches.
"""

def judge_answer(question: str, gold_answer: str, context: str) -> dict:
    prompt = JUDGE_PROMPT.format(
        question=question,
        gold_answer=gold_answer,
        context=context[:4000]  # Truncate to avoid token limits
    )
    
    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers=HEADERS,
            json={
                "model": JUDGE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.0
            },
            timeout=30
        )
        
        if resp.status_code != 200:
            return {"correct": False, "score": 0.0, "reason": f"API error {resp.status_code}"}
        
        text = resp.json()["choices"][0]["message"]["content"].strip()
        
        # Extract JSON from response
        try:
            # Try to parse as JSON directly
            result = json.loads(text)
            return result
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code block or text
            import re
            json_match = re.search(r'\{[^}]+\}', text)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except:
                    pass
            return {"correct": False, "score": 0.0, "reason": f"Parse error: {text[:100]}"}
    except Exception as e:
        return {"correct": False, "score": 0.0, "reason": str(e)}

# Run judge scoring on first 50 QA pairs as POC
print("\n[run] Starting LLM judge scoring (POC: first 50 QA pairs)...", flush=True)

scores = []
total_cost = 0.0
t0 = time.time()

for i, sample in enumerate(ds):
    if len(scores) >= 50:
        break
    
    conv_id, records, qa_pairs = convert_sample(sample)
    if not records or not qa_pairs:
        continue
    
    scopes = {r.scope for r in records}
    engine = ScopeEngine(known_scopes=scopes)
    sub = create_substrate(conv_id, records, engine=engine)
    ctx = make_ctx()
    
    try:
        for qa in qa_pairs[:5]:  # Max 5 per conversation
            if len(scores) >= 50:
                break
            
            # Get hybrid recall results
            result = run_recall(sub, qa.question, embedder=None)
            context = "\n".join(result.selected_texts[:10])
            
            # Judge
            judge_result = judge_answer(qa.question, qa.gold_answer, context)
            scores.append({
                "question_id": qa.question_id,
                "judge_correct": judge_result.get("correct", False),
                "judge_score": judge_result.get("score", 0.0),
                "judge_reason": judge_result.get("reason", ""),
            })
            
            if len(scores) % 10 == 0:
                print(f"[run] Judged {len(scores)}/50 QA pairs...", flush=True)
    finally:
        sub.close()

elapsed = time.time() - t0
print(f"\n[run] Judged {len(scores)} QA pairs in {elapsed:.1f}s", flush=True)

# Aggregate
correct = sum(1 for s in scores if s["judge_correct"])
avg_score = sum(s["judge_score"] for s in scores) / len(scores) if scores else 0.0

results = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "judge_model": JUDGE_MODEL,
    "total_judged": len(scores),
    "correct": correct,
    "accuracy": correct / len(scores) if scores else 0.0,
    "avg_score": avg_score,
    "elapsed_s": elapsed,
    "samples": scores[:5]  # Sample for verification
}

(REPO / "benchmarks" / "judge-results.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2), flush=True)
print("\n[Done] judge-results.json written", flush=True)
