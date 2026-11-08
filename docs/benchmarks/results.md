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
