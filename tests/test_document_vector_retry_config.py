import struct

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


class DistinctEmbedder:
    model_id = 'same-model'

    def embed_document(self, text):
        v = [0.0] * 1024
        v[2 if '\n\n' in text else (0 if text == 'alpha' else 1)] = 1.0
        return v


def test_retry_recomputes_source_vector_for_changed_mode(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    embedder = DistinctEmbedder()
    data = b'alpha\n\nbeta'
    try:
        sid = Ingestor(sub, embedder, doc_centroid=True).ingest_document('a.md', data, ctx, 'coast', 'operator')
        table = sub._vec_table('coast', embedder.model_id)
        def stored():
            row = sub._conn.execute(f'SELECT embedding FROM {table} WHERE record_id=?', (sid,)).fetchone()
            return struct.unpack('1024f', row[0])[:3]
        assert stored()[0] > 0 and stored()[2] == 0
        assert Ingestor(sub, embedder, doc_vectors=True, doc_centroid=False).ingest_document('a.md', data, ctx, 'coast', 'operator') == sid
        assert stored() == (0.0, 0.0, 1.0)
    finally:
        sub.close()


def test_retry_without_source_vectors_refuses_existing_source_vector(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    embedder = DistinctEmbedder()
    data = b'alpha\n\nbeta'
    try:
        sid = Ingestor(sub, embedder, doc_centroid=True).ingest_document('a.md', data, ctx, 'coast', 'operator')
        assert sub.vector_has_record(sid, 'coast', ctx, model_id=embedder.model_id)
        with pytest.raises(ValueError, match='source vector'):
            Ingestor(sub, embedder, doc_vectors=False, doc_centroid=False).ingest_document('a.md', data, ctx, 'coast', 'operator')
        assert sub.vector_has_record(sid, 'coast', ctx, model_id=embedder.model_id)
    finally:
        sub.close()
