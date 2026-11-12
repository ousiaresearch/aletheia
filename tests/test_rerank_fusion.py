"""Regression tests for the rerank/fusion defects measured on LongMemEval-S.

The finding these pin down: `lexical` found the evidence for q201 while `lexical + bi-encoder rerank`
missed it, because `_rerank` re-sorted candidates by normalised cosine alone and threw the fused
(RRF) order away. A rerank that uses the same bi-encoder that produced the vector scores cannot add
information, so it must not be able to overturn the fusion.
"""

from __future__ import annotations

import pytest

from aletheia.retrieval import (RERANK_BAND_DIVISOR, RERANK_MIN_BAND, _fuse_scores, _rerank,
                                recall)
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import VEC_DIM, Record, Substrate


def _padded(head: list[float]) -> list[float]:
    """Vectors go through ``vector_put``, which enforces VEC_DIM. Zero padding leaves cosine
    similarity unchanged, so the ranking behaviour under test is untouched."""
    return head + [0.0] * (VEC_DIM - len(head))


class _Item:
    """The reranker only needs record_id and text; a stand-in keeps these tests about ranking."""

    def __init__(self, record_id: str, text: str = ""):
        self.record_id = record_id
        self.text = text
        self.scope = "benchmark"
        self.tier = "L1"
        self.derived = False
        self.is_summary = False
        self.neighbor = False


class _AdversarialEmbedder:
    """Cosine similarity that ranks the fused winner LAST and the fused loser FIRST.

    This is the shape of the failure: the embedding stage disagrees with full confidence.
    """

    model_id = "test/adversarial"
    dim = 4

    def __init__(self, order: list[str]):
        self._order = order  # best -> worst as the embedder would have it

    def embed_query(self, text: str):
        return _padded([1.0, 0.0])

    def embed_document(self, text: str):
        # Query vector is [1, 0], so [1, 0] is maximum similarity. ``order`` lists ids from most to
        # least similar as this embedder sees them; the fused ranking disagrees by construction.
        if text in self._order[:1]:
            return _padded([1.0, 0.0])
        if text in self._order[-1:]:
            return _padded([0.0, 1.0])
        return _padded([0.5, 0.5])


class _UniformEmbedder:
    """Every cosine identical: the rerank must not invent a spread from noise."""

    model_id = "test/uniform"
    dim = 2

    def embed_query(self, text: str):
        return _padded([1.0, 0.0])

    def embed_document(self, text: str):
        return _padded([1.0, 0.0])


def _items(ids):
    return [_Item(record_id=rid, text=rid) for rid in ids]


def test_rerank_cannot_lift_a_candidate_out_of_its_band():
    """The q201 failure in one assertion: fused says r0 is best and the embedder says it is worst.

    A rerank may reorder near-ties but it may not move a candidate out of its band, so r0 keeps the
    slot range the fusion gave it — which for a top-10 recall is the difference between a hit and a
    miss.
    """
    ids = [f"r{i}" for i in range(10)]
    fused = _fuse_scores([list(ids)])  # r0 first, then descending
    embedder = _AdversarialEmbedder(order=list(reversed(ids)))

    reranked = _rerank("query", _items(ids), embedder, 10, fused=fused)
    position = [it.record_id for it in reranked].index("r0")
    band = max(RERANK_MIN_BAND, 10 // RERANK_BAND_DIVISOR)
    assert position < band, (
        f"the fused winner moved to slot {position}, outside its band of {band}: a bi-encoder "
        "rerank overturned the fusion")


def test_rerank_still_reorders_when_the_fusion_is_close():
    """It is a rerank, not a no-op: among candidates the fusion rates alike, cosine decides."""
    ids = ["a", "b"]
    fused = {"a": 1 / 61, "b": 1 / 62}  # adjacent ranks
    embedder = _AdversarialEmbedder(order=["b", "a"])

    reranked = _rerank("query", _items(ids), embedder, 2, fused=fused)
    assert [it.record_id for it in reranked] == ["b", "a"]


def test_bands_are_wide_enough_to_allow_reordering():
    """A band of one would make the rerank a no-op; the point is to fix near-ties, not to freeze."""
    assert RERANK_BAND_DIVISOR >= 2
    assert RERANK_MIN_BAND >= 2


def test_vector_only_rerank_falls_back_to_pure_cosine():
    """With no fusion to protect, cosine *is* the ranking and must not be diluted."""
    ids = ["a", "b", "c"]
    embedder = _AdversarialEmbedder(order=["c", "a", "b"])
    reranked = _rerank("query", _items(ids), embedder, 3, fused=None)
    assert reranked[0].record_id == "c"


def test_uniform_cosines_do_not_invent_an_order():
    ids = ["a", "b", "c"]
    fused = _fuse_scores([["a", "b", "c"]])
    reranked = _rerank("query", _items(ids), _UniformEmbedder(), 3, fused=fused)
    assert [it.record_id for it in reranked] == ["a", "b", "c"]


def _substrate_with(tmp_path, texts: dict[str, str]) -> tuple[Substrate, ScopeContext]:
    engine = ScopeEngine(known_scopes={"benchmark"})
    ctx = ScopeContext(granted_scopes=frozenset({"benchmark"}), user_authored=True)
    sub = Substrate(tmp_path / "rerank.db", engine)
    for record_id, text in texts.items():
        sub.put(Record(record_id=record_id, record_type="Source", scope="benchmark",
                       payload={"text": text}, epistemic_status="stated",
                       attribution="document"), ctx)
    sub.rebuild_fts("benchmark", ctx)
    return sub, ctx


def test_auto_rerank_is_off_so_lexical_order_survives_hybrid(tmp_path):
    """Auto used to enable the bi-encoder rerank for hybrid/vector. It is off now: the cross-encoder
    is the reranker worth running, and the bi-encoder one removed hits."""
    texts = {
        "hit": "the muse of the harbour keeps the tide tables",
        "noise": "unrelated chatter about lunch",
    }
    sub, ctx = _substrate_with(tmp_path, texts)
    embedder = _AdversarialEmbedder(order=["noise", "hit"])  # cosine prefers the wrong record
    try:
        for record_id, text in texts.items():
            sub.vector_put(record_id, "benchmark", embedder.embed_document(text), ctx,
                           model_id=embedder.model_id)
        items, receipt = recall(sub, "tide tables harbour", ctx, limit=2, mode="hybrid",
                                embedder=embedder)
        assert items[0].record_id == "hit", "an automatic rerank reordered a lexical hit away"
        assert "lexical" in receipt.layers
        assert "cross_encoder" not in receipt.layers
    finally:
        sub.close()