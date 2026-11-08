#!/usr/bin/env python3
"""Mini QA with gpt-6-luna on Colab - 10 QA pairs only."""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

REPO = "/content/aletheia"

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[setup] FAILED: {cmd}\n{r.stderr[:200]}", flush=True)
        sys.exit(1)
    return r.stdout.strip()

if not os.path.exists(REPO):
    run("git clone https://github.com/ousiaresearch/aletheia.git /content/aletheia")
os.chdir(REPO)
sys.path.insert(0, REPO)

run("pip install -e . -q")
run("pip install datasets -q")
run("pip install requests -q")
run("pip install sentence-transformers -q")

import requests
from sentence_transformers import SentenceTransformer

# TokenHarbor
TOKENS = []
for k in ["TOKENHARBOR_API_KEY_1", "TOKENHARBOR_API_KEY_2"]:
    v = os.environ.get(k, "")
    if v:
        TOKENS.append(v)
if not TOKENS:
    try:
        TOKENS = [open(os.path.expanduser("~/.config/vireth/th_token")).read().strip()]
    except:
        pass
if not TOKENS:
    print("[ERROR] No tokens")
    sys.exit(1)

_token_idx = 0
_token_lock = Lock()
def next_token():
    global _token_idx
    with _token_lock:
        t = TOKENS[_token_idx % len(TOKENS)]
        _token_idx += 1
        return t

BASE_URL = "https://tokenharbor.ai/v1"
JUDGE_MODEL = os.environ.get("TOKENHARBOR_JUDGE_MODEL", "gpt-6-luna")
ANSWER_MODEL = os.environ.get("TOKENHARBOR_ANSWER_MODEL", "gpt-6-luna")
MAX_QA = int(os.environ.get("MAX_QA", "10"))

print(f"[setup] Judge: {JUDGE_MODEL}, Answer: {ANSWER_MODEL}, Max QA: {MAX_QA}", flush=True)

# Verify
for m in [JUDGE_MODEL, ANSWER_MODEL]:
    r = requests.post(f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {next_token()}", "Content-Type": "application/json"},
        json={"model": m, "messages": [{"role": "user", "content": "Say ok"}], "max_tokens": 5}, timeout=30)
    print(f"[setup] {m}: {r.status_code}", flush=True)
    if r.status_code != 200:
        sys.exit(1)

# Load
print("[run] Loading dataset...", flush=True)
from benchmarks.full.locomo import _load_dataset, convert_sample
from benchmarks.full.common import make_engine, make_ctx, create_substrate, run_recall
from aletheia.scope import ScopeEngine

class STEmbedder:
    def __init__(self, name="BAAI/bge-m3"):
        self.model = SentenceTransformer(name)
        self.model_id = f"sentence-transformers/{name}"
    def embed_query(self, t):
        return self.model.encode(f"query: {t}", normalize_embeddings=True).tolist()
    def embed_documents(self, texts):
        return self.model.encode(texts, normalize_embeddings=True).tolist()

embedder = STEmbedder()
ds = _load_dataset()

# Collect QA pairs
all_qa = []
for i in range(len(ds)):
    sample = ds[i]
    conv_id, records, qa_pairs = convert_sample(sample)
    for qa in qa_pairs:
        all_qa.append((conv_id, records, qa))
    if len(all_qa) >= MAX_QA:
        break
all_qa = all_qa[:MAX_QA]

# Improved prompts
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

Respond with ONLY a JSON object: {"correct": true/false, "score": float, "reason": "brief explanation"}"""

def generate_answer(question, context):
    prompt = QA_PROMPT.format(context=context[:8000], question=question)
    try:
        r = requests.post(f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {next_token()}", "Content-Type": "application/json"},
            json={"model": ANSWER_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 150, "temperature": 0.0},
            timeout=30)
        if r.status_code != 200:
            return ""
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""

def judge_answer(question, gold_answer, generated_answer, context):
    prompt = JUDGE_PROMPT.format(
        question=question,
        gold_answer=str(gold_answer),
        generated_answer=generated_answer,
        context=context[:8000]
    )
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {next_token()}", "Content-Type": "application/json"},
                json={"model": JUDGE_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200, "temperature": 0.0},
                timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                return {"correct": False, "score": 0.0, "reason": f"API {r.status_code}"}
            text = r.json()["choices"][0]["message"]["content"].strip()
            try:
                return json.loads(text)
            except:
                import re
                m = re.search(r'\{[^}]+\}', text)
                if m:
                    try: return json.loads(m.group())
                    except: pass
                return {"correct": False, "score": 0.0, "reason": f"Parse: {text[:100]}"}
        except Exception as e:
            if attempt == 2:
                return {"correct": False, "score": 0.0, "reason": str(e)}
            time.sleep(1)
    return {"correct": False, "score": 0.0, "reason": "Max retries"}

print(f"[run] Processing {len(all_qa)} QA pairs with {ANSWER_MODEL} + judge {JUDGE_MODEL}", flush=True)
t0 = time.time()
scores = []
for conv_id, records, qa in all_qa:
    scopes = {r.scope for r in records}
    engine = ScopeEngine(known_scopes=scopes)
    sub = create_substrate(conv_id, records, engine=engine, embedder=embedder)
    ctx = make_ctx()
    try:
        result = run_recall(sub, qa.question, embedder=embedder, use_cross_encoder=True, token_getter=None)
        context = "\n".join(result.selected_texts[:10])
    except Exception as e:
        print(f"[run] Recall failed: {e}", flush=True)
        context = ""
    sub.close()
    
    generated = generate_answer(qa.question, context)
    judge_result = judge_answer(qa.question, qa.gold_answer, generated, context)
    scores.append({
        "question_id": qa.question_id,
        "question": qa.question,
        "gold_answer": str(qa.gold_answer),
        "generated_answer": generated[:300],
        "judge_correct": judge_result.get("correct", False),
        "judge_score": judge_result.get("score", 0.0),
        "judge_reason": judge_result.get("reason", ""),
    })
    print(f"[{len(scores)}/{len(all_qa)}] {qa.question_id}: correct={judge_result.get('correct')} score={judge_result.get('score', 0):.2f}", flush=True)

elapsed = time.time() - t0
correct = sum(1 for s in scores if s["judge_correct"])
avg_score = sum(s["judge_score"] for s in scores) / len(scores) if scores else 0.0

results = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "judge_model": JUDGE_MODEL,
    "answer_model": ANSWER_MODEL,
    "recall_mode": "hybrid",
    "eval_method": "generative_qa",
    "total_judged": len(scores),
    "correct": correct,
    "accuracy": correct / len(scores) if scores else 0.0,
    "avg_score": avg_score,
    "elapsed_s": elapsed,
    "samples": scores,
}

out_path = f"{REPO}/benchmarks/qa-mini-strong-models.json"
with open(out_path, "w") as f:
    json.dump(results, f, indent=2)

print(json.dumps(results, indent=2), flush=True)
print(f"\n[Done] {out_path} written", flush=True)
