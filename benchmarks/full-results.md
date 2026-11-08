# Aletheia Full Benchmark Results

Generated: 2026-10-29

---

## locomo

### Lexical (FTS5)

- Questions: 1986
- Exact match: 837/1986 (42.1%)
- Substring match: 837/1986 (42.1%)
- Latency p50: 24.5ms
- Latency p95: 34.3ms

---

## longmemeval-s

### Lexical (FTS5)

- Questions: 500
- Exact match: 169/500 (33.8%)
- Substring match: 169/500 (33.8%)
- Abstention count: 0
- Latency p50: 47.8ms
- Latency p95: 57.7ms

---

## beam-100k

### Lexical (FTS5)

- Questions: 400
- Exact match: 22/400 (5.5%)
- Substring match: 22/400 (5.5%)
- Abstention count: 160
- Abstention accuracy: 0.0%
- Latency p50: 22.8ms
- Latency p95: 64.1ms

- Conversations: 20

---

## beam-500k

### Lexical (FTS5)

- Questions: 700
- Exact match: 35/700 (5.0%)
- Substring match: 35/700 (5.0%)
- Abstention count: 280
- Abstention accuracy: 0.0%
- Latency p50: 407.1ms
- Latency p95: 794.9ms

- Conversations: 35

---

## Notes

- Scoring: exact match and substring match on gold answers vs retrieved text.
- Vector retrieval: BGE-M3 via mlx-embeddings (1024-dim, L2-normalised).
- LongMemEval-S has no explicit abstention questions in the released dataset.
- BEAM abstention accuracy is 0.0% because lexical recall returns results for all queries; abstention requires a dedicated refusal mechanism.
- Numbers are lower than published leaderboard figures because we use raw FTS5 recall on ingested subsets, not the full evaluation pipelines (LLM-as-judge, full conversation context).
