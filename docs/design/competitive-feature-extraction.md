# Aletheia Competitive Feature Extraction — REA Report

**Date:** 2026-10-29
**Scope:** Clean-room feature extraction from Mem0, Honcho, Cognee, Zep/Graphiti, LangMem, Letta, OpenViking, memorywire
**Constraint:** AGPL-3.0; clean-room only for external systems; libraries are dependencies with recorded licenses

---

## 1. What the competitors actually do (features, not marketing)

### Mem0
| Feature | How it works | Impact on recall |
|---------|-------------|------------------|
| **Reranking** | Cross-encoder or LLM reranker rescoring vector hits; opt-in per-search (`rerank=True`). Falls back to raw vector order if reranker fails. | +5–15% precision at same recall |
| **Graph memory** | Built-in entity/relationship graph (platform only); OSS has no graph. | Enables multi-hop reasoning |
| **Fact extraction + dedup** | `add()` runs LLM extraction, deduplication, then embedding. Memory is not raw text — it's cleaned facts. | Higher signal-to-noise in index |
| **Filtering** | JSON-logic filters on `user_id`, `agent_id`, `run_id`, categories, dates. | Scope control at query time |

### Honcho
| Feature | How it works | Impact |
|---------|-------------|--------|
| **Dialectic retrieval** | An agent (not a code path) answers recall queries by searching conclusions semantically, pulling supporting messages, tracing premises. | State-of-the-art benchmark results |
| **Hybrid FTS + vector + RRF** | Full-text and semantic search fused with reciprocal rank fusion. | Better recall than either alone |
| **Automatic context management** | Smart conversation summaries for infinite chats. | Lower context cost |
| **Peer cards + representations** | Compact biographical summaries built from observed messages; updated over time. | Richer recall surface |

### Cognee
| Feature | How it works | Impact |
|---------|-------------|--------|
| **14 search modes** | Graph completion, RAG, chain-of-thought graph traversal, Cypher, temporal, summaries, chunks, etc. | Task-appropriate retrieval |
| **Triplet embeddings** | `(source → relation → target)` text embedded as first-class datapoints. | Graph becomes searchable by similarity |
| **`memify()` enrichment** | Prunes stale nodes, strengthens frequent connections, reweights edges by usage, adds derived facts. | Memory improves over time |
| **Feedback weights** | Thumbs up/down on answers adjusts node/edge weights. | Direct relevance signal |
| **Three stores** | Graph (Ladybug/LanceDB/Neo4j) + Vector (LanceDB/pgvector) + Relational (SQLite/Postgres). | Each indexed for its lookup pattern |

### Zep / Graphiti
| Feature | How it works | Impact |
|---------|-------------|--------|
| **Bi-temporal model** | `valid_at` / `invalid_at` / `created_at` / `expired_at` on every edge/node. | Time-aware fact validity |
| **Hybrid BM25 + vector + graph BFS** | Three methods run concurrently; fused with RRF or cross-encoder reranking. | Best-of-three recall |
| **Search scopes** | Nodes, Edges, Episodes, Communities — each searchable independently. | Targeted retrieval |
| **Episode window** | `retrieve_episodes(reference_time, last_n)` for recent-context grounding. | Recency-aware recall |
| **Multiple search configs** | `EDGE_HYBRID_SEARCH_RRF`, `COMBINED_HYBRID_SEARCH_CROSS_ENCODER`, etc. | Tunable precision/latency |

### LangMem
| Feature | How it works | Impact |
|---------|-------------|--------|
| **Importance + recency + frequency scoring** | Recall rank = `0.5·similarity + 0.3·recency_decay + 0.2·importance`. Half-life 30 days. | Stronger ranking than raw cosine |
| **LLM extraction + consolidation** | Per-conversation LLM call extracts facts; dedup + consolidation against existing memories. | Convergent memory state |
| **Debounced ReflectionExecutor** | Consolidation fires once conversation goes quiet, not on every turn. | Lower cost, fewer redundant writes |
| **Prompt optimization (RSI)** | Rewrites system prompts from feedback — procedural memory as optimizable artifact. | Differentiated procedural layer |

### Letta (MemGPT)
| Feature | How it works | Impact |
|---------|-------------|--------|
| **Memory hierarchy** | Core memory (in-context, editable) + archival memory (out-of-context, searchable). | Bounded context window usage |
| **Memory blocks** | Named editable blocks (persona, human, etc.) that agents edit via tools. | Agent-directed memory management |
| **Agentic context engineering** | Agent decides what to edit/delete/search in its own memory. | Self-improving agents |

### OpenViking
| Feature | How it works | Impact |
|---------|-------------|--------|
| **Three-layer loading (L0/L1/L2)** | L0 = ~100-token abstract; L1 = ~2k overview; L2 = full detail on demand. | Token-efficient progressive loading |
| **Intent analysis + multi-query expansion** | LLM expands one user request into 0–5 typed queries before search. | Better coverage of ambiguous queries |
| **Hierarchical retrieval** | Vector search finds directories, then drills down level-by-level. | Structured result context |
| **Session commit + async distillation** | Archive raw messages first, then extract memories asynchronously. | Non-blocking ingestion |
| **Permission isolation** | Per-peer memory isolation by default. | Multi-tenant safety |

### memorywire
| Feature | How it works | Impact |
|---------|-------------|--------|
| **Five operations** | `remember`, `recall`, `forget`, `merge`, `expire` — stable JSON-Schema wire format. | Complete lifecycle |
| **Four memory types** | `semantic`, `episodic`, `procedural`, `emotional`. | Typed memory |
| **RRF fusion across backends** | Router fans queries to N stores, fuses with RRF (k=60). | Cross-backend retrieval |
| **STM ↔ LTM transformer** | Background task scores short-term items by recency + salience + confidence; promotes to LTM, evicts cold. | Automatic consolidation |
| **Governance channel** | HITL diff-and-approve for writes. | Human oversight |
| **Recovery capability** | Purge poisoned memory by provenance; quarantine untrusted entries. | Security incident response |

---

## 2. What Aletheia already has (do not rebuild)

| Aletheia feature | Competitor analog |
|-------------|------------------|
| Hybrid lexical + vector + RRF | Honcho, Zep, memorywire |
| L0/L1/L2 tier loading | OpenViking |
| Scope engine with deny-by-default | Mem0 filters, OpenViking permissions |
| Receipts / audit trail | memorywire governance |
| Correction supersedes with lineage | Cognee feedback weights (partial) |
| Content-conflict detection | — |
| FTS5 full-text search | — |
| sqlite-vec vector search | — |

---

## 3. Root-cause analysis: why the benchmark numbers are low

### 3.1 The evaluation metric is wrong (primary cause)

Aletheia's full benchmark (`benchmarks/full/common.py`) scores "exact" as:

```python
exact = gold_lower in combined  # substring match on retrieved text
```

**This is incorrect for LoCoMo and BEAM.** Those datasets are generative QA benchmarks:
- LoCoMo gold answers include synthesized responses like "Psychology, counseling certification" and "7 May 2023" — the latter does **not** appear verbatim in any observation record.
- BEAM gold answers are multi-sentence synthesized summaries, not verbatim dialogue quotes.
- The official LoCoMo evaluation uses **F1 + LLM-as-judge** (gpt-4o-mini), not substring exact match.

A memory system that retrieves the right context but fails substring match gets a zero — even if an LLM could answer correctly from that context.

**Effect:** Aletheia's 42.1% LoCoMo "exact" is measuring a retrieval-to-verbatim-matching pipeline, not a memory-to-answer pipeline. Competitors scoring 52–83% use LLM generation + LLM judge.

### 3.2 No reranking (secondary cause)

Aletheia fuses lexical and vector results with RRF (good), but returns raw fused order. Every major competitor adds a reranking pass:
- Mem0: cross-encoder or LLM reranker (+150ms, +5–15% precision)
- Zep: cross-encoder reranker on fused candidates
- OpenViking: rerank after global retrieval

Without reranking, borderline candidates that score moderately on both BM25 and cosine similarity can outrank the single best match.

### 3.3 No importance / recency / frequency scoring (tertiary)

LangMem and memorywire both weight recall by:
- **Importance** (LLM-assigned at write time)
- **Recency** (how recently the memory was accessed/formed)
- **Frequency** (how often recalled)

Aletheia treats all records as equally authoritative. A record that was just confirmed by a Correction should outrank a 6-month-old observation.

### 3.4 No multi-query expansion

OpenViking expands one user query into 0–5 typed queries via LLM intent analysis. Aletheia passes the raw query directly to FTS5 and the embedder. Ambiguous or paraphrased queries miss records that use different wording.

### 3.5 No answer generation in the benchmark

The full benchmark stops at retrieval and never calls an LLM to generate an answer. LoCoMo and BEAM are designed to test end-to-end memory → answer pipelines, not retrieval alone.

---

## 4. Recommended refinements, ranked by impact

### P0 — Fix Phase 9 benchmark methodology (raises scores immediately)

| Change | Rationale | Expected uplift |
|--------|-----------|-----------------|
| **Replace exact-match scoring with LLM-judge scoring** for LoCoMo and BEAM. Use gpt-4o-mini or local judge to grade retrieved-context answers against gold. | Aligns with official LoCoMo eval; accounts for synthesized answers. | +15–30 points |
| **Add an answer-generation step** in `benchmarks/full/common.py`: retrieve top-K context, call an LLM with the context + question, capture the answer, then judge. | Tests the full memory→answer pipeline that real agents use. | +10–20 points |
| **Keep substring/F1 as a secondary metric** for LongMemEval-S (factual answers often do appear verbatim). | LongMemEval-S answers are short facts that are more likely to be exact. | — |

### P1 — Add reranking (next highest ROI)

| Change | Rationale | Expected uplift |
|--------|-----------|-----------------|
| **Add optional cross-encoder reranker** (`cross-encoder/ms-marco-MiniLM-L6-v2` or local equivalent). Run after RRF fusion on top 2× candidates. | Industry standard; +5–15% precision on fused BM25+vector. | +5–10 points |
| **Fall back gracefully** if reranker model unavailable (log warning, return raw fused order). | No hard dependency for default install. | — |

### P1 — Add multi-query expansion

| Change | Rationale | Expected uplift |
|--------|-----------|-----------------|
| **Optional query expansion**: LLM rewrites query into 1–3 paraphrases before search; fuse results. | Catches phrasing mismatches; especially helps LoCoMo temporal questions. | +3–8 points |
| **Gate behind config flag** (`query_expansion.enabled`). | Zero cost when disabled. | — |

### P1 — Add importance + recency scoring

| Change | Rationale | Expected uplift |
|--------|-----------|-----------------|
| **Add `importance` field** to records (LLM-assigned at ingest time, optional). | LangMem shows importance-weighted recall outperforms pure similarity. | +3–5 points |
| **Add `last_accessed` and `access_count`** to records; bump on every recall hit. | Recency + frequency as weak signals. | +2–4 points |
| **Combine in final rank**: `0.5·RRF_score + 0.3·recency_decay + 0.2·importance`. | Mirrors LangMem's formula. | — |

### P2 — Background extraction / consolidation (Phase 11 acceleration)

| Change | Rationale | Expected uplift |
|--------|-----------|-----------------|
| **LLM-based fact extraction** during ingest: turn raw observations into atomic facts, dedupe against existing store. | Reduces noise; increases density of answer-bearing records. | +5–10 points |
| **Debounced consolidation** (LangMem pattern): run once conversation goes idle. | Avoids per-turn LLM cost. | — |

### P2 — Graph / temporal reasoning (Phase 12 acceleration)

| Change | Rationale | Expected uplift |
|--------|-----------|-----------------|
| **Entity linking + bi-temporal edges** without external graph DB (SQLite adjacency). | Zep's temporal model and Cognee's graph traversal both improve multi-hop and temporal QA. | +5–15 points on temporal/multi-hop categories |
| **BFS traversal on linked entities** during recall for multi-hop questions. | Directly addresses LoCoMo category 3 (multi-hop). | — |

---

## 5. Implementation plan

### Phase 9.1 — Benchmark methodology fix (immediate)

1. Add `benchmarks/full/judge.py` — LLM-judge scoring (gpt-4o-mini or configured model).
2. Update `benchmarks/full/common.py`:
   - `run_recall()` returns top-K context.
   - `score_answer()` generates an answer from context + LLM, then judges it.
   - Keep existing exact/substring as secondary metrics.
3. Update `benchmarks/full/locomo.py`, `longmemeval.py`, `beam.py` to use new scoring.
4. Add `judge.enabled` and `judge.model` config to `pyproject.toml` / env.
5. Adversarial review.

### Phase 9.2 — Reranker (next)

1. Add `aletheia/rerank.py` — optional cross-encoder reranker.
2. Update `aletheia/retrieval.py` `recall()`: if `rerank=True`, expand pool to `limit*4`, run reranker, reorder.
3. Add `rerank.provider`, `rerank.model` config.
4. Fallback: if model unavailable, warn and return raw fused order.
5. Adversarial review.

### Phase 9.3 — Query expansion + recency/importance scoring

1. Add `query_expansion.enabled` config; LLM rewrites query.
2. Add `importance`, `last_accessed`, `access_count` to `Record`.
3. Update final ranking formula.
4. Adversarial review.

### Phase 10+ — Accelerate Phase 11 (extraction) and Phase 12 (graph)

Move extraction and graph work earlier in the sequence, since they directly improve benchmark scores.

---

## 6. What we will NOT do (clean-room / AGPL constraints)

| Competitor feature | Why not copied | Aletheia alternative |
|-------------------|---------------|------------------|
| Mem0 managed graph memory | Closed-source platform feature | Phase 12 local graph |
| Honcho Dialectic agent | Closed-source reasoning engine | LLM-backed query expansion + reranker |
| Cognee Ladybug / Kuzu graph DB | External dependency | SQLite-adjacency local graph |
| Zep bi-temporal model | External graph DB required | SQLite `valid_from`/`valid_to` columns (Phase 12) |
| Letta memory blocks | Architectural choice; blocks ≠ scoped records | Aletheia's scope + tier model already cleaner |
| OpenViking VikingFS | Filesystem abstraction; not aligned with Aletheia's evidence-bound model | Keep Aletheia's record/substrate model |
| memorywire wire format | Useful interop layer but adds complexity | Aletheia's public `put()/recall()/rebuild_fts()` already serves |

---

## 7. Competitive positioning after refinements

| Dimension | Aletheia today | After P0–P1 | Differentiator vs. peers |
|-----------|-----------|-------------|------------------------|
| Retrieval precision | 42.1% (exact match) | ~65–75% (LLM-judge) | Comparable to Zep, behind MemHQ |
| Reranking | None | Cross-encoder | Matches Mem0/Zep |
| Query coverage | Raw query | Multi-query + reranker | Matches OpenViking |
| Scoring | Raw fused rank | RRF + recency + importance | Matches LangMem |
| Extraction | None (raw ingest) | LLM fact extraction (Phase 11) | Matches Mem0/Honcho |
| Graph | None (Phase 12) | Local entity + temporal | Matches Cognee/Zep |
| Governance | Receipts + deny-by-default | + HITL governance | Matches memorywire |
| Deployment | CLI + Python | + MCP + Docker | Matches Mem0 Cloud |
