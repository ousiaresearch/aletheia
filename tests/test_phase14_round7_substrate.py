"""Round-7 substrate repairs (Sonnet R6-03..07), written red first.

(03) quarantined legacy rows are erasable and listable; (04) text-less records do not disable
hybrid recall; (05) correct-then-erase still exports/imports; (06) v7 deprecates corrected legacy
rules and the import refusal names the real cause; (07) reembed_missing rejects a stale vector.
"""
import sqlite3

import pytest

from weft.knowledge import KnowledgeRefusal
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import SCHEMA_VERSION, VEC_DIM, Record, ScopeViolation, Substrate

MODEL = 'm@1'
VEC = [1.0] + [0.0] * (VEC_DIM - 1)
USER = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)


def rec(id, typ='Source', scope='a', **kw):
    return Record(record_id=id, record_type=typ, scope=scope,
                  payload=kw.pop('payload', {'text': 'content ' + id}),
                  attribution=kw.pop('attribution', 'user'), **kw)


def bundle(*records, scope='a'):
    return {'schema_version': SCHEMA_VERSION, 'scope': scope,
            'records': [Substrate._record_to_export(r) for r in records]}


class Emb:
    model_id = MODEL

    def __init__(self, hook=None):
        self.docs, self.hook = [], hook

    def embed_document(self, text):
        self.docs.append(text)
        if self.hook:
            self.hook(text)
        return VEC

    def embed_query(self, text):
        return VEC


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / 's.db', ScopeEngine(known_scopes={'a', 'b'}))
    yield s
    s.close()


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


def build_v6(path, rows):
    """rows: (id, type, scope, status, payload_json, created_at, supersedes)."""
    conn = sqlite3.connect(path)
    conn.executescript(_V6_DDL)
    for rid, typ, scope, status, payload, created, supersedes in rows:
        conn.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (rid, typ, scope, status, payload, created, None, supersedes, '[]', '{}',
                      'agent' if typ == 'ProposedRule' else 'user', None, None))
    conn.execute("INSERT INTO meta VALUES ('schema_version','6')")
    conn.execute("INSERT INTO meta VALUES ('store_version','5')")
    conn.execute('PRAGMA user_version = 6')
    conn.commit()
    conn.close()


# ---- R6-03 quarantine is erasable -------------------------------------------------------------

SECRET = b'LEGACY-PLAINTEXT-bad-int'


def quarantined_store(tmp_path):
    p = tmp_path / 'q.db'
    build_v6(p, [('bad-int', 'Source', 'a', 'stated',
                  '{"text": 5, "secret": "LEGACY-PLAINTEXT-bad-int"}', 1.0, None),
                 ('bad-int2', 'Source', 'b', 'stated',
                  '{"text": 5, "secret": "LEGACY-PLAINTEXT-other"}', 1.0, None),
                 ('good', 'Source', 'a', 'stated', '{"text": "hello"}', 1.0, None)])
    return p


def all_table_text(s):
    out = []
    for (name,) in s._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        try:
            for row in s._conn.execute(f'SELECT * FROM "{name}"').fetchall():
                out.append(repr(row))
        except sqlite3.Error:
            pass
    return '\n'.join(out)


def test_delete_erases_quarantined_legacy_record(tmp_path):
    p = quarantined_store(tmp_path)
    s = Substrate(p, ScopeEngine(known_scopes={'a', 'b'}))
    try:
        assert 'LEGACY-PLAINTEXT-bad-int' in all_table_text(s)
        assert s.delete('bad-int', USER('a')) is True
        text = all_table_text(s)
        assert 'LEGACY-PLAINTEXT-bad-int' not in text
        assert 'LEGACY-PLAINTEXT-other' in text  # other row untouched
        assert s.delete('bad-int', USER('a')) is False
    finally:
        s.close()
    assert SECRET not in b''.join(f.read_bytes() for f in tmp_path.glob('q.db*'))


def test_quarantine_delete_obeys_write_authority(tmp_path):
    s = Substrate(quarantined_store(tmp_path), ScopeEngine(known_scopes={'a', 'b'}))
    try:
        assert s.delete('bad-int2', USER('a')) is False
        assert s.delete('bad-int2', None) is False
        assert 'LEGACY-PLAINTEXT-other' in all_table_text(s)  # still there after every refusal
        assert s.delete('bad-int2', USER('b')) is True
        assert 'LEGACY-PLAINTEXT-other' not in all_table_text(s)
    finally:
        s.close()


def test_list_and_purge_quarantined(tmp_path):
    s = Substrate(quarantined_store(tmp_path), ScopeEngine(known_scopes={'a', 'b'}))
    try:
        listed = s.list_quarantined(USER('a'))
        assert [q['record_id'] for q in listed] == ['bad-int']  # only readable scopes, no payload
        assert 'LEGACY-PLAINTEXT' not in repr(listed)
        assert s.list_quarantined(None) == []
        assert s.purge_quarantined(None) == 0
        assert s.purge_quarantined(USER('a'), scope='b') == 0
        assert s.purge_quarantined(USER('a'), record_id='bad-int2') == 0
        assert s.purge_quarantined(USER('a')) == 1
        assert s.list_quarantined(USER('a', 'b'))[0]['record_id'] == 'bad-int2'
        assert s.purge_quarantined(USER('a', 'b')) == 1
        assert 'LEGACY-PLAINTEXT' not in all_table_text(s)
    finally:
        s.close()


def test_delete_of_live_id_also_removes_quarantined_copy(sub):
    u = USER('a')
    sub.put(rec('x'), u)
    sub._conn.execute(
        "INSERT INTO quarantined_records(record_id, reason, record_type, scope, payload) "
        "VALUES ('x','legacy','Source','a','{\"text\": 5, \"s\": \"DUP-SECRET\"}')")
    sub._conn.commit()
    assert sub.delete('x', u) is True
    assert 'DUP-SECRET' not in all_table_text(sub)


def test_prune_derived_reaches_quarantined_rows(tmp_path):
    p = tmp_path / 'p.db'
    build_v6(p, [('old-bad', 'Claim', 'a', 'derived', '{"text": 5, "s": "PRUNE-SECRET"}', 1.0, None)])
    s = Substrate(p, ScopeEngine(known_scopes={'a'}))
    try:
        assert s.prune_derived('a', 100.0, USER('a')) == 1
        assert 'PRUNE-SECRET' not in all_table_text(s)
    finally:
        s.close()


# ---- R6-04 text-less records do not block hybrid completeness ---------------------------------

def test_textless_records_do_not_disable_vector_completeness(sub):
    u = USER('a')
    sub.put(rec('t1'), u)
    sub.put(rec('n1', attribution='agent', payload={'title': 'no text', 'k': 1}), u)
    sub.put(rec('n2', attribution='agent', payload={'text': ''}), u)
    sub.put(rec('n3', attribution='agent', payload={'text': '   '}), u)
    assert not sub.vector_index_complete('a', u, model_id=MODEL)
    res = sub.reembed_missing('a', u, Emb())
    assert res['embedded'] == 1 and res['skipped_no_text'] == 3
    assert sub.vector_index_complete('a', u, model_id=MODEL)


def test_textless_only_scope_is_complete(sub):
    u = USER('a')
    sub.put(rec('n1', payload={'title': 'x'}), u)
    assert sub.vector_index_complete('a', u, model_id=MODEL)


# ---- R6-05 correct, erase original, export, import -------------------------------------------

def make_corrected_then_erased(sub, u):
    sub.put(rec('orig', payload={'text': 'ERASED-PLAINTEXT original'}), u)
    sub.correct('orig', rec('fix1', 'Correction', payload={'text': 'the fix'}), u)
    assert sub.delete('orig', u) is True


def test_export_after_erasing_corrected_original_restores_into_empty_store(sub, tmp_path):
    u = USER('a')
    make_corrected_then_erased(sub, u)
    data = sub.export_scope('a', u)
    assert 'ERASED-PLAINTEXT' not in repr(data)
    fresh = Substrate(tmp_path / 'fresh.db', ScopeEngine(known_scopes={'a'}))
    try:
        assert fresh.import_scope(data, u, strict=True) == 1
        assert fresh.get('fix1', u) is not None
        assert fresh.get('orig', u) is None
        assert 'ERASED-PLAINTEXT' not in all_table_text(fresh)
        # Idempotent, and the restored store re-exports the same tombstone.
        assert fresh.import_scope(data, u, strict=True) == 0
        assert fresh.export_scope('a', u)['records'][0].get('lineage_erased') is True
    finally:
        fresh.close()


def test_marked_correction_does_not_touch_an_existing_record_of_the_old_id(sub, tmp_path):
    # Round 8: no id of the erased original is stored, so the import cannot hijack or collide with it.
    u = USER('a')
    make_corrected_then_erased(sub, u)
    data = sub.export_scope('a', u)
    victim = Substrate(tmp_path / 'v.db', ScopeEngine(known_scopes={'a'}))
    try:
        victim.put(rec('orig', payload={'text': 'live record'}), u)
        assert victim.import_scope(data, u, strict=True) == 1
        assert victim.get('orig', u).payload['text'] == 'live record'
        assert victim.get('orig', u).supersedes is None
    finally:
        victim.close()


def test_lineage_marker_refused_when_malformed_or_misplaced(sub):
    u = USER('a')
    c = rec('fix2', 'Correction', payload={'text': 'f'})
    for bad in ({'x': 1}, '../bad', 7, False):
        b = bundle(c)
        b['records'][0]['lineage_erased'] = bad
        with pytest.raises(KnowledgeRefusal):
            sub.import_scope(b, u, strict=True)
    assert sub.get('fix2', u) is None
    # Plain Correction with no target and no marker is still refused.
    with pytest.raises(KnowledgeRefusal):
        sub.import_scope(bundle(c), u, strict=True)
    # A marker on a non-Correction is refused.
    b = bundle(rec('plain'))
    b['records'][0]['lineage_erased'] = True
    with pytest.raises(KnowledgeRefusal):
        sub.import_scope(b, u, strict=True)


def test_deleting_the_correction_drops_its_marker(sub):
    u = USER('a')
    make_corrected_then_erased(sub, u)
    assert sub.delete('fix1', u)
    assert sub._conn.execute("SELECT count(*) FROM lineage_erased").fetchone()[0] == 0


# ---- R6-06 migration deprecates corrected legacy rules; honest refusal ------------------------

def test_v7_deprecates_legacy_rule_superseded_by_correction(tmp_path):
    p = tmp_path / 'r.db'
    build_v6(p, [
        ('rB', 'ProposedRule', 'a', 'stated', '{"status": "proposed", "description": "x"}', 1.0, None),
        ('rC', 'ProposedRule', 'a', 'stated', '{"status": "proposed", "description": "y"}', 1.0, None),
        ('cB', 'Correction', 'a', 'stated', '{"text": "fix"}', 2.0, 'rB'),
    ])
    s = Substrate(p, ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        assert s.get('rB', u).payload['status'] == 'deprecated'
        assert s.get('rC', u).payload['status'] == 'proposed'
        fresh = Substrate(tmp_path / 'f.db', ScopeEngine(known_scopes={'a'}))
        try:
            assert fresh.import_scope(s.export_scope('a', u), u, strict=True) == 3
        finally:
            fresh.close()
    finally:
        s.close()


def test_import_refusal_for_correction_of_rule_in_same_bundle_is_honest(sub):
    u = USER('a')
    r = rec('rB', 'ProposedRule', payload={'status': 'proposed', 'description': 'x'}, attribution='agent')
    c = rec('cB', 'Correction', payload={'text': 'fix'}, supersedes='rB')
    with pytest.raises(KnowledgeRefusal) as e:
        sub.import_scope(bundle(r, c), u, strict=True)
    msg = str(e.value)
    assert 'this bundle' in msg and 'existing rule' not in msg.replace('would deprecate the rule', '')


# ---- R6-07 reembed refuses a stale vector -----------------------------------------------------

def test_reembed_skips_record_overwritten_during_embedding(sub):
    u = USER('a')
    sub.put(rec('x1', payload={'text': 'original harbor text'}), u)
    sub.put(rec('x2'), u)

    def hook(text):
        if text.startswith('original'):
            sub.put(rec('x1', payload={'text': 'completely different replacement'}), u, overwrite=True)

    res = sub.reembed_missing('a', u, Emb(hook))
    assert res['embedded'] == 1 and res['skipped_changed'] == 1
    assert not sub.vector_has_record('x1', 'a', u, model_id=MODEL)
    assert not sub.vector_index_complete('a', u, model_id=MODEL)
    assert sub.reembed_missing('a', u, Emb())['embedded'] == 1  # the next run repairs it
    assert sub.vector_index_complete('a', u, model_id=MODEL)
