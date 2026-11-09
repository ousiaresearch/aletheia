# Aletheia Benchmark Results (full run — Colab)

**Generated:** 2026-10-29T22:11:18Z
**Methodology:** Retrieval hit@k + MRR when evidence_ids available; token F1 fallback.
**Embedder:** none — lexical only
**Hardware:** Colab CPU (no GPU allocated)

## LoCoMo (10 conversations, 1,986 QA pairs)

| Metric | Value |
|--------|-------|
| Exact match | 55.3% |
| Token F1 | 0.356 |
| MRR | 0.356 |
| p50 latency | 77.6 ms |
| p95 latency | 147.5 ms |

## LongMemEval-S (500 questions)

| Metric | Value |
|--------|-------|
| Exact match | 33.8% |
| Token F1 | 0.014 |
| p50 latency | 153.1 ms |
| p95 latency | 295.9 ms |

## BEAM 100K (20 conversations, 400 questions)

| Metric | Value |
|--------|-------|
| Exact match | 5.5% |
| Token F1 | 0.035 |
| Abstention count | 160 (0.0% accuracy) |
| p50 latency | 86.3 ms |
| p95 latency | 191.2 ms |

## BEAM 500K (35 conversations, 700 questions)

| Metric | Value |
|--------|-------|
| Exact match | 5.0% |
| Token F1 | 0.033 |
| Abstention count | 280 (0.0% accuracy) |
| p50 latency | 1,313.2 ms |
| p95 latency | 2,896.8 ms |

## Competitive context

| System | LoCoMo | BEAM | Method |
|--------|--------|------|--------|
| **Aletheia (lexical)** | **55.3% hit@10** | **5.0–5.5%** | Retrieval MRR / F1 |
| Mem0 | 67.1% | — | LLM judge |
| Zep / Graphiti | 75.1% | — | LLM judge |
| MemHQ | 83.2% | — | LLM judge |

> Methodology note: Competitors (Mem0, Zep, MemHQ) generate answers and score with an LLM judge. Aletheia's numbers use retrieval hit@k + MRR (LoCoMo) and token F1 (BEAM, LongMemEval-S), which are strict retrieval metrics, not generative QA. A direct apples-to-apples comparison requires the same scoring method; the 55.3% is Aletheia's recall quality, not its end-to-end QA accuracy.

## Local run, 2026-11-09 (current code, lexical)

The Colab run above predates 2.1.0. This one is current code, run locally with
`python benchmarks/sample.py --n 500`, over the same LongMemEval-S questions in the local cache.

| Slice | N | Exact match | 95% CI | p50 |
|---|---|---|---|---|
| LongMemEval-S, all questions | 500 | **44.2%** (221/500) | 39.9–48.6 | 15.7 ms |
| offsets 0–49 | 50 | 88.0% | 76.2–94.4 | 15.8 ms |
| offsets 200–249 | 50 | 34.0% | 22.4–47.8 | 15.9 ms |
| offsets 400–449 | 50 | 62.0% | 48.2–74.1 | 15.7 ms |

**Read this carefully.**

- **Exact match is a retrieval-contains metric**: the gold answer string appears somewhere in the
  top-10 retrieved context. It is not answer accuracy, and it is not comparable to a competitor's
  LLM-judged number.
- **Token F1 (0.007) and MRR (0.000) are not informative in this configuration.** MRR needs gold
  evidence ids, which this slice does not carry; F1 compares a short gold answer against long
  retrieved context and therefore measures length mismatch.
- **A 50-question slice is not a score.** Three disjoint slices of the same set scored 88%, 34% and
  62% — a 54-point spread — and their 150-question aggregate (61%) sits well above the full-set mean
  (44.2%). Question order in this file is not neutral; the early questions are largely
  single-session. Every rate above needs its N and interval or it is a rumour.
- Against the Colab run's 33.8% EM over the same 500 questions, this is +10.4 points, but the token
  F1 moved the other way (0.014 → 0.007), which is a sign the two runs do not score identically.
  Treat the improvement as indicative, not established, until both runs use one scoring path.

