"""Round-5 substrate repairs (Terra/Sonnet R4 findings), written red first.

Items: (1) v7 migration FTS duplicates, tested against a v6 store built by hand with the old
layout; (2) migration re-approval for rules that already have binding rows; (3) prune_derived
policy; (4) orphan ownership from the FTS row's own scope; (5) correction / extraction vs vector
completeness; (6) concurrent first open; (7) idempotent import of already-present records.
"""
import json
import sqlite3
import threading
import time

import pytest

from aletheia import cli
from aletheia.knowledge import KnowledgeRefusal
from aletheia.procedural import list_active_rules, promote_rule
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import VEC_DIM, Record, Substrate

MODEL = 'm@1'
VEC = [1.0] + [0.0] * (VEC_DIM - 1)

USER = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)
AGENT = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes))


def rec(id, typ='Source', scope='a', **kw):
    return Record(record_id=id, record_type=typ, scope=scope,
                  payload=kw.pop('payload', {'text': 'content ' + id}),
                  attribution=kw.pop('attribution', 'user'), **kw)


def rule(id='r', **payload):
    return rec(id, 'ProposedRule', payload={'status': 'proposed', 'description': 'x', **payload},
               attribution='agent')


# ---- hand-built schema-6 store (old FTS layout: no fts_rowmap, no rule_* tables) ---------------

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


def build_v6(path, *, with_bindings=False):
    """A schema-6 store written without any current-code index: old FTS layout, no rowmap."""
    conn = sqlite3.connect(path)
    conn.executescript(_V6_DDL)
    records = [
        rec('note', payload={'text': 'zanzibar harbour'}, created_at=1.0),
        rec('ract', 'ProposedRule', created_at=1.0, attribution='agent',
            payload={'status': 'active', 'description': 'x', 'promoted_by': 'd1', 'promoted_at': 2.0}),
        rec('rprop', 'ProposedRule', created_at=1.0, attribution='agent',
            payload={'status': 'proposed', 'description': 'y'}),
        rec('d1', 'Decision', created_at=1.0, payload={'rule_id': 'ract', 'approved': True}),
        rec('d2', 'Decision', created_at=1.0, payload={'rule_id': 'rprop', 'approved': True}),
    ]
    for r in records:
        conn.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', r.to_row())
        conn.execute('INSERT INTO fts(record_id, scope, text) VALUES (?,?,?)',
                     (r.record_id, r.scope, Substrate._payload_only_text(Record.from_row(r.to_row()))))
    if with_bindings:
        # What the unversioned binding implementation left behind: valid-looking tokens and hashes.
        conn.execute('CREATE TABLE rule_incarnations (rule_id TEXT PRIMARY KEY, token TEXT NOT NULL)')
        conn.execute('CREATE TABLE rule_approvals (promoter_id TEXT NOT NULL, rule_id TEXT NOT NULL, '
                     'token TEXT NOT NULL, proposal_hash TEXT NOT NULL, PRIMARY KEY (promoter_id, rule_id))')
        for r, promoter in ((records[1], 'd1'), (records[2], 'd2')):
            conn.execute('INSERT INTO rule_incarnations VALUES (?, ?)', (r.record_id, 'oldtoken'))
            conn.execute('INSERT INTO rule_approvals VALUES (?,?,?,?)',
                         (promoter, r.record_id, 'oldtoken', Substrate._proposal_hash(r)))
    conn.execute("INSERT INTO meta VALUES ('schema_version','6')")
    conn.execute("INSERT INTO meta VALUES ('store_version','5')")
    conn.execute('PRAGMA user_version = 6')
    conn.commit()
    conn.close()


# ---- (1) migration FTS duplicates -----------------------------------------------------------------

def test_real_v6_store_has_no_rowmap_or_current_indexes(tmp_path):
    build_v6(tmp_path / 'v6.db')
    conn = sqlite3.connect(tmp_path / 'v6.db')
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert 'fts_rowmap' not in names and 'rule_incarnations' not in names
    conn.close()


def test_v6_migration_leaves_exactly_one_fts_row_per_record(tmp_path):
    path = tmp_path / 'v6.db'
    build_v6(path)
    c = USER('a')
    s = Substrate(path, ScopeEngine(known_scopes={'a'}))
    try:
        assert s.get('ract', c).payload['status'] == 'proposed'
        rows = s._conn.execute("SELECT record_id, count(*) FROM fts GROUP BY record_id").fetchall()
        assert all(n == 1 for _, n in rows), rows
        assert s.fts_index_complete('a', c)
        # The stale pre-migration projection is gone, not merely shadowed.
        assert not s._conn.execute("SELECT 1 FROM fts WHERE text LIKE '%promoted_by%'").fetchone()
    finally:
        s.close()


# ---- (2) migration with existing binding rows -----------------------------------------------------

def test_v6_store_with_existing_bindings_requires_fresh_approval(tmp_path):
    path = tmp_path / 'v6b.db'
    build_v6(path, with_bindings=True)
    c = USER('a')
    s = Substrate(path, ScopeEngine(known_scopes={'a'}))
    try:
        assert s.get('ract', c).payload['status'] == 'proposed'
        assert list_active_rules(s, c, 'a') == []
        assert 'ract' in s.migration_reapproval
        assert s.fts_index_complete('a', c)
        # Old bindings are untrusted for every rule, not only the formerly active one.
        for rid, did in (('ract', 'd1'), ('rprop', 'd2')):
            with pytest.raises(Exception):
                promote_rule(s, c, rid, did)
        s.put(rec('n1', 'Decision', payload={'rule_id': 'ract', 'approved': True}), c)
        assert promote_rule(s, c, 'ract', 'n1').payload['status'] == 'active'
    finally:
        s.close()


# ---- (3) prune_derived ----------------------------------------------------------------------------

def _old(s, *ids):
    for i in ids:
        s._conn.execute('UPDATE records SET created_at=1.0 WHERE record_id=?', (i,))


def test_prune_cascades_lineage_and_leaves_no_dangling_citation(tmp_path):
    s = Substrate(tmp_path / 'p.db', ScopeEngine(known_scopes={'a'}))
    try:
        u, ag = USER('a'), AGENT('a')
        s.put(rec('s1'), u)
        s.put(rec('cl1', 'Claim', attribution='agent', epistemic_status='derived',
                  source_ids=['s1'], payload={'text': 'claimtext'}), ag)
        s.put(rec('ob1', 'Observation', attribution='document', source_ids=['cl1'],
                  payload={'text': 'obstext'}), u)
        s._conn.execute("INSERT INTO entity_links VALUES ('ob1','cl1','mentions',NULL,NULL,'a')")
        _old(s, 'cl1')
        n = s.prune_derived('a', time.time(), ag)
        assert n >= 1
        assert s.get('cl1', u) is None
        assert s.get('ob1', u) is None, 'dependent Observation must be cascaded, not left dangling'
        assert s.get('s1', u) is not None
        for r in s.query(u)[0]:
            assert all(s.get(i, u) is not None for i in r.source_ids), r.record_id
        assert s._conn.execute("SELECT count(*) FROM entity_links").fetchone()[0] == 0
        assert s.fts_index_complete('a', u)
        assert not s._conn.execute("SELECT 1 FROM fts WHERE text LIKE '%obstext%'").fetchone()
    finally:
        s.close()


def test_prune_never_deletes_verified_records(tmp_path):
    s = Substrate(tmp_path / 'v.db', ScopeEngine(known_scopes={'a'}, ))
    try:
        u = USER('a')
        s.register_verifier('ver', u)
        vctx = ScopeContext(granted_scopes=frozenset({'a'}), actor='ver')
        s.put(rec('s1'), u)
        s.put(rec('v1', 'Verification', attribution='verifier', payload={'claim_id': 'cv'}), vctx)
        s.put(rec('cv', 'Claim', attribution='verifier', epistemic_status='verified',
                  source_ids=['v1'], payload={'text': 'verified'}), vctx)
        # A derived claim whose lineage a verified claim retains must survive too.
        s.put(rec('v2', 'Verification', attribution='verifier', payload={'claim_id': 'cv2'}), vctx)
        s.put(rec('d0', 'Claim', attribution='agent', epistemic_status='derived',
                  source_ids=['s1'], payload={'text': 'base'}), AGENT('a'))
        s.put(rec('cv2', 'Claim', attribution='verifier', epistemic_status='verified',
                  source_ids=['v2', 'd0'], payload={'text': 'verified2'}), vctx)
        _old(s, 'cv', 'cv2', 'd0', 'v1', 'v2')
        s.prune_derived('a', time.time(), AGENT('a'))
        assert s.get('cv', u) is not None and s.get('cv2', u) is not None
        assert s.get('d0', u) is not None, 'cannot prune a record a verified claim cites'
    finally:
        s.close()


# ---- (4) orphan ownership -------------------------------------------------------------------------

def test_foreign_scope_orphan_does_not_fail_this_scope(tmp_path):
    s = Substrate(tmp_path / 'o.db', ScopeEngine(known_scopes={'a'}))  # request-scope engine
    try:
        u = USER('a')
        s.put(rec('s1'), u)
        s._conn.execute("INSERT INTO fts(record_id, scope, text) VALUES ('ghost','b','x')")
        assert s.fts_index_complete('a', u)
        s._conn.execute("INSERT INTO fts(record_id, scope, text) VALUES ('ghost2','a','x')")
        assert not s.fts_index_complete('a', u), 'an orphan labelled with this scope still fails'
    finally:
        s.close()


# ---- (5) vector completeness ----------------------------------------------------------------------

def test_correcting_an_embedded_record_keeps_vector_index_complete(tmp_path):
    s = Substrate(tmp_path / 'c.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        for i in ('s1', 's2'):
            s.put(rec(i), u)
            s.vector_put(i, 'a', VEC, u, model_id=MODEL)
        assert s.vector_index_complete('a', u, model_id=MODEL)
        s.correct('s1', rec('c1', 'Correction', payload={'text': 'fixed'}), u)
        assert s.vector_index_complete('a', u, model_id=MODEL)
    finally:
        s.close()


def test_unsuperseded_record_without_vector_still_incomplete(tmp_path):
    s = Substrate(tmp_path / 'c2.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        s.put(rec('s1'), u)
        s.put(rec('s2'), u)
        s.vector_put('s1', 'a', VEC, u, model_id=MODEL)
        assert not s.vector_index_complete('a', u, model_id=MODEL)
    finally:
        s.close()


class _Emb:
    model_id = MODEL

    def embed_document(self, text):
        return VEC

    def embed_query(self, text):
        return VEC


def test_cli_extract_keeps_vector_index_complete(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('ALETHEIA_LLM_PROVIDER', 'openai-compatible')
    monkeypatch.setenv('ALETHEIA_LLM_BASE_URL', 'https://extract.example/v1')
    monkeypatch.setenv('ALETHEIA_LLM_MODEL', 'test-model')
    monkeypatch.setenv('ALETHEIA_LLM_API_KEY', 'test-key')
    monkeypatch.setattr(cli, 'default_text_embedder', lambda: _Emb())
    db = tmp_path / 'w.db'
    note = tmp_path / 'n.md'
    note.write_text('Alice lives in Paris. Bob works at Acme Corporation. The deadline is Friday.',
                    encoding='utf-8')
    assert cli.main(['--db', str(db), 'ingest', '--scope', 'k', '--file', str(note)]) == 0
    capsys.readouterr()
    s0 = Substrate(db, ScopeEngine(known_scopes={'k'}))
    src = s0.query(USER('k'), record_type='Source')[0][0].record_id
    s0.close()
    claim = Record(record_id='xc1', record_type='Claim', scope='k', attribution='agent',
                   epistemic_status='derived', source_ids=[src], payload={'text': 'alice lives in paris'})
    import aletheia.extraction as extraction
    monkeypatch.setattr(extraction, 'extract_facts', lambda *a, **k: [claim])
    assert cli.main(['--db', str(db), 'extract', '--scope', 'k', '--allow-external']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['claims_written'] >= 1, out
    s = Substrate(db, ScopeEngine(known_scopes={'k'}))
    try:
        assert s.vector_index_complete('k', USER('k'), model_id=MODEL)
    finally:
        s.close()


# ---- (6) concurrent first open --------------------------------------------------------------------

def test_concurrent_first_open_of_fresh_db_tolerates_wal_lock(tmp_path):
    failures = []
    for round_ in range(15):
        path = tmp_path / f'fresh{round_}' / 'x.db'
        barrier = threading.Barrier(12)

        def worker():
            try:
                barrier.wait()
                Substrate(path, ScopeEngine(known_scopes={'a'})).close()
            except Exception as exc:  # noqa: BLE001
                failures.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert not failures, failures[:3]


def test_concurrent_upgrade_does_not_rerun_rule_migration(tmp_path):
    path = tmp_path / 'v6.db'
    build_v6(path)
    first = Substrate(path, ScopeEngine(known_scopes={'a'}))
    c = USER('a')
    first.put(rec('n1', 'Decision', payload={'rule_id': 'ract', 'approved': True}), c)
    promote_rule(first, c, 'ract', 'n1')
    first.close()
    # A second opener that read user_version before the first committed must not demote again.
    s = Substrate(path, ScopeEngine(known_scopes={'a'}))
    try:
        assert s.get('ract', c).payload['status'] == 'active'
        assert s.migration_reapproval == []
    finally:
        s.close()


# ---- (7) idempotent import ------------------------------------------------------------------------

def test_reimport_into_same_store_skips_identical_records(tmp_path):
    s = Substrate(tmp_path / 'i.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        s.put(rec('s1'), u)
        s.put(rule('r1'), u)
        s.correct('s1', rec('c1', 'Correction', payload={'text': 'fixed'}), u)
        bundle = s.export_scope('a', u)
        before = s._conn.execute('SELECT count(*) FROM records').fetchone()[0]
        assert s.import_scope(bundle, u, strict=True) == 0
        assert s.last_import_refusal is None
        assert s._conn.execute('SELECT count(*) FROM records').fetchone()[0] == before
    finally:
        s.close()


def test_partial_store_import_adds_only_missing_records(tmp_path):
    s = Substrate(tmp_path / 'i2.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        s.put(rec('s1'), u)
        s.put(rule('r1'), u)
        s.put(rec('s2'), u)
        bundle = s.export_scope('a', u)
        assert s.delete('s2', u)
        assert s.import_scope(bundle, u, strict=True) == 1
        assert s.get('s2', u) is not None
    finally:
        s.close()


def test_import_still_refuses_different_content_for_existing_rule_and_correction(tmp_path):
    s = Substrate(tmp_path / 'i3.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        s.put(rec('s1'), u)
        s.put(rule('r1'), u)
        s.correct('s1', rec('c1', 'Correction', payload={'text': 'fixed'}), u)
        good = s.export_scope('a', u)
        for rid, edit in (('r1', lambda p: p.update(description='changed')),
                          ('c1', lambda p: p.update(text='other'))):
            bad = json.loads(json.dumps(good))
            for r in bad['records']:
                if r['record_id'] == rid:
                    edit(r['payload'])
            assert s.import_scope(bad, u) == 0
            assert s.last_import_refusal
            with pytest.raises(KnowledgeRefusal):
                s.import_scope(bad, u, strict=True)
    finally:
        s.close()


def test_import_still_never_restores_approvals_or_active_rules(tmp_path):
    s = Substrate(tmp_path / 'i4.db', ScopeEngine(known_scopes={'a'}))
    t = Substrate(tmp_path / 'i5.db', ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        s.put(rule('r1'), u)
        s.put(rec('d', 'Decision', payload={'rule_id': 'r1', 'approved': True}), u)
        promote_rule(s, u, 'r1', 'd')
        bundle = s.export_scope('a', u)
        assert t.import_scope(bundle, u) == 0  # active rule refuses the whole bundle
        assert t.get('r1', u) is None
    finally:
        s.close()
        t.close()
