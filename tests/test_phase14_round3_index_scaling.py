"""Round-3 R3-05/R3-06/R3-09 index repairs, written red first.

Scaling assertions are relative (SQLite VM steps via a progress handler, never wall-clock).
"""
import pytest

from weft.procedural import ProposedRule, propose_rule
from weft.retrieval import recall
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate, VEC_DIM


class Emb:
    model_id = 'm'

    def embed_query(self, text):
        return [0.1] * VEC_DIM

    embed_document = embed_query


def rec(rid, scope='a', text='needle word', typ='Source', **kw):
    return Record(record_id=rid, record_type=typ, scope=scope, payload={'text': text},
                  attribution=kw.pop('attribution', 'user'), **kw)


@pytest.fixture
def env(tmp_path):
    sub = Substrate(tmp_path / 's.db', ScopeEngine(known_scopes={'a', 'b'}))
    both = ScopeContext(granted_scopes={'a', 'b'}, user_authored=True)
    a = ScopeContext(granted_scopes={'a'}, user_authored=True)
    yield sub, both, a
    sub.close()


def steps(sub, fn):
    n = [0]

    def tick():
        n[0] += 1
        return 0
    sub._conn.set_progress_handler(tick, 100)
    try:
        fn()
    finally:
        sub._conn.set_progress_handler(None, 0)
    return n[0]


def rewrite(sub, rid, text):
    """Change a stored record's text and re-index it (what correct()/import do)."""
    import json
    sub._conn.execute('UPDATE records SET payload=? WHERE record_id=?',
                      (json.dumps({'text': text}), rid))
    sub._fts_index(sub._lookup_record(rid))


def fill(sub, ctx, start, stop):
    with sub.ingestion_transaction():
        for i in range(start, stop):
            sub.put(rec(f'r{i}', text=f'needle token{i} filler words here'), ctx)


# ---- (A) scaling -----------------------------------------------------------------------

def test_completeness_scales_500_to_2000(env):
    sub, both, a = env
    fill(sub, a, 0, 500)
    small = steps(sub, lambda: sub.fts_index_complete('a', a))
    fill(sub, a, 500, 2000)
    big = steps(sub, lambda: sub.fts_index_complete('a', a))
    # 4x records: linear is ~4x, the old per-record scan was ~16x.
    assert big < small * 7, (small, big)


def test_per_write_index_cost_is_not_linear_in_store_size(env):
    sub, both, a = env
    fill(sub, a, 0, 500)
    small = steps(sub, lambda: fill(sub, a, 500, 600))
    fill(sub, a, 600, 2000)
    big = steps(sub, lambda: fill(sub, a, 2000, 2100))
    def churn(lo):
        for i in range(lo, lo + 100):
            rewrite(sub, f'r{i}', f'needle churned{i}')
    small_o = steps(sub, lambda: churn(0))
    fill(sub, a, 2100, 4000)
    big_o = steps(sub, lambda: churn(1000))
    assert big_o < small_o * 2.5, (small_o, big_o)
    # same number of writes at 4x the store: roughly constant per write, not 4x.
    assert big < small * 2.5, (small, big)


def test_overwrite_and_delete_leave_exactly_one_or_zero_rows(env):
    sub, both, a = env
    fill(sub, a, 0, 50)
    for i in range(3):
        rewrite(sub, 'r7', f'needle revised{i}')
    rows = sub._conn.execute("SELECT text FROM fts WHERE record_id='r7'").fetchall()
    assert len(rows) == 1 and 'revised2' in rows[0][0]
    assert sub.fts_index_complete('a', a)
    assert sub.delete('r7', a)
    assert sub._conn.execute("SELECT count(*) FROM fts WHERE record_id='r7'").fetchone()[0] == 0
    assert sub.fts_index_complete('a', a)


def test_rolled_back_write_leaves_index_consistent(env):
    sub, both, a = env
    fill(sub, a, 0, 20)
    with pytest.raises(RuntimeError):
        with sub.ingestion_transaction():
            rewrite(sub, 'r3', 'needle changed')
            sub.put(rec('new', text='needle new'), a)
            raise RuntimeError
    assert sub.fts_index_complete('a', a)
    rewrite(sub, 'r3', 'needle again')
    assert sub._conn.execute("SELECT count(*) FROM fts WHERE record_id='r3'").fetchone()[0] == 1
    assert sub.fts_index_complete('a', a)


@pytest.mark.parametrize('damage', ['missing', 'stale', 'duplicate', 'orphan'])
def test_integrity_guard_still_detects_damage_at_scale(env, damage):
    sub, both, a = env
    fill(sub, a, 0, 600)
    assert sub.fts_index_complete('a', a)
    c = sub._conn
    if damage == 'missing':
        c.execute("DELETE FROM fts WHERE record_id='r300'")
    elif damage == 'stale':
        c.execute("UPDATE fts SET text='stale' WHERE record_id='r300'")
    elif damage == 'duplicate':
        c.execute("INSERT INTO fts SELECT record_id,scope,text FROM fts WHERE record_id='r300'")
    else:
        c.execute("INSERT INTO fts(record_id,scope,text) VALUES ('ghost','a','x')")
    assert not sub.fts_index_complete('a', a)
    sub.rebuild_fts('a', a)
    assert sub.fts_index_complete('a', a)
    # the repaired index is still writable through the row map
    rewrite(sub, 'r300', 'needle healed')
    assert sub._conn.execute("SELECT count(*) FROM fts WHERE record_id='r300'").fetchone()[0] == 1
    assert sub.fts_index_complete('a', a)


def test_legacy_store_without_row_map_is_backfilled(tmp_path):
    p = tmp_path / 'legacy.db'
    s = Substrate(p, ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes={'a'}, user_authored=True)
    fill(s, ctx, 0, 30)
    s._conn.execute('DROP TABLE IF EXISTS fts_rowmap')
    s._conn.execute("DELETE FROM meta WHERE key='fts_rowmap_ready'")
    s.close()
    s = Substrate(p, ScopeEngine(known_scopes={'a'}))
    try:
        rewrite(s, 'r5', 'needle legacy rewrite')
        assert s._conn.execute("SELECT count(*) FROM fts WHERE record_id='r5'").fetchone()[0] == 1
        assert s.fts_index_complete('a', ctx)
    finally:
        s.close()


# ---- (A) per-query BM25 corpus cost ----------------------------------------------------

def test_repeat_queries_do_not_rebuild_corpus_until_data_changes(env):
    sub, both, a = env
    fill(sub, a, 0, 200)
    first = sub.fts_candidates('"needle"', a, 5, scopes=['a'])
    builds = sub._fts_corpus_builds
    for _ in range(5):
        assert sub.fts_candidates('"needle"', a, 5, scopes=['a']) == first
    assert sub._fts_corpus_builds == builds
    sub.put(rec('fresh', text='needle needle needle needle unique'), a)
    assert 'fresh' in sub.fts_candidates('"unique"', a, 5, scopes=['a'])
    assert sub._fts_corpus_builds == builds + 1


def test_corpus_cache_not_reused_after_rollback_or_delete(env):
    sub, both, a = env
    fill(sub, a, 0, 10)
    sub.fts_candidates('"needle"', a, 5, scopes=['a'])
    with pytest.raises(RuntimeError):
        with sub.ingestion_transaction():
            sub.put(rec('ghostly', text='needle phantom'), a)
            assert 'ghostly' in sub.fts_candidates('"phantom"', a, 5, scopes=['a'])
            raise RuntimeError
    assert sub.fts_candidates('"phantom"', a, 5, scopes=['a']) == []
    sub.put(rec('gone', text='needle vanish'), a)
    assert sub.fts_candidates('"vanish"', a, 5, scopes=['a']) == ['gone']
    sub.delete('gone', a)
    assert sub.fts_candidates('"vanish"', a, 5, scopes=['a']) == []


def test_corpus_cache_respects_ctx_scopes_and_markers(env):
    sub, both, a = env
    sub.put(rec('ra', text='needle alpha'), both)
    sub.put(rec('rb', 'b', text='needle beta'), both)
    assert sub.fts_candidates('"needle"', a, 10, scopes=['a']) == ['ra']
    assert sorted(sub.fts_candidates('"needle"', both, 10, scopes=['a', 'b'])) == ['ra', 'rb']
    assert sub.fts_candidates('"needle"', a, 10, scopes=['a']) == ['ra']
    # a marker change must not reuse a stale visibility decision
    sub.scope_engine.register_scope('b', {'alpha'})
    assert sub.fts_candidates('"needle"', both, 10, scopes=['a', 'b']) == ['rb']


def test_bm25_rank_in_scope_a_ignores_scope_b_with_cache(env):
    sub, both, a = env
    for i in range(3):
        sub.put(rec(f'a{i}', text='needle ' + 'pad ' * i), both)
    before = sub.fts_candidates('"needle"', a, 10, scopes=['a'])
    sub.fts_candidates('"needle"', both, 10, scopes=['a', 'b'])
    for i in range(40):
        sub.put(rec(f'b{i}', 'b', text='needle needle needle'), both)
    assert sub.fts_candidates('"needle"', a, 10, scopes=['a']) == before


# ---- (B) embeddable types --------------------------------------------------------------

def test_rule_and_decision_do_not_break_hybrid_recall(env):
    sub, both, a = env
    e = Emb()
    sub.put(rec('s1', text='needle fact'), both)
    sub.vector_put('s1', 'a', [0.1] * VEC_DIM, both, model_id='m')
    assert recall(sub, 'needle', a, mode='hybrid', embedder=e)[0]
    propose_rule(sub, both, ProposedRule(rule_id='r1', scope='a', name='n', description='needle rule'))
    sub.put(Record(record_id='d1', record_type='Decision', scope='a',
                   payload={'text': 'approve', 'rule_id': 'r1', 'approved': True},
                   attribution='user'), both)
    sub.put(Record(record_id='c1', record_type='Commitment', scope='a',
                   payload={'text': 'we will'}, attribution='user'), both)
    assert sub.vector_index_complete('a', a, model_id='m')
    items, _ = recall(sub, 'needle', a, mode='hybrid', embedder=e)
    assert items


@pytest.mark.parametrize('typ', ['Source', 'Observation', 'Claim'])
def test_missing_vector_on_embeddable_type_still_refuses(env, typ):
    sub, both, a = env
    sub.put(rec('s1', text='needle fact'), both)
    sub.vector_put('s1', 'a', [0.1] * VEC_DIM, both, model_id='m')
    kw = {'Source': {}, 'Observation': {'source_ids': ['s1'], 'attribution': 'document'},
          'Claim': {'source_ids': ['s1'], 'attribution': 'agent', 'epistemic_status': 'derived'}}[typ]
    sub.put(rec('x1', text='needle other', typ=typ, **kw), both)
    assert not sub.vector_index_complete('a', a, model_id='m')
    with pytest.raises(ValueError, match='complete'):
        recall(sub, 'needle', a, mode='hybrid', embedder=Emb())


def test_non_embeddable_type_still_needs_valid_fts_projection(env):
    sub, both, a = env
    sub.put(rec('s1', text='needle fact'), both)
    sub.vector_put('s1', 'a', [0.1] * VEC_DIM, both, model_id='m')
    sub.put(rec('c1', typ='Commitment', text='we will'), both)
    assert sub.vector_index_complete('a', a, model_id='m')
    sub._conn.execute("DELETE FROM fts WHERE record_id='c1'")
    assert not sub.vector_index_complete('a', a, model_id='m')


# ---- (C) orphan scoping at recall level ------------------------------------------------

@pytest.mark.parametrize('mode', ['lexical', 'hybrid'])
def test_orphan_in_scope_b_does_not_fail_recall_in_scope_a(env, mode):
    sub, both, a = env
    sub.put(rec('s1', text='needle fact'), both)
    sub.vector_put('s1', 'a', [0.1] * VEC_DIM, both, model_id='m')
    sub._conn.execute("INSERT INTO fts(record_id,scope,text) VALUES ('ghost','b','needle x')")
    items, _ = recall(sub, 'needle', both, scope_hint='a', mode=mode, embedder=Emb())
    assert items
    with pytest.raises(ValueError, match='complete'):
        recall(sub, 'needle', both, scope_hint='b', mode='lexical')
    with pytest.raises(ValueError, match='complete'):
        recall(sub, 'needle', both, mode='lexical')
    sub._conn.execute("INSERT INTO fts(record_id,scope,text) VALUES ('ghost2','a','needle x')")
    with pytest.raises(ValueError, match='complete'):
        recall(sub, 'needle', both, scope_hint='a', mode=mode, embedder=Emb())
