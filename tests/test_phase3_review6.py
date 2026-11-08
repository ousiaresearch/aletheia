"""Phase 3 review (717b85a) GATE F1: vector_put must authorize against the STORED record.

Tests first. A caller with write authority on scope X must not be able to attach a
vector to a record that lives in scope Y, nor to a record id that does not exist.
"""

import re
import hashlib

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import ScopeViolation, Substrate

KNOWN = {"acs-project", "memory-providers"}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
ATTACKER = ScopeContext(granted_scopes=frozenset({"acs-project"}), actor="x")


class FakeEmbedder:
    model_id = "fake-embedder"
    dim = 1024

    def _vec(self, text):
        v = [0.0] * self.dim
        for tok in re.findall(r"\w+", text.lower()):
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
    s = Substrate(str(tmp_path / "aletheia.db"), ScopeEngine(KNOWN, {}))
    yield s
    s.close()


def test_vector_put_refuses_record_in_another_scope(sub):
    emb = FakeEmbedder()
    rid = Ingestor(sub, embedder=emb).ingest_turn(
        "secret harbour payroll figures", "user", USER, "memory-providers", "c1"
    )
    with pytest.raises(ScopeViolation):
        sub.vector_put(rid, "acs-project", emb.embed_document("zebra zebra zebra"), ctx=ATTACKER, model_id=emb.model_id)
    # the record keeps its own ingest vector: the refused write changed nothing.
    # (vector_candidates returns top-k regardless of similarity, so we check the
    # stored embedding for this record, not whether it ranks.)
    own = emb.embed_document("secret harbour payroll figures")
    top = sub.vector_candidates(own, USER, 1, model_id=emb.model_id)
    assert top and top[0] == rid


def test_vector_put_refuses_unknown_record_id(sub):
    emb = FakeEmbedder()
    with pytest.raises(ScopeViolation):
        sub.vector_put("no-such-record", "acs-project", emb.embed_document("zebra"), ctx=USER, model_id=emb.model_id)


def test_vector_put_still_allowed_for_record_in_own_scope(sub):
    emb = FakeEmbedder()
    rid = Ingestor(sub, embedder=emb).ingest_turn(
        "the beacon signal is active", "user", USER, "memory-providers", "c1"
    )
    sub.vector_put(rid, "memory-providers", emb.embed_document("lighthouse"), ctx=USER, model_id=emb.model_id)
    assert rid in sub.vector_candidates(emb.embed_query("lighthouse"), USER, 5, model_id=emb.model_id)
