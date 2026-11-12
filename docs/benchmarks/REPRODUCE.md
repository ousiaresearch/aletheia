# Reproducing the benchmark results

Every number in [`results.md`](results.md) is produced by one of the commands below, on an 8 GB
Apple-silicon machine with the local `BAAI/bge-m3` 4-bit MLX embedder in the Hugging Face cache for
hybrid mode. Lexical mode needs no model at all.

Run everything with `env -u PYTHONPATH` if your shell exports a `PYTHONPATH` that shadows this
project's virtualenv — a stray `PYTHONPATH` is the most common way these runs fail with an
unrelated numpy error.

## The suite, on one command

```bash
# whole LoCoMo, lexical — 1,986 questions, ~36 s
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset locomo

# LongMemEval-S, first 100 questions
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset longmem --n 100

# BEAM 100K, first 40 questions
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset beam --splits 100K --n 40

# all three, a seeded random sample, hybrid, writing JSON
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset all --n 120 \
    --sample random --seed 1 --mode hybrid --json out.json
```

`--sample random --seed N` draws uniformly instead of taking the first N in file order. Use it for
anything you intend to quote: the first 200 LoCoMo questions are all one conversation, and the first
ten of each conversation are category-skewed (41/43/14/2/0 against the benchmark's
282/321/96/841/446).

### What each dataset can and cannot be scored on

| Dataset | Gold unit | hit@k | coverage / full recovery | abstention |
|---|---|---|---|---|
| LoCoMo | a record | yes — evidence record ids | yes | yes — 446 questions with no answer |
| LongMemEval-S | a **session** (~460 messages) | yes | **no** — an id names a session, so "coverage" would be meaningless | yes |
| BEAM | none shipped | **no** — no evidence ids | no | yes |

`run.py` labels this per dataset rather than emitting one coverage column across all three. Reading a
LongMemEval coverage figure against a LoCoMo one is the same category error as comparing a retrieval
rate to an LLM-judged answer score.

## The paired diagnostics

These answer "why", not "what". Both build one substrate (and one embedding pass) per conversation
and then run every configuration against the same records, so the comparison is paired.

```bash
# LoCoMo: configurations side by side, with hits gained and lost per question
env -u PYTHONPATH .venv/bin/python benchmarks/diagnose_locomo.py --n 120 --sample random --seed 1 \
    --configs lexical,vector,hybrid-noRerank,hybrid+rerank --json diagnose.json

# depth curve: is the coverage gap truncation or ranking?
env -u PYTHONPATH .venv/bin/python benchmarks/diagnose_locomo.py --n 1986 --limit 50 \
    --configs lexical --json depth50.json

# LongMemEval: mode and rerank comparison, paired
env -u PYTHONPATH .venv/bin/python benchmarks/diagnose_recall.py --offset 200 --n 3
```

A hybrid run on this machine is expensive: the embedder costs roughly 11 records/s on index builds
and turns a ~20 ms lexical query into ~300 ms (≈2,700 ms with a bi-encoder rerank). The whole-set
lexical pass takes 36 s; the same pass while building vectors took 431 s. Budget accordingly, and
prefer `--sample random` with a modest `--n` over full sets in hybrid mode.

## The older runners, and why they disagree

| Runner | What it does | Status |
|---|---|---|
| `benchmarks/run.py` | the suite entrypoint above | **use this** |
| `benchmarks/sample.py` | one dataset, gold-id scoring, sets N and interval | still valid; superseded by `run.py` for suite-level runs |
| `benchmarks/diagnose_locomo.py`, `diagnose_recall.py` | paired per-question comparisons | valid, diagnostic only |
| `benchmarks/full/run_all.py` | LoCoMo + LongMemEval + BEAM | **do not quote.** It scores LongMemEval-S and BEAM without passing their gold ids, so both fall back to the answer-string proxy |
| `benchmarks/full/*/run()` | per-dataset runners | same defect as above |
| `benchmarks/smoke.py` | 22 gold cases, saturates | sanity check only |

## Reproducibility checks worth running after any harness change

```bash
# same seeds, two runs, identical per-question outcomes
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset locomo \
    --sample random --seed 1 --n 120 --json a.json
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset locomo \
    --sample random --seed 1 --n 120 --json b.json
python - <<'PY'
import json
a, b = (json.load(open(p))["results"][0] for p in ("a.json", "b.json"))
for key in ("hits", "gold_scored", "hit_at_k", "mrr", "full_recovery"):
    assert a[key] == b[key], (key, a[key], b[key])
print("per-dataset aggregates reproduce")
PY
```

Expected whole-set LoCoMo, lexical, depth 10: **1,538 graded, 990 hits, hit@10 64.4% (CI 61.9–66.7),
MRR 0.389, coverage 0.574, full recovery 52.2%, abstention 0/444, and gold validation reporting 5 of
1,982 questions citing evidence ids no record carries.** The 1,538 is the answerable
remainder after the 444 questions with no gold answer are moved to the abstention column; scoring
those as retrieval questions gives 64.6% over 1,982 instead, which is higher and less honest.