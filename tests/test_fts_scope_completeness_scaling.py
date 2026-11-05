import pytest

from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate, VEC_DIM


def record(rid, scope='a', text='visible needle'):
    return Record(record_id=rid, record_type='Source', scope=scope,
                  payload={'text': text}, attribution='user')


@pytest.fixture
def env(tmp_path):
    sub = Substrate(tmp_path / 'store.db', ScopeEngine(known_scopes={'a', 'b'}))
    both = ScopeContext(granted_scopes={'a', 'b'}, user_authored=True)
    a = ScopeContext(granted_scopes={'a'}, user_authored=True)
    sub.put(record('visible'), both)
    sub.vector_put('visible', 'a', [0.1] * VEC_DIM, both, model_id='m')
    yield sub, both, a
    sub.close()


def complete(sub, ctx, kind):
    return (sub.fts_index_complete('a', ctx) if kind == 'fts' else
            sub.vector_index_complete('a', ctx, model_id='m'))


@pytest.mark.parametrize('kind', ['fts', 'vector'])
def test_foreign_orphan_does_not_change_visible_completeness(env, kind):
    sub, both, a = env
    assert complete(sub, a, kind)
    assert sub.fts_candidates('needle', a, 10, scopes=['a']) == ['visible']
    sub._conn.execute("INSERT INTO fts(record_id,scope,text) VALUES ('gone','b','foreign')")
    assert complete(sub, a, kind)
    assert sub.fts_candidates('needle', a, 10, scopes=['a']) == ['visible']
    assert complete(sub, both, kind)
    assert not (sub.fts_index_complete('b', both) if kind == 'fts' else
                sub.vector_index_complete('b', both, model_id='m'))


@pytest.mark.parametrize('kind', ['fts', 'vector'])
@pytest.mark.parametrize('damage', ['missing', 'stale', 'duplicate', 'orphan', 'foreign_duplicate'])
def test_visible_corruption_is_rejected(env, kind, damage):
    sub, both, a = env
    assert complete(sub, a, kind)
    if damage == 'missing':
        sub._conn.execute("DELETE FROM fts WHERE record_id='visible'")
    elif damage == 'stale':
        sub._conn.execute("UPDATE fts SET text='stale' WHERE record_id='visible'")
    elif damage == 'duplicate':
        sub._conn.execute("INSERT INTO fts SELECT record_id,scope,text FROM fts WHERE record_id='visible'")
    elif damage == 'orphan':
        sub._conn.execute("INSERT INTO fts(record_id,scope,text) VALUES ('gone','a','stray')")
    else:
        sub._conn.execute("INSERT INTO fts SELECT record_id,'b',text FROM fts WHERE record_id='visible'")
    assert not complete(sub, a, kind)


@pytest.mark.parametrize('kind', ['fts', 'vector'])
def test_hidden_payload_corruption_matches_absence(env, kind):
    sub, both, a = env
    assert complete(sub, a, kind)
    sub.put(record('hidden', text='secretmarker'), both)
    sub.vector_put('hidden', 'a', [0.1] * VEC_DIM, both, model_id='m')
    assert sub.get('hidden', both) is not None
    sub.scope_engine.register_scope('b', {'secretmarker'})
    assert sub.get('hidden', a) is None
    sub._conn.execute("UPDATE fts SET text='stale' WHERE record_id='hidden'")
    assert complete(sub, a, kind)
    sub.scope_engine.markers.clear()
    assert not complete(sub, both, kind)


def test_fts_completeness_sql_work_scales_linearly(tmp_path):
    sub = Substrate(tmp_path / 'scale.db', ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes={'a'}, user_authored=True)
    def add(start, stop):
        with sub.ingestion_transaction():
            for i in range(start, stop):
                sub.put(record(str(i)), ctx)
    def cost():
        steps = [0]
        def progress():
            steps[0] += 1
            return 0
        sub._conn.set_progress_handler(progress, 100)
        try:
            assert sub.fts_index_complete('a', ctx)
        finally:
            sub._conn.set_progress_handler(None, 0)
        return steps[0]
    try:
        add(0, 300)
        small = cost()
        add(300, 600)
        large = cost()
        assert large < small * 3, (small, large)
    finally:
        sub.close()
