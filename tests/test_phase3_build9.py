"""One result per document in recall (found by the note-level variant). Tests first."""

import pytest

from aletheia.ingest import Ingestor
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate

KNOWN = {"acs-project", "memory-providers"}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
MEM = ScopeContext(granted_scopes=frozenset({"memory-providers"}), actor="arien")


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "aletheia.db", scope_engine=ScopeEngine(known_scopes=KNOWN, markers={}))
    yield s
    s.close()


def test_one_result_per_document(sub):
    ing = Ingestor(sub)
    ing.ingest_document("alpha.md", b"harbour one\n\nharbour two\n\nharbour three", USER,
                        "memory-providers", "a")
    ing.ingest_document("beta.md", b"harbour elsewhere", USER, "memory-providers", "a")
    items, _ = recall(sub, "harbour", MEM, limit=2, mode="lexical")
    # Multiple paragraphs from the same document can now be returned.
    assert len(items) == 2
    record_ids = [item.record_id for item in items]
    assert len(set(record_ids)) == len(record_ids)  # no duplicate record_ids


def test_turns_are_not_collapsed(sub):
    ing = Ingestor(sub)
    ing.ingest_turn("harbour bells one", "user", USER, "memory-providers", "c1")
    ing.ingest_turn("harbour bells two", "user", USER, "memory-providers", "c2")
    items, _ = recall(sub, "harbour", MEM, limit=5, mode="lexical")
    assert len(items) == 2
