"""Note-level (document head) vectors. Tests first."""

import re
import hashlib
import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate

KNOWN = {"acs-project", "memory-providers"}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
MEM = ScopeContext(granted_scopes=frozenset({"memory-providers"}), actor="arien")


class FakeEmbedder:
    model_id = "fake"
    dim = 1024

    def _vec(self, text):
        v = [0.0] * self.dim
        for tok in re.findall(r"\w+", text.lower()):
            idx = int(hashlib.sha256(tok.encode()).hexdigest(), 16) % self.dim
            v[idx] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed_document(self, text):
        return self._vec(text)

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "aletheia.db", scope_engine=ScopeEngine(known_scopes=KNOWN, markers={}))
    yield s
    s.close()


def test_document_head_vector_is_written_when_enabled(sub):
    emb = FakeEmbedder()
    doc = b"Harbour Notes\n\nThe quay watch rota covers night shifts.\n\nUnrelated closing paragraph."
    sid = Ingestor(sub, embedder=emb, doc_vectors=True).ingest_document(
        "harbour.md", doc, USER, "memory-providers", "a")
    assert sid in sub.vector_candidates(emb.embed_query("harbour notes quay"), MEM, 10, model_id=emb.model_id)


def test_document_centroid_vector_is_normalised_mean_of_paragraphs(sub):
    emb = FakeEmbedder()
    doc = b"The quay watch rota covers night shifts.\n\nHarbour lamps are trimmed at dusk."
    sid = Ingestor(sub, embedder=emb, doc_centroid=True).ingest_document(
        "harbour.md", doc, USER, "memory-providers", "a")
    assert sid in sub.vector_candidates(emb.embed_query("harbour lamps dusk"), MEM, 10, model_id=emb.model_id)


def test_document_centroid_on_by_default(sub):
    emb = FakeEmbedder()
    doc = b"The quay watch rota covers night shifts.\n\nHarbour lamps are trimmed at dusk."
    sid = Ingestor(sub, embedder=emb).ingest_document("harbour.md", doc, USER, "memory-providers", "a")
    assert sid in sub.vector_candidates(emb.embed_query("harbour lamps dusk"), MEM, 10, model_id=emb.model_id)
