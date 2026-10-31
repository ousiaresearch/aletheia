"""Phase 3 retrieval (spec §8, D3-1, D3-2, D3-11, D3-12).

Scope is decided before any candidate is read: only the caller's readable scopes
(optionally narrowed by scope_hint) are queried, lexically and by vector. Every
candidate is checked again by the read gate. Every recall leaves a receipt holding
scope names and counts, never record text.

Modes: "lexical" (FTS5), "vector" (sqlite-vec, needs an embedder), and "hybrid"
(reciprocal-rank fusion of both). D3-1: vector is a default only after the recall
gate passes (see eval/recall_gate.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from weft.entity_link import entity_boost

ABSTRACT_CHARS = 400  # L0 abstract (D3-11)
OVERVIEW_CHARS = 2000  # L1 overview
_TIER_ORDER = {"L0": 0, "L1": 1, "L2": 2}
MODES = ("lexical", "vector", "hybrid")
RRF_K = 60  # standard reciprocal-rank-fusion constant
# Function words must not generate lexical matches (found by the gate's paraphrase split:
# "the" alone pulled unrelated notes above the vector results during fusion).
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "how", "i",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "what", "when",
    "where", "which", "who", "why", "with", "you", "your",
})


def _neighbor_expand(sub, candidate_ids: list[str], limit: int) -> list[str]:
    """P1: add adjacent-turn neighbors from the same conversation.
    
    For each candidate that has a conversation_id in provenance, find the
    immediately preceding and following records in that conversation and
    prepend them to the candidate pool. This gives the ranker more context
    without changing the recall modes.
    """
    if not candidate_ids:
        return []
    
    expanded = list(candidate_ids)
    seen = set(expanded)
    conv_to_rids: dict[str, list[str]] = {}
    
    for rid in candidate_ids:
        try:
            rec = sub._lookup_record(rid)
            if not rec or rec.record_type not in ("Source", "Observation"):
                continue
            conv_id = (rec.payload.get("provenance") or {}).get("conversation_id") if isinstance(rec.payload, dict) else None
            if not conv_id:
                continue
            conv_to_rids.setdefault(conv_id, []).append(rid)
        except Exception:
            continue
    
    if not conv_to_rids:
        return expanded
    
    # P1 perf: build one in-memory index per scope instead of N LIKE queries.
    conv_chain: dict[str, list[tuple[str, float]]] = {}
    for scope in sub.scope_engine.known_scopes:
        try:
            rows = sub._conn.execute(
                "SELECT record_id, payload FROM records WHERE scope = ?", (scope,)
            ).fetchall()
        except Exception:
            continue
        for rid, payload_json in rows:
            try:
                payload = json.loads(payload_json)
                conv_id = (payload.get("provenance") or {}).get("conversation_id")
                if conv_id and conv_id in conv_to_rids:
                    conv_chain.setdefault(conv_id, []).append((rid, payload.get("created_at") or 0))
            except Exception:
                continue
    
    for conv_id, chain in conv_chain.items():
        chain.sort(key=lambda x: x[1])
        rid_to_idx = {rid: i for i, (rid, _) in enumerate(chain)}
        neighbors: set[str] = set()
        for rid, _ in chain:
            if rid not in conv_to_rids.get(conv_id, []):
                continue
            idx = rid_to_idx[rid]
            for offset in (-1, 1):
                nidx = idx + offset
                if 0 <= nidx < len(chain):
                    neighbors.add(chain[nidx][0])
        
        new_neighbors = [n for n in neighbors if n not in seen]
        expanded = expanded + new_neighbors
        seen.update(new_neighbors)
        
        if len(expanded) >= limit * 4:
            break
    
    return expanded[:limit * 4]


@dataclass(frozen=True)
class Item:
    record_id: str
    scope: str
    text: str
    tier: str
    derived: bool = False  # R-P3-12: machine-derived text (OCR, transcript, description)
    is_summary: bool = False

    def render(self) -> str:
        """The only form recalled text may take when handed back to a model (D3-5).

        Wrapped in the recall fence so re-ingestion strips it instead of storing it as
        user speech (review G-P3-4). Tag tokens inside the text are escaped (G-P3-8).
        Machine-derived text carries a label so it is never presented as verbatim (D3-6).
        """
        safe = re.sub(r"<(/?weft-recall)", r"&lt;\1", self.text, flags=re.IGNORECASE)
        label = ' derived="machine"' if self.derived else ""
        return f'<weft-recall scope="{self.scope}"{label}>{safe}</weft-recall>'


@dataclass
class Receipt:
    kind: str = "recall"
    scope_consulted: list = field(default_factory=list)
    tier: str = "L0"
    selected: list = field(default_factory=list)
    exclusions: list = field(default_factory=list)
    mode: str = "lexical"
    layers: list = field(default_factory=list)


def _pick_tier(text: str, terms: list) -> tuple[str, str]:
    """Smallest tier whose text contains a query term; L0 when none does (D3-11)."""
    tiers = [("L0", text[:ABSTRACT_CHARS]), ("L1", text[:OVERVIEW_CHARS]), ("L2", text)]
    for name, shown in tiers:
        lowered = shown.lower()
        if any(t in lowered for t in terms):
            return name, shown
    return "L0", tiers[0][1]


def _fuse(ranked_lists: list, limit: int) -> list:
    """Reciprocal-rank fusion: each list contributes 1 / (RRF_K + rank)."""
    scores: dict = {}
    for ranked in ranked_lists:
        for rank, rid in enumerate(ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    order = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [rid for rid, _ in order[:limit]]


def _cosine(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _rerank(query: str, items, embedder, top_k: int):
    """Re-score fused candidates by query–candidate cosine similarity with min-max normalisation."""
    if embedder is None or not items:
        return items[:top_k]
    try:
        q_emb = embedder.embed_query(query)
        texts = [it.text for it in items]
        try:
            cand_embs = embedder.embed_documents(texts)
        except AttributeError:
            cand_embs = [embedder.embed_document(t) for t in texts]
        if len(cand_embs) != len(items):
            return items[:top_k]
        scored = [(_cosine(q_emb, emb), it) for it, emb in zip(items, cand_embs)]
        # Min-max normalise so a weak absolute cosine still yields a meaningful spread
        if scored:
            vals = [s for s, _ in scored]
            lo, hi = min(vals), max(vals)
            if hi > lo:
                scored = [((s - lo) / (hi - lo), it) for s, it in scored]
            else:
                scored = [(1.0, it) for _, it in scored]
        scored.sort(key=lambda x: (-x[0], x[1].record_id))
        return [it for _, it in scored[:top_k]]
    except Exception:
        return items[:top_k]


from weft.reranking import CrossEncoderReranker  # noqa: E402


def recall(sub, query: str, ctx, scope_hint: str | None = None, limit: int = 5,
           mode: str = "hybrid", embedder=None, rerank: bool | None = None,
           use_cross_encoder: bool = False, token_getter=None):
    """Recall over the caller's readable scopes. Returns (items, receipt)."""
    if mode not in MODES:
        raise ValueError(f"unknown recall mode: {mode!r}")
    if mode != "lexical" and embedder is None:
        raise ValueError(f"mode {mode!r} needs an embedder")
    # None means "auto": rerank is on for hybrid/vector where we have an embedder.
    if rerank is None:
        rerank = mode in ("vector", "hybrid") and embedder is not None
    cross_encoder = None
    if use_cross_encoder:
        try:
            cross_encoder = CrossEncoderReranker()
        except Exception:
            pass
    
    # Optional query expansion: generate alternative phrasings and fuse results.
    expanded_queries = [query]
    if token_getter is not None and mode in ("hybrid", "lexical"):
        try:
            from weft.query_expansion import expand_query, expand_query_hyde, expand_query_multihop
            expanded_queries = expand_query(token_getter, query, n=2)
            # P1: HyDE adds a hypothetical answer to the candidate pool.
            hyde = expand_query_hyde(token_getter, query)
            expanded_queries.extend(hyde[1:])
            # P1: multi-hop adds sub-questions for compound queries.
            multihop = expand_query_multihop(token_getter, query)
            expanded_queries.extend(multihop[1:])
            # Deduplicate while preserving order.
            seen = set()
            deduped = []
            for q in expanded_queries:
                if q not in seen:
                    deduped.append(q)
                    seen.add(q)
            expanded_queries = deduped
        except Exception:
            pass
    engine = sub.scope_engine
    readable = sorted(
        s for s in engine.known_scopes if engine.authorize_read(s, ctx, "").allowed
    )
    if scope_hint is not None:
        readable = [s for s in readable if s == scope_hint]
    receipt = Receipt(scope_consulted=readable, mode=mode)
    terms = re.findall(r"\w+", (query or "").lower())
    content_terms = [t for t in terms if t not in STOPWORDS]
    use_lexical = mode in ("lexical", "hybrid") and bool(content_terms)
    use_vector = mode in ("vector", "hybrid")
    if not readable or (not use_lexical and not use_vector):
        if ctx is not None:
            sub.record_receipt("recall", readable, {"selected": 0, "excluded": 0}, ctx)
        return [], receipt

    def _recall_one(q: str):
        """Core recall for a single query string. Returns candidate_ids list."""
        q_terms = re.findall(r"\w+", (q or "").lower())
        q_content = [t for t in q_terms if t not in STOPWORDS]
        q_lexical = mode in ("lexical", "hybrid") and bool(q_content)
        q_vector = mode in ("vector", "hybrid")
        q_embedding = embedder.embed_query(q) if q_vector and embedder else None
        q_pool = limit * 6  # match outer pool
        ranked = []
        if q_lexical:
            # P1: preserve exact phrases from the original query for FTS5 phrase matching.
            quoted = re.findall(r'"([^"]+)"', q)
            if quoted:
                phrase_parts = [f'"{phrase}"' for phrase in quoted]
                if q_content:
                    phrase_parts.append(" OR ".join(q_content))
                match = " OR ".join(phrase_parts)
            else:
                # Use quoted OR for both short and long queries.
                # NEAR/N is not supported by this FTS5 build and produced a
                # syntax error that was silently swallowed, leaving candidates
                # empty for 2-3 term queries.
                match = " OR ".join(f'"{t}"' for t in q_content)
            ranked.append(sub.fts_candidates(match, ctx, q_pool, scopes=readable))
        if q_vector and embedder is not None:
            ranked.append(sub.vector_candidates(q_embedding, ctx, q_pool, scopes=readable, model_id=embedder.model_id))
        return ranked[0] if len(ranked) == 1 else _fuse(ranked, q_pool)

    # D3-1: embedding computed outside the database transaction so long-running
    # model inference does not pin a WAL snapshot or hold the connection lock.
    pool = limit * 6  # P1: wider candidate pool for cross-encoder rerank
    # Snapshot-time consistency: completeness check, candidate selection, and
    # per-record reads must observe one view (review G-P3-7). record_receipt
    # writes its own BEGIN IMMEDIATE after this block.
    with sub._read_transaction():
        if use_lexical and not use_vector:
            for scope in readable:
                if not sub.fts_index_complete(scope, ctx):
                    raise ValueError("lexical recall requires a complete full-text index for every readable scope")
        if use_vector:
            assert embedder is not None
            for scope in readable:
                if not sub.vector_index_complete(scope, ctx, model_id=embedder.model_id):
                    raise ValueError("hybrid/vector recall requires a complete vector index for every readable scope")
        
        # Run recall for each expanded query and fuse candidate IDs.
        all_ranked = []
        for q in expanded_queries:
            try:
                all_ranked.append(_recall_one(q))
            except ValueError:
                # Re-raise dimension and other validation errors — they
                # indicate a misconfigured embedder, not a recoverable
                # retrieval failure.
                raise
            except Exception:
                # Swallow transient/query-expansion errors so one bad
                # expansion does not abort the whole recall.
                pass
        if all_ranked:
            candidate_ids = _fuse(all_ranked, pool) if len(all_ranked) > 1 else all_ranked[0]
        else:
            candidate_ids = []

        # Phase 12: graph boost — reorder candidates by entity overlap with query.
        graph_scores = {}
        try:
            graph_scores = entity_boost(sub, ctx, query, candidate_ids)
            if graph_scores:
                candidate_ids.sort(key=lambda rid: (-graph_scores.get(rid, 0.0), candidate_ids.index(rid)))
        except Exception:
            pass

        # P2: temporal boost — when the query asks about time, promote records
        # that contain date expressions (e.g., summaries with anchor dates).
        temporal_scores = {}
        try:
            from weft.temporal import temporal_boost
            temporal_scores = temporal_boost(sub, ctx, query, candidate_ids)
            if temporal_scores:
                candidate_ids.sort(key=lambda rid: (-temporal_scores.get(rid, 0.0), candidate_ids.index(rid)))
        except Exception:
            pass

        # Confidence tiebreaker for Claims: higher-confidence claims rank higher
        # without overriding strong lexical/vector matches.
        try:
            conf_scores: dict[str, float] = {}
            for rid in candidate_ids:
                rec = sub._lookup_record(rid)
                if rec and rec.record_type == "Claim" and isinstance(rec.payload, dict):
                    try:
                        conf_scores[rid] = float(rec.payload.get("confidence", 0.5))
                    except (TypeError, ValueError):
                        pass
            if conf_scores:
                candidate_ids.sort(key=lambda rid: (-conf_scores.get(rid, 0.0), candidate_ids.index(rid)))
        except Exception:
            pass
        
        # P1: neighbor expansion — add adjacent-turn neighbors from the same
        # conversation to give the ranker more context.
        try:
            candidate_ids = _neighbor_expand(sub, candidate_ids, limit)
        except Exception:
            pass

        items: list[Item] = []
        seen_record_ids: set = set()
        for rid in candidate_ids:
            if len(items) >= pool:
                break
            if rid in seen_record_ids:
                continue
            seen_record_ids.add(rid)
            rec = sub.get(rid, ctx)  # gated again: defence in depth
            if rec is None:
                receipt.exclusions.append("withheld by content rule")
                continue
            if rec.record_type in ("Summary", "RecallReceipt"):
                receipt.exclusions.append("composite artifact")
                continue
            text = rec.payload.get("text", "") if isinstance(rec.payload, dict) else ""
            tier, shown = _pick_tier(text, terms)
            derived = bool(isinstance(rec.provenance, dict) and rec.provenance.get("machine_derived"))
            is_summary = rec.record_type == "Summary" or "-ss-" in rid or "summary" in rid.lower()
            items.append(Item(rid, rec.scope, text, tier, derived, is_summary=is_summary))
            receipt.selected.append(rid)
            if _TIER_ORDER[tier] > _TIER_ORDER[receipt.tier]:
                receipt.tier = tier

        # Cross-encoder rerank on the pooled candidates before truncation.
        # Run before bi-encoder rerank so the cross-encoder sees the full pool.
        cross_encoder_applied = False
        if use_cross_encoder and cross_encoder is not None and len(items) > limit:
            cross_encoder_applied = True
            items = cross_encoder.rerank(query, items, top_k=len(items))

        # Rerank over the full pool before truncation to limit.
        # Skip when cross-encoder already reranked the same pool — it is more
        # accurate than bi-encoder cosine, and both would hit the same latency.
        if rerank and len(items) > limit and not cross_encoder_applied:
            items = _rerank(query, items, embedder, limit)

    # Final truncation + receipt recording happens outside the read transaction.
    selected = items[:limit]
    receipt.selected = [it.record_id for it in selected]
    receipt.layers = [
        layer for layer in [
            "lexical" if use_lexical else None,
            "vector" if use_vector else None,
            "query_expansion" if len(expanded_queries) > 1 else None,
            "cross_encoder" if cross_encoder_applied else None,
            "graph_boost" if graph_scores else None,
            "temporal_boost" if temporal_scores else None,
        ] if layer is not None
    ]
    if ctx is not None:
        sub.record_receipt("recall", readable,
                           {"selected": len(selected), "excluded": len(receipt.exclusions)}, ctx)
    return selected, receipt
