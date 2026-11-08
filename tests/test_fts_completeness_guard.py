from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


class Embedder:
    model_id = 'test/model'

    def embed_document(self, text):
        return [1.0] + [0.0] * 1023


def test_vector_completeness_refuses_missing_fts_member(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sid = Ingestor(sub, Embedder()).ingest_document(
            'test.txt', b'A lighthouse records the tides.', ctx, 'coast', 'operator')
        assert isinstance(sid, str)
        assert sub.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
        sub._conn.execute('DELETE FROM fts WHERE record_id = ?', (sid,))
        assert not sub.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
    finally:
        sub.close()


def test_vector_completeness_refuses_stale_fts_text(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sid = Ingestor(sub, Embedder()).ingest_document(
            'test.txt', b'A lighthouse records the tides.', ctx, 'coast', 'operator')
        assert isinstance(sid, str)
        assert sub.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
        sub._conn.execute('UPDATE fts SET text = ? WHERE record_id = ?', ('unrelated stale text', sid))
        assert not sub.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
    finally:
        sub.close()
