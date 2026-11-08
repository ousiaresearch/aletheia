import pytest
import sqlite_vec

from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 1e39, -1e39], ids=['nan', 'positive-infinity', 'negative-infinity', 'float32-overflow', 'negative-float32-overflow'])
def test_nonfinite_document_vectors_are_refused_without_replacing_existing_vector(tmp_path, bad):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sub.put(Record(record_id='r', record_type='Source', scope='coast', payload={'text': 'fixture'}, attribution='document'), ctx)
        sub.vector_put('r', 'coast', [1.0] + [0.0] * 1023, ctx, model_id='m')
        table = sub._vec_table('coast', 'm')
        before = sub._conn.execute(f'SELECT embedding FROM {table} WHERE record_id=?', ('r',)).fetchone()[0]
        with pytest.raises(ValueError, match='finite'):
            sub.vector_put('r', 'coast', [bad] + [0.0] * 1023, ctx, model_id='m')
        after = sub._conn.execute(f'SELECT embedding FROM {table} WHERE record_id=?', ('r',)).fetchone()[0]
        assert after == before
    finally:
        sub.close()


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 1e39, -1e39], ids=['nan', 'positive-infinity', 'negative-infinity', 'float32-overflow', 'negative-float32-overflow'])
def test_nonfinite_query_vectors_are_refused(tmp_path, bad):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sub.put(Record(record_id='r', record_type='Source', scope='coast', payload={'text': 'fixture'}, attribution='document'), ctx)
        sub.vector_put('r', 'coast', [1.0] + [0.0] * 1023, ctx, model_id='m')
        assert sub.vector_candidates([1.0] + [0.0] * 1023, ctx, 1, model_id='m') == ['r']
        with pytest.raises(ValueError, match='finite'):
            sub.vector_candidates([bad] + [0.0] * 1023, ctx, 1, model_id='m')
    finally:
        sub.close()


def test_hybrid_recall_rejects_legacy_nonfinite_readable_vector(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    class Embedder:
        model_id = 'm'
        def embed_query(self, text):
            return [1.0] + [0.0] * 1023
    try:
        sub.put(Record(record_id='r', record_type='Source', scope='coast', payload={'text': 'fixture'}, attribution='document'), ctx)
        sub.vector_put('r', 'coast', [1.0] + [0.0] * 1023, ctx, model_id='m')
        items, _ = recall(sub, 'fixture', ctx, mode='hybrid', embedder=Embedder())
        assert any(item.record_id == 'r' for item in items)
        table = sub._vec_table('coast', 'm')
        sub._conn.execute(f'DELETE FROM {table} WHERE record_id=?', ('r',))
        sub._conn.execute(f'INSERT INTO {table}(record_id, embedding) VALUES (?, ?)', ('r', sqlite_vec.serialize_float32([float('nan')] + [0.0] * 1023)))
        with pytest.raises(ValueError, match='vector index'):
            recall(sub, 'fixture', ctx, mode='hybrid', embedder=Embedder())
    finally:
        sub.close()
