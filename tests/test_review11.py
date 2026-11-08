"""Regression tests from the independent audit of CLI commit 5323b29."""

import pytest

from aletheia.ingest import Ingestor
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


class _VectorEmbedder:
    model_id = "review/test-v1"

    def __init__(self, document_axis=0):
        self.document_axis = document_axis

    @staticmethod
    def _axis(index):
        vector = [0.0] * 1024
        vector[index] = 1.0
        return vector

    def embed_document(self, text):
        return self._axis(self.document_axis)

    def embed_query(self, text):
        return self._axis(0)


class _WrongDimensionEmbedder(_VectorEmbedder):
    def embed_document(self, text):
        return [1.0, 0.0]


class _FailingEmbedder(_VectorEmbedder):
    def embed_document(self, text):
        raise RuntimeError("synthetic model failure")


def _context():
    return ScopeContext(granted_scopes=frozenset({"coast"}))


def _substrate(path, engine=None):
    return Substrate(path, engine or ScopeEngine(known_scopes={"coast"}))


def test_document_wrong_dimension_does_not_leave_records_or_running_job(tmp_path):
    db = tmp_path / "wrong-dimension.db"
    sub = _substrate(db)
    try:
        with pytest.raises(ValueError, match="dimensions"):
            Ingestor(sub, embedder=_WrongDimensionEmbedder()).ingest_document(
                "note.md", b"A non-empty document paragraph.", _context(), "coast", "operator"
            )
        assert sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert sub._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        sub.close()


def test_turn_embedding_failure_does_not_leave_a_source(tmp_path):
    sub = _substrate(tmp_path / "turn.db")
    try:
        with pytest.raises(RuntimeError, match="synthetic model failure"):
            Ingestor(sub, embedder=_FailingEmbedder()).ingest_turn(
                "A conversation turn.", "user", ScopeContext(granted_scopes=frozenset({"coast"}), user_authored=True), "coast", "conversation-1"
            )
        assert sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    finally:
        sub.close()


def test_media_embedding_failure_leaves_no_source_and_failed_job(tmp_path):
    sub = _substrate(tmp_path / "media.db")
    try:
        with pytest.raises(RuntimeError, match="synthetic model failure"):
            Ingestor(sub, embedder=_FailingEmbedder()).ingest_media(
                "photo.png", b"image-bytes", "image", _context(), "coast",
                describer=lambda data: "A lighthouse beside the shore.",
            )
        assert sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        jobs = sub._conn.execute("SELECT state, reason FROM jobs WHERE stage='derive'").fetchall()
        assert jobs == [("failed", "embedding_failed")]
    finally:
        sub.close()


def test_hidden_text_does_not_make_vector_completeness_reveal_its_presence(tmp_path):
    db = tmp_path / "hidden-text.db"
    writer = Substrate(db, ScopeEngine(known_scopes={"coast", "inland"}))
    try:
        result = Ingestor(writer).ingest_document(
            "evidence.md", b"Inland-only marker hidden from this reader.",
            _context(), "coast", "operator",
        )
        assert isinstance(result, str)
    finally:
        writer.close()

    reader_engine = ScopeEngine(
        known_scopes={"coast", "inland"},
        markers={"inland": {"Inland-only marker"}},
    )
    reader = Substrate(db, reader_engine)
    try:
        assert reader.vector_index_complete("coast", _context(), model_id="review/test-v1") is True
        items, _ = recall(
            reader, "marker", _context(), scope_hint="coast", mode="hybrid",
            embedder=_VectorEmbedder(),
        )
        assert items == []
    finally:
        reader.close()


def test_hybrid_vector_recall_finds_a_match_without_lexical_overlap(tmp_path):
    sub = _substrate(tmp_path / "vector-hit.db")
    try:
        result = Ingestor(sub, embedder=_VectorEmbedder()).ingest_document(
            "note.md", b"The beacon keeper records the evening tides.",
            _context(), "coast", "operator",
        )
        assert isinstance(result, str)
        items, _ = recall(
            sub, "quasar", _context(), scope_hint="coast", mode="hybrid",
            embedder=_VectorEmbedder(),
        )
        assert items
        assert "beacon" in items[0].text.lower()
    finally:
        sub.close()


def test_hybrid_recall_rejects_one_missing_vector_even_when_table_exists(tmp_path):
    sub = _substrate(tmp_path / "one-missing-vector.db")
    embedder = _VectorEmbedder()
    try:
        result = Ingestor(sub, embedder=embedder).ingest_document(
            "note.md", b"The beacon keeper records the evening tides.",
            _context(), "coast", "operator",
        )
        assert isinstance(result, str)
        table = sub._vec_table("coast", embedder.model_id)
        sub._conn.execute(f"DELETE FROM {table} WHERE record_id=?", (f"{result}-p0000",))
        with pytest.raises(ValueError, match="complete vector index"):
            recall(
                sub, "quasar", _context(), scope_hint="coast", mode="hybrid",
                embedder=embedder,
            )
    finally:
        sub.close()
