"""Round-8 substrate repairs (Terra/Sonnet R7), written red first.

(A) erased originals leave NO id behind: a Correction carries a boolean ``lineage_erased`` marker;
(B) v7 deprecates a legacy ACTIVE rule that a Correction supersedes;
(C) erasing a quarantined row repairs dependents; migration never leaves a dangling Correction;
(D) deletion leaves no plaintext in the DB file, the WAL, or the FTS shadow tables.
"""
import sqlite3
from pathlib import Path

import pytest

from aletheia.knowledge import KnowledgeRefusal
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import SCHEMA_VERSION, Record, Substrate

from tests.test_phase14_round7_substrate import build_v6, all_table_text

USER = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)
AGENT = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes), user_authored=False)


def rec(id, typ='Source', scope='a', **kw):
    return Record(record_id=id, record_type=typ, scope=scope,
                  payload=kw.pop('payload', {'text': 'content ' + id}),
                  attribution=kw.pop('attribution', 'user'), **kw)


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / 's.db', ScopeEngine(known_scopes={'a', 'b'}))
    yield s
    s.close()


def fresh_store(tmp_path, name='fresh.db'):
    return Substrate(tmp_path / name, ScopeEngine(known_scopes={'a', 'b'}))


def corrected_then_erased(sub, u):
    sub.put(rec('orig', payload={'text': 'ERASED-PLAINTEXT original'}), u)
    sub.correct('orig', rec('fix1', 'Correction', payload={'text': 'the fix'}), u)
    assert sub.delete('orig', u) is True


# ---- (A) lineage_erased marker -----------------------------------------------------------------

def test_marker_set_export_has_no_erased_id(sub):
    u = USER('a')
    corrected_then_erased(sub, u)
    out = sub.export_scope('a', u)['records']
    assert len(out) == 1
    assert out[0]['lineage_erased'] is True and out[0]['supersedes'] is None
    assert 'erased_supersedes' not in out[0]
    assert 'orig' not in repr(out)
    # no table anywhere stores the erased id apart from the (id-only) journal
    assert not any(r[0] == 'erased_supersedes' for r in sub._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    for table in ('records', 'lineage_erased'):
        assert "'orig'" not in repr(sub._conn.execute(f'SELECT * FROM {table}').fetchall())


def test_id_reuse_after_delete_exports_and_imports(sub, tmp_path):
    u = USER('a')
    corrected_then_erased(sub, u)
    sub.put(rec('orig', payload={'text': 'brand new orig'}), u)
    data = sub.export_scope('a', u)
    fresh = fresh_store(tmp_path)
    try:
        assert fresh.import_scope(data, u, strict=True) == 2
        assert fresh.get('orig', u).payload['text'] == 'brand new orig'
        assert fresh.get('fix1', u) is not None
        assert fresh.export_scope('a', u)['records'][0].get('lineage_erased') in (True, None)
    finally:
        fresh.close()


def test_roundtrip_into_empty_store_and_reexport_keeps_marker(sub, tmp_path):
    u = USER('a')
    corrected_then_erased(sub, u)
    data = sub.export_scope('a', u)
    fresh = fresh_store(tmp_path)
    try:
        assert fresh.import_scope(data, u, strict=True) == 1
        assert fresh.import_scope(data, u, strict=True) == 0  # idempotent
        assert fresh.export_scope('a', u)['records'][0]['lineage_erased'] is True
    finally:
        fresh.close()


def test_non_user_authored_import_of_marked_correction_refused(sub, tmp_path):
    u = USER('a')
    corrected_then_erased(sub, u)
    data = sub.export_scope('a', u)
    # an agent-attributed copy, so only the marker rule can decide the outcome
    data['records'][0]['attribution'] = 'agent'
    fresh = fresh_store(tmp_path)
    try:
        with pytest.raises(KnowledgeRefusal) as e:
            fresh.import_scope(data, AGENT('a'), strict=True)
        assert fresh.get('fix1', u) is None
        plain = {'schema_version': SCHEMA_VERSION, 'scope': 'a',
                 'records': [Substrate._record_to_export(rec('p1', 'Correction', attribution='agent'))]}
        with pytest.raises(KnowledgeRefusal) as e2:
            fresh.import_scope(plain, AGENT('a'), strict=True)
        # the generic refusal: identical text whether or not the marker is present
        assert str(e.value).split(':', 1)[-1] == str(e2.value).split(':', 1)[-1]
        assert 'orig' not in str(e.value)
    finally:
        fresh.close()


def test_marker_forgery_variants_refused(sub):
    u = USER('a')
    c = Substrate._record_to_export(rec('c1', 'Correction'))
    for bad in (False, 'yes', 1, None, {'x': 1}):
        b = {'schema_version': SCHEMA_VERSION, 'scope': 'a', 'records': [{**c, 'lineage_erased': bad}]}
        with pytest.raises(KnowledgeRefusal):
            sub.import_scope(b, u, strict=True)
    # marker on a non-Correction, and marker on a Correction that still has a supersedes
    s1 = Substrate._record_to_export(rec('s1'))
    with pytest.raises(KnowledgeRefusal):
        sub.import_scope({'schema_version': SCHEMA_VERSION, 'scope': 'a',
                          'records': [{**s1, 'lineage_erased': True}]}, u, strict=True)
    sub.put(rec('t1'), u)
    c2 = Substrate._record_to_export(rec('c2', 'Correction', supersedes='t1'))
    with pytest.raises(KnowledgeRefusal):
        sub.import_scope({'schema_version': SCHEMA_VERSION, 'scope': 'a',
                          'records': [{**c2, 'lineage_erased': True}]}, u, strict=True)
    assert sub.get('c1', u) is None and sub.get('s1', u) is None and sub.get('c2', u) is None


def test_foreign_id_probing_is_not_an_oracle(sub):
    u = USER('a')
    sub.put(rec('foreign', scope='b'), USER('b'))

    def probe(target, cid):
        c = Substrate._record_to_export(rec(cid, 'Correction', supersedes=target))
        try:
            sub.import_scope({'schema_version': SCHEMA_VERSION, 'scope': 'a', 'records': [c]}, u, strict=True)
            return 'ok'
        except KnowledgeRefusal as exc:
            return str(exc).split(':', 1)[-1]

    assert probe('foreign', 'p1') == probe('absent-xyz', 'p2')
    # a marked Correction never consults other ids: identical outcome with or without the id existing
    marked = lambda cid: {'schema_version': SCHEMA_VERSION, 'scope': 'a', 'records': [
        {**Substrate._record_to_export(rec(cid, 'Correction')), 'lineage_erased': True}]}
    assert sub.import_scope(marked('m1'), u, strict=True) == 1
    sub.put(rec('absent-xyz', scope='b'), USER('b'))
    assert sub.import_scope(marked('m2'), u, strict=True) == 1


def test_import_does_no_store_wide_lookup_for_marked_correction(sub):
    u = USER('a')
    c = {**Substrate._record_to_export(rec('m1', 'Correction')), 'lineage_erased': True}
    seen = []
    orig = sub._lookup_record
    sub._lookup_record = lambda rid: (seen.append(rid), orig(rid))[1]
    sub.import_scope({'schema_version': SCHEMA_VERSION, 'scope': 'a', 'records': [c]}, u, strict=True)
    assert set(seen) <= {'m1'}


def test_deleting_marked_correction_drops_marker(sub):
    u = USER('a')
    corrected_then_erased(sub, u)
    assert sub.delete('fix1', u)
    assert sub._conn.execute('SELECT count(*) FROM lineage_erased').fetchone()[0] == 0


def test_dev_schema_erased_supersedes_table_is_dropped(tmp_path):
    p = tmp_path / 'dev.db'
    s = Substrate(p, ScopeEngine(known_scopes={'a'}))
    s.put(rec('fix1x', 'Source'), USER('a'))
    s.close()
    conn = sqlite3.connect(p)
    conn.execute('CREATE TABLE erased_supersedes (record_id TEXT PRIMARY KEY, erased_id TEXT NOT NULL)')
    conn.execute("INSERT INTO erased_supersedes VALUES ('fix1x', 'GONE-ID')")
    conn.commit()
    conn.close()
    s = Substrate(p, ScopeEngine(known_scopes={'a'}))
    try:
        assert not any(r[0] == 'erased_supersedes' for r in s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        s.close()
    assert b'GONE-ID' not in b''.join(f.read_bytes() for f in tmp_path.glob('dev.db*'))


# ---- (B) active legacy rule superseded by a Correction -----------------------------------------

def test_v7_deprecates_active_legacy_rule_superseded_by_correction(tmp_path):
    p = tmp_path / 'r.db'
    build_v6(p, [
        ('rA', 'ProposedRule', 'a', 'stated',
         '{"status": "active", "description": "x", "promoted_by": "dec1", "promoted_at": 1.0}', 1.0, None),
        ('rD', 'ProposedRule', 'a', 'stated',
         '{"status": "active", "description": "y", "promoted_by": "dec1", "promoted_at": 1.0}', 1.0, None),
        ('cA', 'Correction', 'a', 'stated', '{"text": "fix"}', 2.0, 'rA'),
    ])
    s = Substrate(p, ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        a = s.get('rA', u).payload
        assert a['status'] == 'deprecated' and 'promoted_by' not in a
        assert s.get('rD', u).payload['status'] == 'proposed'  # uncorrected active rule: re-approvable
        fresh = fresh_store(tmp_path, 'f.db')
        try:
            assert fresh.import_scope(s.export_scope('a', u), u, strict=True) == 3
        finally:
            fresh.close()
    finally:
        s.close()


# ---- (C) quarantine dependents -----------------------------------------------------------------

def quarantine_with_correction(tmp_path):
    p = tmp_path / 'q.db'
    build_v6(p, [('bad-orig', 'Source', 'a', 'stated', '{"text": 5, "s": "QSECRET"}', 1.0, None),
                 ('good-fix', 'Correction', 'a', 'stated', '{"text": "fix"}', 2.0, 'bad-orig')])
    return p


def test_migration_does_not_leave_correction_dangling(tmp_path):
    s = Substrate(quarantine_with_correction(tmp_path), ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        assert s.get('good-fix', u).supersedes is None
        data = s.export_scope('a', u)
        assert data['records'][0]['lineage_erased'] is True
        fresh = fresh_store(tmp_path, 'f.db')
        try:
            assert fresh.import_scope(data, u, strict=True) == 1
        finally:
            fresh.close()
    finally:
        s.close()


def test_erasing_quarantined_row_repairs_dependents(tmp_path):
    s = Substrate(quarantine_with_correction(tmp_path), ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        # re-create a dangling dependent as an older store could have left it
        s._conn.execute("UPDATE records SET supersedes='bad-orig' WHERE record_id='good-fix'")
        s._conn.execute("DELETE FROM lineage_erased")
        assert s.delete('bad-orig', u) is True
        assert s.get('good-fix', u).supersedes is None
        data = s.export_scope('a', u)
        assert data['records'][0]['lineage_erased'] is True
        assert 'bad-orig' not in repr(data)
        fresh = fresh_store(tmp_path, 'f.db')
        try:
            assert fresh.import_scope(data, u, strict=True) == 1
        finally:
            fresh.close()
    finally:
        s.close()


def test_purge_quarantined_repairs_dependents(tmp_path):
    s = Substrate(quarantine_with_correction(tmp_path), ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        s._conn.execute("UPDATE records SET supersedes='bad-orig' WHERE record_id='good-fix'")
        s._conn.execute("DELETE FROM lineage_erased")
        assert s.purge_quarantined(u) == 1
        assert s.get('good-fix', u).supersedes is None
        assert s.export_scope('a', u)['records'][0]['lineage_erased'] is True
    finally:
        s.close()


# ---- (D) deletion residue ----------------------------------------------------------------------

SENT = 'quuxplurbleunique'
NEEDLES = (b'quuxplurble', b'QUUXPLURBLE')


def raw_bytes(db):
    out = b''
    for suffix in ('', '-wal'):
        f = Path(str(db) + suffix)
        if f.exists():
            out += f.read_bytes()
    return out


def fts_shadow_text(s):
    out = []
    for (name,) in s._conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'fts_%'").fetchall():
        for row in s._conn.execute(f'SELECT * FROM "{name}"').fetchall():
            out.append(repr(row))
    return ''.join(out).lower().encode()


def assert_clean(s, db):
    blob = raw_bytes(db).lower()
    for n in NEEDLES:
        assert n.lower() not in blob
        assert n.lower() not in fts_shadow_text(s)


def seed_filler(s, u):
    for i in range(5):
        s.put(rec(f'filler{i}', payload={'text': f'filler words number {i} harbor'}), u)


def test_secure_delete_enabled_on_open(sub):
    assert sub._conn.execute('PRAGMA secure_delete').fetchone()[0] == 1


def test_delete_leaves_no_plaintext_in_db_wal_or_fts(tmp_path):
    db = tmp_path / 'd.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        seed_filler(s, u)
        s.put(rec('doc1', payload={'text': f'{SENT} zorptangleworth'}), u)
        assert any(n in raw_bytes(db).lower() for n in (b'quuxplurble',))  # sanity: present before
        assert s.delete('doc1', u) is True
        assert_clean(s, db)
    finally:
        s.close()


def test_prune_derived_leaves_no_plaintext(tmp_path):
    import time
    db = tmp_path / 'p.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    try:
        u = USER('a')
        seed_filler(s, u)
        s.put(rec('src1'), u)
        s.put(Record(record_id='d1', record_type='Claim', scope='a', attribution='agent',
                     source_ids=['src1'], epistemic_status='derived',
                     payload={'text': f'{SENT} derived'}), u)
        s._conn.execute("UPDATE records SET created_at=? WHERE record_id='d1'", (time.time() - 86400,))
        assert s.prune_derived('a', time.time() - 3600, u) == 1
        assert_clean(s, db)
    finally:
        s.close()


def test_quarantine_purge_leaves_no_plaintext(tmp_path):
    db = tmp_path / 'qp.db'
    build_v6(db, [('bad1', 'Source', 'a', 'stated', '{"text": 5, "s": "%s"}' % SENT, 1.0, None),
                  ('ok1', 'Source', 'a', 'stated', '{"text": "filler harbor"}', 1.0, None)])
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    try:
        assert s.purge_quarantined(USER('a')) == 1
        assert_clean(s, db)
    finally:
        s.close()


def test_cascaded_delete_scrubs_once(sub, monkeypatch):
    u = USER('a')
    sub.put(rec('o1', payload={'text': 'root'}), u)
    for i in range(3):
        sub._write(Record(record_id=f'ob{i}', record_type='Claim', scope='a', attribution='agent',
                          source_ids=['o1'], epistemic_status='derived', payload={'text': f'obs {i}'}),
                   u, validate_schema=True, overwrite=False)
    calls = []
    real = sub._scrub
    monkeypatch.setattr(sub, '_scrub', lambda: (calls.append(1), real())[1])
    assert sub.delete('o1', u) is True
    assert len(calls) == 1
