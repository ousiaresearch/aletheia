"""Phase 3 review 717b85a, residuals fixed in pass 7: F3 (model isolation), F9 (stale vector on overwrite).

Tests first. Vectors from one embedding model must never be scored against an index built
with another, and a superseded or overwritten record must not keep a vector for its old text.
"""

import hashlib
import re

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate
from aletheia.substrate import Record

KNOWN = {"acs-project", "memory-providers"}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
SYNONYMS = {"beacon": "lighthouse"}


class FakeEmbedder:
    model_id = "model-a"
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


class OtherModel(FakeEmbedder):
    model_id = "model-b"


@pytest.fixture
def sub(tmp_path):
    s = Substrate(str(tmp_path / "aletheia.db"), ScopeEngine(KNOWN, {}))
    yield s
    s.close()


def test_vectors_are_isolated_by_model(sub):
    a = FakeEmbedder()
    rid = Ingestor(sub, embedder=a).ingest_turn(
        "the beacon signal is active", "user", USER, "memory-providers", "c1"
    )
    q = a.embed_query("lighthouse")
    assert rid in sub.vector_candidates(q, USER, 5, model_id="model-a")
    # the same vector asked under another model finds nothing: no mixed-space scoring
    assert sub.vector_candidates(q, USER, 5, model_id="model-b") == []


def test_overwrite_clears_stale_vector(sub):
    a = FakeEmbedder()
    rid = Ingestor(sub, embedder=a).ingest_turn(
        "the beacon signal is active", "user", USER, "memory-providers", "c1"
    )
    sub.put(Record(
        record_id=rid, record_type="Source", scope="memory-providers",
        payload={"text": "tide tables for the harbour"}, attribution="user", source_ids=[],
        provenance={"conversation_id": "c1", "speaker": "user"},
    ), USER, overwrite=True)
    # the old text's vector must no longer rank the record
    assert rid not in sub.vector_candidates(a.embed_document("the beacon signal is active"), USER, 5, model_id="model-a")


def test_vector_put_needs_explicit_model(sub):
    a = FakeEmbedder()
    rid = Ingestor(sub, embedder=a).ingest_turn(
        "the beacon signal is active", "user", USER, "memory-providers", "c1"
    )
    with pytest.raises(TypeError):
        sub.vector_candidates(a.embed_query("lighthouse"), USER, 5)  # no model_id given
