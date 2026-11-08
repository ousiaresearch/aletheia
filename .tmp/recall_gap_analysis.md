# Aletheia Recall Gap Analysis — Past 53.5% hit@k

**Date:** 2026-10-30  
**Scope:** Aletheia local-first memory retrieval stack at `/Users/johannross/workspace/aletheia`  
**Constraint:** No reverse-engineering of competitor code; analyze only public docs/methodologies. No benchmarking.

---

## Current Stack Summary

| Layer | Implementation | Key file |
|---|---|---|
| Lexical | FTS5 with BM25 ordering, stopword filter | `aletheia/substrate.py:fts_candidates` |
| Vector | sqlite-vec (vec0), BAAI/bge-m3 via mlx-embeddings | `aletheia/substrate.py:vector_candidates`, `aletheia/adapters.py` |
| Fusion | RRF (K=60), rank-only | `aletheia/retrieval.py:_fuse` |
| Query expansion | LLM via TokenHarbor (n=2, fail-closed) | `aletheia/query_expansion.py:expand_query` |
| Entity boost | Heuristic NER (capitalization), real-time re-extraction | `aletheia/entity_link.py:entity_boost` |
| Reranking | Cross-encoder (ms-marco-MiniLM-L-6-v2) + cosine min-max | `aletheia/retrieval.py:_rerank`, `aletheia/reranking.py` |
| Extraction | LLM fact extraction → Claims (manual utility) | `aletheia/extraction.py` |
| Temporal | Bi-temporal validity (valid_from/valid_to) | `aletheia/temporal.py` |

**Benchmark baseline (200 QA, LoCoMo-derived):**
- Lexical-only: 20.5% hit@k / 0.093 MRR / ~12ms p50
- Hybrid + cross-encoder + query expansion: 53.5% hit@k / 0.332 MRR / ~1.2s p50
- Miss rate: **46.5%**

---

## Prioritized Findings

### P0 — CRITICAL (fix first, highest expected recall gain)

#### 1. Document-level dedup eliminates multi-paragraph evidence
**File:** `aletheia/retrieval.py:237-252`

```python
document = rec.provenance.get("document") if isinstance(rec.provenance, dict) else None
key = document if document else f"record:{rid}"
if key in seen_documents:
    continue
seen_documents.add(key)
```

For ingested documents, every paragraph-Observation shares `provenance.document = "notes.txt"`. Only the **first** paragraph encountered in candidate order is returned; all subsequent paragraphs from the same document are silently dropped.

**Impact:** If paragraph 1 scores higher in fusion but paragraph 3 contains the answer, the answer is unreachable. This is a silent hard failure that directly causes misses.

**Fix:** Replace document-level dedup with "top-scoring paragraph per document" — allow up to N paragraphs from the same document, selecting the best N by `_rerank` score. Or dedup on `(document, location)` for paragraphs.

**Complexity:** Small. Changes 6 lines. Add test for "answer in paragraph 3 of 5 is found."

---

#### 2. No automatic Claims extraction at ingestion time
**Files:** `aletheia/ingest.py:Ingestor`, `aletheia/extraction.py:extract_facts`

`extraction.py` is an optional module with manual `extract_facts()` / `write_claims()` helpers. `Ingestor.ingest_document` and `ingest_turn` **never call it**.

For LoCoMo-style QA, questions target specific facts. Raw paragraph text is verbose and dilute — vector embeddings of 500-word paragraphs lose detail. Short, focused Claim records ("X attended Y on DATE") are much easier retrieval targets. This is the single biggest evidence-quality gap.

**Fix:** After writing Observations/Sources, optionally run `extract_facts()` and `write_claims(sub, ctx, claims, embedder)`. Store Claims with `epistemic_status="derived"`, `attribution="agent"`, `source_ids=[source_rid]`. This adds a second retrieval tier: Claims for fact lookup, Sources for context.

**Complexity:** Medium. Adds optional extraction step to Ingestor. Must be opt-in (LLM call, cost, latency).

---

#### 3. Query expansion is external-only and silently degrades to no-op
**File:** `aletheia/query_expansion.py:16-67`

```python
def expand_query(token_getter, query: str, n: int = 3) -> list[str]:
    token = token_getter()
    if not token:
        return [query]   # <-- silent fail-closed
```

Without a TokenHarbor token, query expansion produces zero paraphrases. Most local-first users won't configure a token. The 53.5% enhanced score includes query expansion (via the benchmark's `token_getter`), so the production default for most users is effectively **lexical-only** (20.5%).

**Fix:** Add a lightweight local fallback that doesn't require an external API:
- Character n-gram variations (e.g., swap adjacent letters for fuzzy matching)
- Simple WordNet-like synonym pairs for common query words
- A small bundled thesaurus (100-200 high-frequency term pairs)

**Complexity:** Small. Returns `[query, alt1, ...]` same as the LLM path.

---

### P1 — HIGH IMPACT (expected +5-10pp combined)

#### 4. Single-vector-per-record loses information in long passages
**Files:** `aletheia/ingest.py:200-228`, `aletheia/substrate.py`

Every Source/Observation gets exactly one embedding. A 400-word paragraph compresses into a single 1024-dim vector; BAAI/bge-m3's effective receptive context is much smaller than that. Multiple facts in one paragraph compete in the same vector.

**Fix:** During ingestion, compute **overlapping sliding-window embeddings** (window=128 tokens, stride=64) for long records. Store additional vectors keyed to the same `record_id` (or a new `chunks` table). At recall time, a match against any chunk scores the parent record.

**Complexity:** Medium. Requires substrate schema change or vector keying extension. Embedding count grows ~3x for long records.

---

#### 5. Pure RRF discards score magnitudes; score fusion is stronger
**File:** `aletheia/retrieval.py:_fuse` (lines 74-81)

```python
def _fuse(ranked_lists: list, limit: int) -> list:
    scores: dict = {}
    for ranked in ranked_lists:
        for rank, rid in enumerate(ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
```

RRF treats rank 1 and rank 10 identically (both contribute ~1/60). In practice, a candidate ranked #1 lexically and #5 vectorially should outrank one ranked #3 in both. Score-based fusion (CombSum / linear interpolation of normalized BM25 + cosine scores) consistently outperforms RRF on recall benchmarks.

**Fix:** Add `_fuse_scored()` that accepts `(rid, score)` pairs, normalizes each list to [0,1] min-max, then interpolates: `combined = α * lex_norm + (1-α) * vec_norm`. Use it when both lexical and vector paths produce scored candidates. Keep `_fuse` as the ranked-only fallback.

**Complexity:** Small. New function alongside `_fuse`, used in `_recall_one`.

---

#### 6. FTS5 query has no phrase matching
**File:** `aletheia/retrieval.py:177`

```python
match = " OR ".join(f'"{t}"' for t in q_content)
```

Individual terms are OR'd. Queries like "Melanie painted a lake" match any of those words, but not the phrase. Entities like "LGBTQ support group" split into independent terms. LoCoMo questions frequently reference specific phrases or multi-word entities.

**Fix:** Extract multi-word candidates (capitalized sequences, quoted substrings, entities from `entity_boost`) and build FTS5 `NEAR` or phrase queries:
- `"melanie painted"` — phrase query
- `"lgbtq" NEAR "support group"` — proximity query
- Fall back to term-OR when no multi-word terms found.

**Complexity:** Small. Add a `_build_fts_query(q_content, entities)` helper.

---

#### 7. Entity boost re-parses every candidate text on every recall
**File:** `aletheia/entity_link.py:110-136`

`entity_boost` calls `extract_entities(record.payload["text"])` for every candidate record during every recall. The `entity_links` table (populated by `link_entities`) already has `(record_id, entity)` mappings but is never queried.

For a pool of 20 candidates, this re-parses 20 texts per query. It also misses entity aliases and cross-references that the link table captures.

**Fix:** Rewrite `entity_boost` to query `entity_links` for candidate record_ids instead of re-extracting. Use a `LEFT JOIN` or batch query to get all entities for the candidate set in one SQL call.

**Complexity:** Small. Performance gain + slight accuracy gain from consistent entity resolution.

---

### P2 — MEDIUM IMPACT (expected +3-7pp combined)

#### 8. Cross-encoder pool is the same size as the embedder-rerank pool
**File:** `aletheia/retrieval.py:174, 262-267`

```python
q_pool = limit * 4   # 20 for k=5
...
if use_cross_encoder and cross_encoder is not None and len(items) > limit:
    items = cross_encoder.rerank(query, items, top_k=limit)
```

Cross-encoder reranking operates on the same 20 candidates already truncated by cosine rerank. More candidates before cross-encoder = higher chance the gold item survives to rerank.

**Fix:** When `use_cross_encoder=True`, draw `cross_encoder_pool = max(limit * 4, 50)` from the fused candidate list before cosine rerank, then cross-encoder rerank to `limit`. This adds ~30 extra model calls (cheap for ms-marco-MiniLM).

**Complexity:** Small.

---

#### 9. No query decomposition for multi-hop questions
**File:** `aletheia/query_expansion.py`, `aletheia/retrieval.py`

LoCoMo questions like "What did X do before Y happened?" require two reasoning steps. A single-query retrieval can only surface evidence matching the whole compound question, not the individual constraints.

**Fix:** Add a lightweight decomposition step: detect conjunction patterns ("and", "before", "after", "also") and split into sub-queries. Run `_recall_one` for each sub-query, then fuse results with `_fuse`. This is a modest extension of the existing query-expansion pattern.

**Complexity:** Medium. Rule-based decomposition (no LLM) is sufficient for a first pass.

---

#### 10. No automatic temporal extraction from text
**File:** `aletheia/extraction.py`, `aletheia/temporal.py`

`temporal.py` provides `set_validity`/`close_window`/`effective_at` but no mechanism populates `valid_from`/`valid_to` from observation text. Dates mentioned in conversations ("on 8 May 2023") remain invisible to temporal filtering.

**Fix:** In `_extract_facts`, add date entity detection (regex for ISO dates, "8 May 2023" patterns, relative dates like "last year"). For temporal facts, set `valid_from` to the parsed timestamp. This lets `effective_at()` filter recall to "what was true at time T."

**Complexity:** Medium. Date parsing is imperfect but even coarse extraction helps temporal QA.

---

### P3 — LOW IMPACT / NICE-TO-HAVE

#### 11. Consolidation uses exact text match, not embeddings
**File:** `aletheia/extraction.py:192-229`

`consolidate_scope` deduplicates Claims by `text.lower().strip()`. Two semantically equivalent Claims with different phrasing ("attended meeting" vs "was at meeting") are counted as distinct. Over time this dilutes retrieval.

**Fix:** Use embedding-based near-duplicate detection: compute centroid of existing Claims, compare new Claim against closest centroid with cosine threshold (0.95). Report as duplicate but don't auto-supersede (user-attributed Corrections are required per design).

**Complexity:** Small. Requires embedder availability during consolidation.

---

#### 12. No query intent classification
**File:** New: `aletheia/query_classifier.py` or inline in `aletheia/retrieval.py`

All queries are treated identically regardless of type. Temporal queries, entity-centric queries, and factual lookups each benefit from different evidence weighting:
- Temporal → boost records with date entities, apply `effective_at` filter
- Entity-centric → increase `entity_boost` weight, prefer Claims
- Factual lookup → prefer BM25-high + cross-encoder

**Fix:** Add a 3-class classifier using simple heuristics (presence of date words, question word, entity count). Zero model calls; pure regex + word list. Adjust recall parameters per class.

**Complexity:** Small.

---

#### 13. Vector candidates: inconsistent scoring across the pipeline
**File:** `aletheia/substrate.py:730-792`, `aletheia/retrieval.py:84-117`

`vector_candidates` returns record IDs ranked by **L2 distance** (lower is better). `_rerank` re-scores by **cosine similarity** (higher is better). RRF fusion combines L2-ranked IDs with BM25-ranked IDs using rank-only scoring. The score semantics are inconsistent across stages.

**Impact:** Low for recall (RRF is rank-robust), but this makes it impossible to do score-based fusion without a normalization layer.

**Fix:** Document the scoring contract per stage. If moving to score fusion (finding #5), normalize L2→cosine at the `vector_candidates` boundary.

**Complexity:** Small.

---

#### 14. Reranker gap: cross-encoder queries `selected_texts` but Item has no `selected_texts`
**File:** `aletheia/reranking.py:43`, `aletheia/retrieval.py:34-51`

`CrossEncoderReranker.rerank` accesses `item.selected_texts[0]`, but `Item` dataclass in `retrieval.py` has `text`, not `selected_texts`. This would raise `AttributeError` at runtime if cross-encoder is actually invoked.

Looking at `benchmarks/full/common.py:124-126`, `selected_texts` is populated from `item.text` before cross-encoding in the benchmark harness. But if cross-encoder is called directly (e.g., from `recall()`), it fails.

**Fix:** Either add `selected_texts` property to `Item` that returns `[self.text]`, or change `reranking.py` to use `item.text`.

**Complexity:** Trivial. One-line fix.

---

## Failure-Mode Analysis: 46.5% Miss Rate

Based on code inspection and LoCoMo dataset characteristics:

| Failure Mode | Estimated Contribution | Root Cause | Fix Priority |
|---|---|---|---|
| **Multi-paragraph documents: only first paragraph returned** | ~10-15pp | Document-level dedup | P0 (#1) |
| **No Claims evidence tier** | ~8-12pp | Extraction not wired into ingestion | P0 (#2) |
| **Query expansion silently disabled without TokenHarbor** | ~5-8pp | No local fallback | P0 (#3) |
| **Long passages compressed into single vector** | ~5-8pp | No multi-vector / chunking | P1 (#4) |
| **RRF discards score information** | ~3-5pp | Rank-only fusion | P1 (#5) |
| **Phrase queries not supported** | ~3-5pp | Term-only FTS query | P1 (#6) |
| **Entity boost re-parses instead of using precomputed links** | ~2-3pp | entity_links table unused | P1 (#7) |
| **Small cross-encoder pool** | ~2-3pp | pool = 4×limit | P2 (#8) |
| **No multi-hop decomposition** | ~3-5pp | Single-query recall | P2 (#9) |
| **Temporal facts not extracted** | ~1-2pp | No date parsing in extraction | P2 (#10) |

These are additive estimates; actual interaction effects may differ. The three P0 fixes alone address an estimated **23-35pp** of misses, which would push hit@k from 53.5% to **~65-75%** range before P1 items.

---

## Recommended Implementation Order

1. **Fix #14** (cross-encoder AttributeError) — 5 min, prevents runtime crash
2. **Fix #1** (document dedup) — 1 hour, highest recall impact
3. **Fix #3** (local query expansion fallback) — 2 hours, enables feature for all users
4. **Fix #2** (wire extraction into ingestion) — 3 hours, biggest evidence quality gain
5. **Fix #5** (score fusion) — 2 hours, improves ranking quality
6. **Fix #6** (phrase FTS queries) — 1 hour, better lexical precision
7. **Fix #7** (use entity_links table) — 1 hour, faster + more accurate boost
8. **Fix #4** (multi-vector per record) — 4 hours, requires schema work
9. **Fix #8** (larger cross-encoder pool) — 30 min
10. **Fix #9** (query decomposition) — 3 hours
11. **Fix #10** (temporal extraction) — 2 hours
12. **Fix #11** (embedding-based consolidation) — 2 hours
13. **Fix #12** (query intent classifier) — 2 hours

Items 1-7 are small, low-risk changes that together should push past 60% hit@k. Items 8-13 are medium-effort improvements that can push toward the 70-75% range.
