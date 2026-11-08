"""Round-6 substrate/temporal repairs (Terra/Sonnet R5), written red first.

(1) temporal API read gate; (2) denied correction has no durable effects; (3) import never
transitions an existing rule, and inert rules restore; (4) vector completeness survives deleting a
Correction; (5) legacy invalid records are quarantined by the v7 migration; (6) re-embedding.
"""
import json
import math
import sqlite3

import pytest

from aletheia import cli, temporal
from aletheia.knowledge import KnowledgeRefusal
from aletheia.procedural import list_active_rules, promote_rule
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import (SCHEMA_VERSION, VEC_DIM, Record, Substrate, ScopeViolation)

MODEL = 'm@1'
VEC = [1.0] + [0.0] * (VEC_DIM - 1)

USER = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)


def rec(id, typ='Source', scope='a', **kw):
    return Record(record_id=id, record_type=typ, scope=scope,
                  payload=kw.pop('payload', {'text': 'content ' + id}),
                  attribution=kw.pop('attribution', 'user'), **kw)


def rule(id='r', scope='a', **payload):
    return rec(id, 'ProposedRule', scope=scope,
               payload={'status': 'proposed', 'description': 'x', **payload}, attribution='agent')


def bundle(*records, scope='a'):
    return {'schema_version': SCHEMA_VERSION, 'scope': scope,
            'records': [Substrate._record_to_export(r) for r in records]}


class Emb:
    model_id = MODEL

    def __init__(self):
        self.docs = []

    def embed_document(self, text):
        self.docs.append(text)
        return VEC

    def embed_query(self, text):
        return VEC


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / 's.db', ScopeEngine(known_scopes={'a', 'b'}))
    yield s
    s.close()


# ---- (1) temporal read gate -----------------------------------------------------------------------

def test_effective_at_requires_a_grant_for_the_scope(sub):
    sub.put(rec('secret', scope='b', payload={'text': 'classified'}), USER('b'))
    sub.put(rec('mine', scope='a'), USER('a'))
    ctx_a = USER('a')
    assert temporal.effective_at(sub, ctx_a, 'b', 0) == []
    assert [r.record_id for r in temporal.effective_at(sub, ctx_a, 'a', 0)] == ['mine']
    assert [r.record_id for r in temporal.effective_at(sub, USER('b'), 'b', 0)] == ['secret']
    assert temporal.effective_at(sub, None, 'a', 0) == []
    assert temporal.effective_at(sub, ScopeContext(granted_scopes=frozenset()), 'a', 0) == []


def test_temporal_boost_does_not_probe_unreadable_records(sub):
    sub.put(rec('secret', scope='b', payload={'text': 'on 8 May 2023', 'is_temporal': True,
                                              'temporal_dates': [{'expr': 'x'}]}), USER('b'))
    assert temporal.temporal_boost(sub, USER('a'), 'when was it', ['secret']) == {}
    assert temporal.temporal_boost(sub, None, 'when was it', ['secret']) == {}
    assert 'secret' in temporal.temporal_boost(sub, USER('b'), 'when was it', ['secret'])


def test_close_window_is_scope_gated(sub):
    sub.put(rec('secret', scope='b'), USER('b'))
    assert temporal.close_window(sub, USER('a'), 'secret', 5.0) is False
    assert sub.get('secret', USER('b')).valid_to is None
    assert temporal.close_window(sub, USER('b'), 'secret', 5.0) is True
    assert sub.get('secret', USER('b')).valid_to == 5.0


def test_pure_temporal_helpers_touch_no_store():
    # set_validity / extract_dates / is_temporal_query / apply_temporal_metadata take no store.
    r = rec('x', payload={'text': 'met on 8 May 2023'})
    assert temporal.apply_temporal_metadata(r).payload['is_temporal']
    assert temporal.set_validity(r, 1.0, 2.0).valid_to == 2.0
    assert temporal.is_temporal_query('when') and temporal.extract_dates('8 May 2023')


# ---- (2) denied correction is side-effect free -----------------------------------------------------

def test_denied_correction_collision_changes_nothing(sub):
    ua, ub = USER('a'), USER('b')
    sub.put(rule('rule'), ua)
    sub.put(rec('src'), ua)
    sub.vector_put('src', 'a', VEC, ua, model_id=MODEL)
    sub.put(rec('secret', scope='b'), ub)
    table = sub._vec_table('a', MODEL)

    def snap():
        return (sub._conn.execute("SELECT payload FROM records WHERE record_id='rule'").fetchone(),
                sorted(sub._conn.execute("SELECT record_id, scope, text FROM fts").fetchall()),
                sorted(r[0] for r in sub._conn.execute(f"SELECT record_id FROM {table}")),
                sub._conn.execute("SELECT count(*) FROM records").fetchone())

    before = snap()
    with pytest.raises(ScopeViolation):
        sub.correct('rule', Record(record_id='secret', record_type='Correction', scope='a',
                                   payload={'text': 'fix'}, attribution='user'), ua)
    assert snap() == before
    assert json.loads(before[0][0])['status'] == 'proposed'


# ---- (3) import never transitions an existing rule -------------------------------------------------

def test_import_cannot_activate_existing_proposed_rule(sub):
    u = USER('a')
    sub.put(rule('r'), u)
    sub.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': True}), u)
    b = bundle(sub.get('r', u))
    b['records'][0]['payload'] = {'status': 'active', 'description': 'x', 'promoted_by': 'd'}
    with pytest.raises(KnowledgeRefusal):
        sub.import_scope(b, u, strict=True)
    assert sub.get('r', u).payload['status'] == 'proposed'
    assert list_active_rules(sub, u, 'a') == []


@pytest.mark.parametrize('status', ['rejected', 'deprecated'])
def test_import_cannot_transition_existing_rule_even_to_inert(sub, status):
    u = USER('a')
    sub.put(rule('r'), u)
    b = bundle(sub.get('r', u))
    b['records'][0]['payload'] = {**b['records'][0]['payload'], 'status': status}
    assert sub.import_scope(b, u) == 0
    assert 'differs' in sub.last_import_refusal
    assert sub.get('r', u).payload['status'] == 'proposed'


def test_import_identical_rule_is_skipped_and_different_content_refuses_atomically(sub):
    u = USER('a')
    sub.put(rule('r'), u)
    same = sub.get('r', u)
    assert sub.import_scope(bundle(same, rec('extra')), u) == 1  # only 'extra' is new
    changed = sub.get('r', u)
    changed.payload = {**changed.payload, 'description': 'changed'}
    assert sub.import_scope(bundle(rec('extra2'), changed), u) == 0
    assert sub.get('extra2', u) is None


@pytest.mark.parametrize('status', ['rejected', 'deprecated'])
def test_inert_rules_restore_exactly_as_exported(tmp_path, status):
    src = Substrate(tmp_path / 'src.db', ScopeEngine(known_scopes={'a'}))
    dst = Substrate(tmp_path / 'dst.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        r = rule('r', **{'rejection_reason': 'no'} if status == 'rejected' else {'deprecated_at': 3.0})
        r.payload['status'] = status
        src._conn.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', r.to_row())
        b = src.export_scope('a', u)
        assert dst.import_scope(b, u, strict=True) == 1
        assert dst.get('r', u).payload == r.payload
        assert list_active_rules(dst, u, 'a') == []
        assert dst.import_scope(b, u, strict=True) == 0  # re-import: identical, skipped
    finally:
        src.close()
        dst.close()


def test_import_active_rule_refuses_with_accurate_message(sub):
    u = USER('a')
    r = rule('r')
    r.payload.update(status='active', promoted_by='d')
    assert sub.import_scope(bundle(r), u) == 0
    assert 'active' in sub.last_import_refusal
    assert 'rejected' not in sub.last_import_refusal


def test_correct_then_export_import_round_trips_into_empty_store(tmp_path):
    src = Substrate(tmp_path / 'src.db', ScopeEngine(known_scopes={'a'}))
    dst = Substrate(tmp_path / 'dst.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        src.put(rule('r'), u)
        src.correct('r', rec('c1', 'Correction', payload={'text': 'fixed'}), u)
        assert dst.import_scope(src.export_scope('a', u), u, strict=True) == 2
        assert dst.get('r', u).payload['status'] == 'deprecated'
    finally:
        src.close()
        dst.close()


def test_import_correction_cannot_deprecate_a_live_local_rule(sub):
    u = USER('a')
    sub.put(rule('r'), u)
    c = rec('c1', 'Correction', payload={'text': 'fixed'}, supersedes='r')
    assert sub.import_scope(bundle(c), u) == 0
    assert sub.get('r', u).payload['status'] == 'proposed'


# ---- (4) vectors survive correct + delete Correction -------------------------------------------------

def test_deleting_a_correction_keeps_hybrid_recall_possible(sub):
    u = USER('a')
    for i in ('s1', 's2'):
        sub.put(rec(i, payload={'text': 'harbor ' + i}), u)
        sub.vector_put(i, 'a', VEC, u, model_id=MODEL)
    sub.correct('s1', rec('c1', 'Correction', payload={'text': 'fixed harbor'}), u)
    assert sub.vector_index_complete('a', u, model_id=MODEL)
    assert sub.delete('c1', u)
    assert sub.vector_index_complete('a', u, model_id=MODEL)
    assert sub.vector_has_record('s1', 'a', u, model_id=MODEL)
    items, _ = recall(sub, 'harbor', u, limit=5, mode='hybrid', embedder=Emb())
    assert {i.record_id for i in items} >= {'s1', 's2'}


# ---- (5) legacy invalid records are quarantined -----------------------------------------------------

_V6_DDL = """
CREATE TABLE records (record_id TEXT PRIMARY KEY, record_type TEXT NOT NULL, scope TEXT NOT NULL,
  epistemic_status TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL, effective_at REAL,
  supersedes TEXT, source_ids TEXT NOT NULL DEFAULT '[]', provenance TEXT NOT NULL DEFAULT '{}',
  attribution TEXT, valid_from REAL, valid_to REAL);
CREATE TABLE verifiers (actor_id TEXT PRIMARY KEY, registered_at REAL NOT NULL);
CREATE VIRTUAL TABLE fts USING fts5(record_id UNINDEXED, scope UNINDEXED, text, tokenize='porter unicode61');
CREATE TABLE jobs (job_id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, source_id TEXT NOT NULL,
  stage TEXT NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
  UNIQUE (scope, source_id, stage, content_hash));
CREATE TABLE receipts (receipt_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  scope_consulted TEXT NOT NULL, payload_counts TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE journal (seq INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, record_id TEXT, scope TEXT,
  detail TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entity_links (source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
  valid_from REAL, valid_to REAL, scope TEXT NOT NULL, PRIMARY KEY (source_id, target_id, relation),
  FOREIGN KEY (source_id) REFERENCES records(record_id) ON DELETE CASCADE);
"""

_BAD = {
    'bad-int': ('{"text": 5}', 1.0),
    'bad-list': ('{"text": ["x", "y"]}', 1.0),
    'bad-null': ('{"text": null}', 1.0),
    'bad-dict': ('{"text": {"k": "v"}}', 1.0),
    'bad-inf': ('{"text": "inf time"}', float('inf')),
}


def build_v6_with_legacy(path):
    conn = sqlite3.connect(path)
    conn.executescript(_V6_DDL)
    rows = [('good', '{"text": "hello harbor"}', 1.0)] + [(k, p, t) for k, (p, t) in _BAD.items()]
    for rid, payload, created in rows:
        conn.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (rid, 'Source', 'a', 'stated', payload, created, None, None, '[]', '{}', 'user',
                      None, None))
        conn.execute('INSERT INTO fts(record_id, scope, text) VALUES (?,?,?)',
                     (rid, 'a', 'text hello harbor' if rid == 'good' else 'legacy junk'))
    conn.execute("INSERT INTO meta VALUES ('schema_version','6')")
    conn.execute("INSERT INTO meta VALUES ('store_version','5')")
    conn.execute('PRAGMA user_version = 6')
    conn.commit()
    conn.close()


def test_v7_migration_quarantines_legacy_invalid_records(tmp_path):
    build_v6_with_legacy(tmp_path / 'v6.db')
    s = Substrate(tmp_path / 'v6.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        assert sorted(s.migration_quarantined) == sorted(_BAD)
        # Every read path stays usable and shows only the good record.
        assert [r.record_id for r in s.all_records(u)] == ['good']
        assert s.query(u, scope='a')[0][0].record_id == 'good'
        assert all(s.get(k, u) is None for k in _BAD)
        assert [r['record_id'] for r in s.export_scope('a', u)['records']] == ['good']
        json.dumps([Substrate._record_to_export(r) for r in s.all_records(u)], allow_nan=False)
        assert [r.record_id for r in temporal.effective_at(s, u, 'a', 0)] == ['good']
        assert s.fts_index_complete('a', u)
        items, _ = recall(s, 'harbor', u, limit=5, mode='lexical')
        assert [i.record_id for i in items] == ['good']
        # Rows are preserved, not destroyed, and counted.
        assert s._conn.execute('SELECT count(*) FROM quarantined_records').fetchone()[0] == len(_BAD)
        assert s._conn.execute("SELECT count(*) FROM fts WHERE record_id LIKE 'bad-%'").fetchone()[0] == 0
        inf_row = s._conn.execute(
            "SELECT created_at FROM quarantined_records WHERE record_id='bad-inf'").fetchone()
        assert math.isinf(inf_row[0])
        assert s._conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    finally:
        s.close()


def test_clean_store_quarantines_nothing(sub):
    sub.put(rec('x'), USER('a'))
    assert sub.migration_quarantined == []


# ---- (6) re-embedding existing records -------------------------------------------------------------

def test_reembed_missing_fills_gaps_and_restores_completeness(sub):
    u = USER('a')
    sub.put(rec('s1'), u)
    sub.put(rec('s2'), u)
    sub.put(rec('cl', 'Claim', payload={'text': 'a claim'}, attribution='agent', epistemic_status='derived'), u)
    sub.put(rec('d1', 'Decision', payload={'text': 'no vector needed'}), u)
    sub.vector_put('s1', 'a', VEC, u, model_id=MODEL)
    assert not sub.vector_index_complete('a', u, model_id=MODEL)
    emb = Emb()
    result = sub.reembed_missing('a', u, emb)
    assert result['embedded'] == 2 and result['already_present'] == 1
    assert sorted(emb.docs) == ['a claim', 'content s2']  # s1 already had a vector
    assert sub.vector_index_complete('a', u, model_id=MODEL)
    assert sub.reembed_missing('a', u, emb)['embedded'] == 0


def test_reembed_missing_requires_write_grant(sub):
    sub.put(rec('s1'), USER('a'))
    with pytest.raises(ScopeViolation):
        sub.reembed_missing('a', USER('b'), Emb())
    with pytest.raises(ScopeViolation):
        sub.reembed_missing('a', None, Emb())


def test_reembed_missing_reports_unembeddable_text(sub):
    u = USER('a')
    sub.put(rec('empty', payload={'text': '   '}), u)
    result = sub.reembed_missing('a', u, Emb())
    assert result['embedded'] == 0 and result['skipped_no_text'] == 1


def test_cli_reembed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'default_text_embedder', lambda: Emb())
    db = tmp_path / 'w.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    s.put(rec('s1'), USER('a'))
    s.close()
    assert cli.main(['--db', str(db), 'reembed', '--scope', 'a']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'reembedded' and out['embedded'] == 1 and out['index_complete'] is True
