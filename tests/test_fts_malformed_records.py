import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate, SubstrateError


class Embedder:
    model_id = 'test/model'

    def embed_document(self, text):
        return [1.0] + [0.0] * 1023


def test_malformed_payload_cannot_be_rebuilt_or_certified_complete(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sid = Ingestor(sub, Embedder()).ingest_document('a.txt', b'tidal beacon', ctx, 'coast', 'user')
        assert isinstance(sid, str)
        assert sub.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
        old = [tuple(row) for row in sub._conn.execute('SELECT record_id,scope,text FROM fts').fetchall()]
        sub._conn.execute('UPDATE records SET payload=? WHERE record_id=?', ('[]', sid))
        with pytest.raises(SubstrateError):
            sub.rebuild_fts('coast', ctx)
        assert [tuple(row) for row in sub._conn.execute('SELECT record_id,scope,text FROM fts').fetchall()] == old
        assert not sub.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
    finally:
        sub.close()
