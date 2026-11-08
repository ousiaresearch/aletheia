Analyze Aletheia's current recall implementation and identify concrete, implementable gaps or additions that could push recall past the current 53.5% hit@k. Focus on: 1) retrieval path improvements beyond what is already implemented, 2) extraction/consolidation improvements that would give recall more/better evidence, 3) lightweight reranking/context-ranking improvements, 4) query understanding improvements, 5) failure-mode analysis of the current 46.5% miss rate. Provide prioritized findings with specific file targets and implementation notes. Do not benchmark.

Current stack:
- Hybrid vector recall: BAAI/bge-m3 + sqlite-vec
- Lexical FTS5 fallback
- RRF fusion with K=60
- Cross-encoder reranking (cross-encoder/ms-marco-MiniLM-L-6-v2)
- LLM-based query expansion (3 paraphrases, RRF fused)
- Graph entity boost after RRF
- Bi-temporal validity (valid_from/valid_to)
- Entity linking with heuristic NER
- Scope-based access control

Latest local benchmark (200 QA):
- Baseline lexical-only: 20.5% hit@k, 0.093 MRR, ~12ms
- Enhanced: 53.5% hit@k, 0.332 MRR, ~1.2s

Competitor generative QA scores: Mem0 67.1%, Zep 75.1%, MemHQ 83.2%

Clean-room constraint: no reverse-engineering of competitor codebases; analyze only public docs/methodologies.
