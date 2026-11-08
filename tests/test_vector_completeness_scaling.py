from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate, VEC_DIM


def test_vector_completeness_scans_projection_once(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    try:
        with sub.ingestion_transaction():
            for i in range(30):
                rid = str(i)
                sub.put(Record(record_id=rid, record_type='Source', scope='a',
                               payload={'text': 'visible'}, attribution='user'), ctx)
                sub.vector_put(rid, 'a', [0.1] * VEC_DIM, ctx, model_id='m')
        statements = []
        sub._conn.set_trace_callback(statements.append)
        try:
            assert sub.vector_index_complete('a', ctx, model_id='m')
        finally:
            sub._conn.set_trace_callback(None)
        scans = [q for q in statements if q.lower().startswith('select ')
                 and 'embedding from vec_' in q.lower()]
        assert len(scans) == 1, len(scans)
    finally:
        sub.close()
