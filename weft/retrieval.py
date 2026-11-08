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


def _chain_stamp(value) -> float:
    """Writer-controlled created_at -> a finite number; anything else sorts as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) else 0.0


def _neighbor_expand(sub, candidate_ids: list[str], limit: int, scopes: list[str]) -> list[str]:
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
    for scope in scopes:
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
                    conv_chain.setdefault(conv_id, []).append((rid, _chain_stamp(payload.get("created_at"))))
            except Exception:
                continue
    
    for conv_id, chain in sorted(conv_chain.items()):
        chain.sort(key=lambda x: (x[1], x[0]))
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
        
        new_neighbors = sorted(n for n in neighbors if n not in seen)
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
    neighbor: bool = False  # adjacent-turn context, not a query match; ranked after matches

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


def _fuse_scores(ranked_lists: list) -> dict:
    """Reciprocal-rank fusion scores: each list contributes 1 / (RRF_K + rank)."""
    scores: dict = {}
    for ranked in ranked_lists:
        for rank, rid in enumerate(ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def _fuse(ranked_lists: list, limit: int) -> list:
    """Reciprocal-rank fusion: each list contributes 1 / (RRF_K + rank)."""
    scores = _fuse_scores(ranked_lists)
    order = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [rid for rid, _ in order[:limit]]


class IndexIncompleteError(ValueError):
    """A readable scope's full-text or vector index is incomplete or stale."""


def _bm25_scores(sub, match, ctx, scopes, ids):
    """{record_id: bm25} from the same FTS5 corpus and ``bm25(corpus)`` that
    ``fts_candidates`` ranks by (lower is better). Ranks are corpus-level, so asking
    for a subset of ids returns the identical values the candidate query ordered by.
    """
    if not ids:
        return {}
    placeholders = ",".join("?" * len(scopes))
    where = f" WHERE records.scope IN ({placeholders})"
    local = sub._fts_visible_corpus(ctx, scopes, where, tuple(scopes))
    marks = ",".join("?" * len(ids))
    rows = local.execute(
        f"SELECT record_id, bm25(corpus) FROM corpus WHERE corpus MATCH ? AND record_id IN ({marks})",
        (match, *ids)).fetchall()
    return {rid: score for rid, score in rows}


def _boost_adjacent_pairs(candidate_ids, scores, tied=None):
    """Tiebreak only: swap an adjacent pair (fixed bands 0-1, 2-3, ...) when the
    boost favours the lower one AND ``tied(a, b)`` says base relevance is equal.

    Base relevance is BM25 in lexical mode and the fused (RRF) score in hybrid
    mode; equal BM25 alone does not tie in hybrid. A boost therefore can never
    lift a candidate over a strictly more relevant one, so agent-controlled
    metadata cannot pin rank 1. ``tied=None``
    treats every pair as tied (unit-test hook only).
    """
    import math
    result = list(candidate_ids)
    for i in range(0, len(result) - 1, 2):
        a, b = result[i:i + 2]
        left = float(scores.get(a, 0.0))
        right = float(scores.get(b, 0.0))
        if not math.isfinite(left) or not math.isfinite(right):
            raise ValueError("non-finite boost")
        if right > left and (tied is None or tied(a, b)):
            result[i:i + 2] = [b, a]
    return result


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
    cross_encoder = CrossEncoderReranker() if use_cross_encoder else None
    
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

    lex_scores: list = []  # one {record_id: bm25} per lexical query, in query order
    query_lists: list = []  # per query, the ranked lists that were fused (hybrid)

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
            lex = sub.fts_candidates(match, ctx, q_pool, scopes=readable)
            try:
                lex_scores.append(_bm25_scores(sub, match, ctx, readable, lex))
            except Exception:
                lex_scores.append({})  # no true rank available: nothing counts as tied
            ranked.append(lex)
        if q_vector and embedder is not None:
            ranked.append(sub.vector_candidates(q_embedding, ctx, q_pool, scopes=readable, model_id=embedder.model_id))
        query_lists.append(ranked)
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
                    raise IndexIncompleteError("lexical recall requires a complete full-text index for every readable scope")
        if use_vector:
            assert embedder is not None
            for scope in readable:
                if not sub.vector_index_complete(scope, ctx, model_id=embedder.model_id):
                    raise IndexIncompleteError("hybrid/vector recall requires a complete vector index for every readable scope")
        
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

        # Boosts are bounded to fixed adjacent rank bands. They cannot stack
        # to move a weak candidate arbitrarily far above the base retrieval.
        graph_applied = temporal_applied = False
        fused = None
        if mode == "hybrid" and query_lists:
            if len(all_ranked) > 1:
                fused = _fuse_scores(all_ranked)
            elif len(query_lists) == 1:
                fused = _fuse_scores(query_lists[0])

        def _tied(a, b):
            """Tied only when both hold a true BM25 under every lexical query and the
            values are equal. Anything unranked (vector-only, missing) is never tied.
            In hybrid mode the pair must also have equal FUSED relevance."""
            if not lex_scores:
                return False
            if mode == "hybrid":
                if fused is None or a not in fused or b not in fused or fused[a] != fused[b]:
                    return False
            for scores in lex_scores:
                if a not in scores or b not in scores or scores[a] != scores[b]:
                    return False
            return True

        try:
            graph_scores = entity_boost(sub, ctx, query, candidate_ids, scopes=readable)
            reordered = _boost_adjacent_pairs(candidate_ids, graph_scores, _tied)
            graph_applied = reordered != candidate_ids
            candidate_ids = reordered
        except Exception:
            pass

        try:
            from weft.temporal import temporal_boost
            temporal_scores = temporal_boost(sub, ctx, query, candidate_ids)
            reordered = _boost_adjacent_pairs(candidate_ids, temporal_scores, _tied)
            temporal_applied = reordered != candidate_ids
            candidate_ids = reordered
        except Exception:
            pass

        # Confidence compares only adjacent lexically tied Claims, never a Claim to a Source.
        import math
        for i in range(0, len(candidate_ids) - 1, 2):
            try:
                left, right = (sub._lookup_record(rid) for rid in candidate_ids[i:i + 2])
                if not left or not right or left.record_type != "Claim" or right.record_type != "Claim":
                    continue
                if not _tied(*candidate_ids[i:i + 2]):
                    continue
                a = float(left.payload.get("confidence", 0.5))
                b = float(right.payload.get("confidence", 0.5))
                if math.isfinite(a) and math.isfinite(b) and max(0.0, min(1.0, b)) > max(0.0, min(1.0, a)):
                    candidate_ids[i:i + 2] = candidate_ids[i:i + 2][::-1]
            except (TypeError, ValueError, AttributeError):
                continue

        # P1: neighbor expansion — add adjacent-turn neighbors from the same
        # conversation to give the ranker more context.
        # Neighbors are context, not matches: they are capped and always ranked after every
        # record that matched, so writer-controlled conversation_id/created_at cannot push
        # a non-matching record ahead of a matching one.
        neighbor_ids: set = set()
        try:
            matched = list(candidate_ids)
            expanded_ids = _neighbor_expand(sub, matched, limit, readable)
            neighbor_ids = set(expanded_ids) - set(matched)
            candidate_ids = matched + [n for n in expanded_ids if n in neighbor_ids]
        except Exception:
            neighbor_ids = set()

        items: list[Item] = []
        seen_record_ids: set = set()
        for rid in candidate_ids:
            if len(items) >= pool:
                break
            if rid in seen_record_ids:
                continue
            seen_record_ids.add(rid)
            rec = sub.get(rid, ctx)  # gated again: defence in depth
            if rec is None or rec.scope not in readable:
                receipt.exclusions.append("withheld by content rule")
                continue
            if rec.record_type in ("Summary", "RecallReceipt"):
                receipt.exclusions.append("composite artifact")
                continue
            text = rec.payload.get("text", "") if isinstance(rec.payload, dict) else ""
            tier, shown = _pick_tier(text, terms)
            derived = bool(isinstance(rec.provenance, dict) and rec.provenance.get("machine_derived"))
            is_summary = rec.record_type == "Summary" or "-ss-" in rid or "summary" in rid.lower()
            items.append(Item(rid, rec.scope, text, tier, derived, is_summary=is_summary,
                              neighbor=rid in neighbor_ids))
            receipt.selected.append(rid)
            if _TIER_ORDER[tier] > _TIER_ORDER[receipt.tier]:
                receipt.tier = tier

        # Matches-first partition BEFORE any rerank or truncation: neighbors are context only
        # and must never displace a matching record at any stage. Matches keep their ranked
        # slots (reranked only among themselves); neighbors fill the remaining slots and are
        # reranked only among themselves.
        matches = [it for it in items if not it.neighbor]
        neighbors = [it for it in items if it.neighbor]

        def _order(group, slots):
            """Cross-encoder (when available) or bi-encoder rerank of one group, cut to slots."""
            applied = False
            if slots <= 0 or not group:
                return [], applied
            if cross_encoder is not None and group:
                applied = True
                return cross_encoder.rerank(query, group, top_k=len(group))[:slots], applied
            # Skip bi-encoder when the cross-encoder already reranked: it is more accurate.
            if rerank and len(group) > slots:
                return _rerank(query, group, embedder, slots), applied
            return group[:slots], applied

        matches, cross_encoder_applied = _order(matches, limit)
        neighbors, ce_neighbors = _order(neighbors, limit - len(matches))
        cross_encoder_applied = cross_encoder_applied or ce_neighbors
        items = matches + neighbors

    # Final truncation + receipt recording happens outside the read transaction.
    selected = items[:limit]
    receipt.selected = [it.record_id for it in selected]
    receipt.tier = max((it.tier for it in selected), key=lambda tier: _TIER_ORDER[tier], default="L0")
    receipt.layers = [
        layer for layer in [
            "lexical" if use_lexical else None,
            "vector" if use_vector else None,
            "query_expansion" if len(expanded_queries) > 1 else None,
            "cross_encoder" if cross_encoder_applied else None,
            "graph_boost" if graph_applied else None,
            "temporal_boost" if temporal_applied else None,
        ] if layer is not None
    ]
    if ctx is not None:
        sub.record_receipt("recall", readable,
                           {"selected": len(selected), "excluded": len(receipt.exclusions)}, ctx)
    return selected, receipt
