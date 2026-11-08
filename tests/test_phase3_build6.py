"""Phase 3 residual fixes (R-P3-10, R-P3-12, N-P3-1) and vector/hybrid retrieval. Tests first."""

import hashlib
import re

import pytest

from aletheia.ingest import Ingestor
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import ScopeViolation, Substrate

KNOWN = {"acs-project", "memory-providers"}
MARKERS = {"acs-project": {"ACS", "Listening Commonwealth"}}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
MEM = ScopeContext(granted_scopes=frozenset({"memory-providers"}), actor="arien")
SYNONYMS = {"beacon": "lighthouse"}


class FakeEmbedder:
    """Deterministic, test-only. Synonyms share a direction, so paraphrase is testable."""

    model_id = "fake-embedder"
    dim = 1024

    def _vec(self, text):
        v = [0.0] * self.dim
        for tok in re.findall(r"\w+", text.lower()):
            tok = SYNONYMS.get(tok, tok)
            idx = int(hashlib.sha256(tok.encode()).hexdigest(), 16) % self.dim
            v[idx] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed_document(self, text):
        return self._vec(text)

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "aletheia.db", scope_engine=ScopeEngine(known_scopes=KNOWN, markers=MARKERS))
    yield s
    s.close()


@pytest.fixture
def emb():
    return FakeEmbedder()


# ---- R-P3-10: deleting an imported Source releases its job row -------------

def test_reimport_after_delete_ingests_again(sub):
    ing = Ingestor(sub)
    doc = b"First paragraph about harbours.\n\nSecond paragraph about cranes."
    sid = ing.ingest_document("notes.txt", doc, USER, "memory-providers", "author")
    assert sub.delete(sid, USER, reason="test")
    assert ing.ingest_document("notes.txt", doc, USER, "memory-providers", "author") == sid
    obs = [r for r in sub.all_records(USER) if r.record_type == "Observation"]
    assert len(obs) == 2


# ---- R-P3-12: machine-derived text is labelled on read ----------------------

def test_derived_media_is_labelled_on_read(sub):
    ing = Ingestor(sub)
    ing.ingest_media("p.png", b"\x89PNG-lh", "image", USER, "memory-providers",
                     describer=lambda _d: "a lighthouse at dusk")
    items, _ = recall(sub, "lighthouse", MEM, mode="lexical")
    assert items and items[0].derived is True
    assert 'derived="machine"' in items[0].render()


def test_document_text_is_not_labelled_derived(sub):
    Ingestor(sub).ingest_document("n.txt", b"a lighthouse note", USER, "memory-providers", "a")
    items, _ = recall(sub, "lighthouse", MEM, mode="lexical")
    assert items and items[0].derived is False
    assert "derived=" not in items[0].render()


# ---- N-P3-1: scope_hint narrows the candidates, not only the receipt -------

def test_scope_hint_narrows_candidates(sub):
    ing = Ingestor(sub)
    ing.ingest_turn("zebra stripes are in the acs archive", "user", USER, "acs-project", "c1")
    ing.ingest_turn("zebra stripes are in memory", "user", USER, "memory-providers", "c2")
    items, _ = recall(sub, "zebra", USER, scope_hint="memory-providers", mode="lexical")
    assert items and all(i.scope == "memory-providers" for i in items)


# ---- vector path: gated, scope-first, deletable -----------------------------

def test_vector_candidates_are_scope_gated(sub, emb):
    rid = Ingestor(sub, embedder=emb).ingest_turn(
        "the beacon signal is active", "user", USER, "acs-project", "c1")
    assert sub.vector_candidates(emb.embed_query("lighthouse"), MEM, 5, model_id=emb.model_id) == []
    assert rid in sub.vector_candidates(emb.embed_query("lighthouse"), USER, 5, model_id=emb.model_id)


def test_delete_removes_vector_row(sub, emb):
    rid = Ingestor(sub, embedder=emb).ingest_turn(
        "the beacon signal is active", "user", USER, "memory-providers", "c1")
    assert rid in sub.vector_candidates(emb.embed_query("lighthouse"), USER, 5, model_id=emb.model_id)
    assert sub.delete(rid, USER, reason="test")
    assert rid not in sub.vector_candidates(emb.embed_query("lighthouse"), USER, 5, model_id=emb.model_id)


def test_vector_put_requires_write_authority(sub, emb):
    with pytest.raises(ScopeViolation):
        sub.vector_put("x1", "acs-project", emb.embed_document("hi"), ctx=MEM, model_id=emb.model_id)
    with pytest.raises(ScopeViolation):
        sub.vector_put("x1", "acs-project", emb.embed_document("hi"), ctx=None, model_id=emb.model_id)


# ---- hybrid recall: a paraphrase that lexical search misses ----------------

def test_lexical_misses_paraphrase_hybrid_and_vector_find_it(sub, emb):
    Ingestor(sub, embedder=emb).ingest_turn(
        "the beacon signal is active", "user", USER, "memory-providers", "c1")
    lexical, _ = recall(sub, "lighthouse", MEM, mode="lexical")
    hybrid, _ = recall(sub, "lighthouse", MEM, mode="hybrid", embedder=emb)
    vector, _ = recall(sub, "lighthouse", MEM, mode="vector", embedder=emb)
    assert lexical == []
    assert hybrid and vector


def test_vector_mode_without_embedder_is_refused(sub):
    with pytest.raises(ValueError):
        recall(sub, "lighthouse", MEM, mode="vector")
