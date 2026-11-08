import pytest
import sqlite_vec

from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate


def test_direct_vector_search_refuses_legacy_nonfinite_visible_row(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    query = [1.0] + [0.0] * 1023
    try:
        sub.put(Record(record_id='r', record_type='Source', scope='coast', payload={'text': 'fixture'}, attribution='document'), ctx)
        sub.vector_put('r', 'coast', query, ctx, model_id='m')
        assert sub.vector_candidates(query, ctx, 1, model_id='m') == ['r']
        table = sub._vec_table('coast', 'm')
        sub._conn.execute(f'DELETE FROM {table} WHERE record_id=?', ('r',))
        sub._conn.execute(f'INSERT INTO {table}(record_id, embedding) VALUES (?, ?)', ('r', sqlite_vec.serialize_float32([float('nan')] + [0.0] * 1023)))
        with pytest.raises(ValueError, match='vector index'):
            sub.vector_candidates(query, ctx, 1, model_id='m')
    finally:
        sub.close()
