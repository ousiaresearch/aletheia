import pytest

from aletheia.ingest import Ingestor
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


class Embedder:
    model_id = 'test/model'

    def embed_document(self, text):
        return [1.0] + [0.0] * 1023

    embed_query = embed_document


def test_hybrid_refuses_orphan_fts_row_in_requested_scope(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sid = Ingestor(sub, Embedder()).ingest_document('a.txt', b'beacon tide record', ctx, 'coast', 'user')
        assert isinstance(sid, str)
        items, _ = recall(sub, 'beacon', ctx, mode='hybrid', embedder=Embedder())
        assert items
        sub._conn.execute('INSERT INTO fts(record_id, scope, text) VALUES (?,?,?)',
                          ('orphan', 'coast', 'beacon tide deleted secret'))
        with pytest.raises(ValueError, match='complete'):
            recall(sub, 'beacon', ctx, mode='hybrid', embedder=Embedder())
        sub.rebuild_fts('coast', ctx)
        assert sub._conn.execute('SELECT 1 FROM fts WHERE record_id=?', ('orphan',)).fetchone() is None
        items, _ = recall(sub, 'beacon', ctx, mode='hybrid', embedder=Embedder())
        assert items
    finally:
        sub.close()
