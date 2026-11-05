"""Round-9 substrate repairs (Terra/Sonnet R8), written red first.

(1) the WAL scrub never fails open silently (scrub_pending flag + retry);
(2) deletion redacts the erased id / reason from the journal;
(3) correct()/import check scope before promoter relevance;
(4) quarantined-original erasure cascades lineage like a live delete;
(5) import refuses a bundle holding any active rule before any skip/insert;
(6) a future PRAGMA user_version is refused before any write;
(7) legacy target-less Corrections are marked lineage_erased by v7;
(8) a missing fts_rowmap hint falls back to a scan on delete;
(9) a lineage veto is visible to the user-authored caller;
(10) bulk deletes can share one scrub (deferred_scrub), single delete cost is bounded.
"""
import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from weft.knowledge import KnowledgeRefusal
from weft.procedural import promote_rule
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, ScopeViolation, Substrate, SubstrateError

from tests.test_phase14_round7_substrate import build_v6

USER = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)
AGENT = lambda *scopes: ScopeContext(granted_scopes=frozenset(scopes), user_authored=False)

SENT = 'quuxplurbleunique'


def rec(id, typ='Source', scope='a', **kw):
    return Record(record_id=id, record_type=typ, scope=scope,
                  payload=kw.pop('payload', {'text': 'content ' + id}),
                  attribution=kw.pop('attribution', 'user'), **kw)


def new_store(tmp_path, name='s.db', **kw):
    return Substrate(tmp_path / name, ScopeEngine(known_scopes={'a', 'b'}), **kw)


@pytest.fixture
def sub(tmp_path):
    s = new_store(tmp_path)
    yield s
    s.close()


def raw_bytes(db):
    out = b''
    for suffix in ('', '-wal'):
        f = Path(str(db) + suffix)
        if f.exists():
            out += f.read_bytes()
    return out


def wal_bytes(db):
    f = Path(str(db) + '-wal')
    return f.read_bytes() if f.exists() else b''


def seed_filler(s, u):
    for i in range(5):
        s.put(rec(f'filler{i}', payload={'text': f'filler words number {i} harbor'}), u)


def meta(s, key):
    row = s._conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def hold_reader(db):
    reader = sqlite3.connect(str(db), isolation_level=None)
    reader.execute('BEGIN')
    reader.execute('SELECT count(*) FROM records').fetchone()
    return reader


# ---- (1) WAL scrub does not fail open --------------------------------------------------------

def test_delete_with_reader_sets_pending_then_complete_scrub_cleans(tmp_path, caplog):
    db = tmp_path / 'w.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.2)
    u = USER('a')
    try:
        seed_filler(s, u)
        s.put(rec('doc1', payload={'text': f'{SENT} zorptangleworth'}), u)
        reader = hold_reader(db)
        with caplog.at_level(logging.WARNING, logger='weft.substrate'):
            assert s.delete('doc1', u) is True
        assert s.last_scrub_complete is False
        assert s.scrub_pending() is True
        assert meta(s, 'scrub_pending') is not None
        assert any('scrub' in r.getMessage().lower() for r in caplog.records)
        assert SENT.encode() in wal_bytes(db)  # honest: the WAL still holds it
        assert s.complete_scrub() is False      # reader still there: reports, does not raise
        assert s.scrub_pending() is True
        reader.execute('COMMIT')
        reader.close()
        assert s.complete_scrub() is True
        assert s.scrub_pending() is False and meta(s, 'scrub_pending') is None
        assert SENT.encode() not in raw_bytes(db)
    finally:
        s.close()


def test_next_open_completes_pending_scrub(tmp_path):
    db = tmp_path / 'o.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.1)
    u = USER('a')
    try:
        seed_filler(s, u)
        s.put(rec('doc1', payload={'text': f'{SENT} zorptangle'}), u)
        reader = hold_reader(db)
        assert s.delete('doc1', u) is True
        assert s.scrub_pending() is True
        reader.execute('COMMIT')
        reader.close()
        t = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.5)
        try:
            assert t.scrub_pending() is False
            assert SENT.encode() not in raw_bytes(db)
        finally:
            t.close()
    finally:
        s.close()


def test_next_write_retries_pending_scrub(tmp_path):
    db = tmp_path / 'n.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.1)
    u = USER('a')
    try:
        seed_filler(s, u)
        s.put(rec('doc1', payload={'text': f'{SENT} zorptangle'}), u)
        reader = hold_reader(db)
        assert s.delete('doc1', u) is True
        assert s.scrub_pending() is True
        t0 = time.monotonic()
        s.put(rec('later'), u)  # reader still holding: must not stall for busy_timeout
        assert time.monotonic() - t0 < 2.0
        assert s.scrub_pending() is True
        reader.execute('COMMIT')
        reader.close()
        s.put(rec('later2'), u)
        assert s.scrub_pending() is False
        assert SENT.encode() not in raw_bytes(db)
    finally:
        s.close()


def test_scrub_retry_is_bounded_by_configured_window(tmp_path):
    db = tmp_path / 'b.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.3)
    u = USER('a')
    try:
        s.put(rec('doc1', payload={'text': SENT}), u)
        reader = hold_reader(db)
        t0 = time.monotonic()
        assert s.delete('doc1', u) is True
        elapsed = time.monotonic() - t0
        assert 0.25 <= elapsed < 3.0
        reader.execute('COMMIT')
        reader.close()
    finally:
        s.close()


def test_clean_delete_leaves_no_pending_flag(sub):
    u = USER('a')
    sub.put(rec('d1'), u)
    assert sub.delete('d1', u) is True
    assert sub.last_scrub_complete is True
    assert sub.scrub_pending() is False


def test_prune_and_purge_also_flag_pending(tmp_path):
    db = tmp_path / 'pp.db'
    build_v6(db, [('bad1', 'Source', 'a', 'stated', '{"text": 5, "s": "%s"}' % SENT, 1.0, None),
                  ('ok1', 'Source', 'a', 'stated', '{"text": "filler harbor"}', 1.0, None)])
    s = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.1)
    try:
        reader = hold_reader(db)
        assert s.purge_quarantined(USER('a')) == 1
        assert s.scrub_pending() is True
        reader.execute('COMMIT')
        reader.close()
        assert s.complete_scrub() is True
        assert SENT.encode() not in raw_bytes(db)
    finally:
        s.close()


# ---- (2) journal redaction ---------------------------------------------------------------------

def test_delete_redacts_id_and_reason_from_journal(tmp_path):
    db = tmp_path / 'j.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    rid = f'patient-{SENT}-id'
    try:
        seed_filler(s, u)
        s.put(rec(rid, payload={'text': 'plain body'}), u)
        s.correct(rid, rec('fix1', 'Correction', payload={'text': 'fixed'}), u)
        assert s.delete(rid, u, reason=f'gdpr request from {SENT}-person') is True
        rows = s._conn.execute('SELECT op, record_id, scope, detail FROM journal').fetchall()
        assert not any(SENT in (r[1] or '') or SENT in r[3] for r in rows)
        erased = [r for r in rows if r[1] == '[erased]']
        assert {r[0] for r in erased} >= {'put', 'delete'}
        assert all(r[2] == 'a' for r in erased)  # scope kept for audit
        entries = s.journal_entries(u)
        assert any(e['op'] == 'delete' and e['record_id'] == '[erased]' for e in entries)
    finally:
        s.close()
    assert SENT.encode() not in raw_bytes(db)


def test_cascaded_and_denied_rows_redacted_too(tmp_path):
    db = tmp_path / 'jc.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        s.put(rec('root'), u)
        s._write(Record(record_id=f'obs-{SENT}', record_type='Claim', scope='a', attribution='agent',
                        source_ids=['root'], epistemic_status='derived', payload={'text': 'x'}),
                 u, validate_schema=True, overwrite=False)
        assert s.delete('root', u) is True
        assert s.get(f'obs-{SENT}', u) is None
        assert SENT not in repr(s._conn.execute('SELECT * FROM journal').fetchall())
    finally:
        s.close()
    assert SENT.encode() not in raw_bytes(db)


# ---- (3) scope before promoter relevance ------------------------------------------------------

def _foreign_promoter_store(tmp_path):
    s = new_store(tmp_path, 'f.db')
    ub = USER('b')
    s.put(rec('rb', 'ProposedRule', scope='b', payload={'status': 'proposed', 'description': 'x'},
              attribution='agent'), ub)
    s.put(rec('secret', 'Decision', scope='b', payload={'rule_id': 'rb', 'approved': True}), ub)
    promote_rule(s, ub, 'rb', 'secret')
    assert s.get('rb', ub).payload['status'] == 'active'
    return s


def _refusal(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, str(exc), getattr(exc, 'audit_recorded', None)
    return None


def test_correct_foreign_promoter_refusal_identical_to_missing(tmp_path):
    s = _foreign_promoter_store(tmp_path)
    try:
        ua = USER('a')
        foreign = _refusal(lambda: s.correct('secret', rec('c1', 'Correction'), ua))
        missing = _refusal(lambda: s.correct('no-such-id', rec('c2', 'Correction'), ua))
        assert foreign is not None and foreign == missing
        assert s.get('c1', ua) is None and s.get('secret', USER('b')) is not None
    finally:
        s.close()


def test_import_foreign_promoter_refusal_identical_to_missing(tmp_path):
    s = _foreign_promoter_store(tmp_path)
    try:
        ua = USER('a')

        def bundle(target):
            return {'schema_version': 7, 'scope': 'a', 'records': [{
                'record_id': 'cx', 'record_type': 'Correction', 'scope': 'a', 'epistemic_status': 'stated',
                'payload': {'text': 'x'}, 'supersedes': target, 'attribution': 'user'}]}

        assert s.import_scope(bundle('secret'), ua) == 0
        r1 = s.last_import_refusal
        assert s.import_scope(bundle('no-such-id'), ua) == 0
        assert r1 == s.last_import_refusal
        with pytest.raises(KnowledgeRefusal) as e1:
            s.import_scope(bundle('secret'), ua, strict=True)
        with pytest.raises(KnowledgeRefusal) as e2:
            s.import_scope(bundle('no-such-id'), ua, strict=True)
        assert str(e1.value) == str(e2.value)
    finally:
        s.close()


# ---- (4) quarantined original: lineage cascade --------------------------------------------------

def _insert_derived(s, rid, cites, scope='a'):
    s._conn.execute(
        'INSERT INTO records (record_id, record_type, scope, epistemic_status, payload, created_at, '
        "source_ids, provenance, attribution) VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, 'Observation', scope, 'derived', json.dumps({'text': f'derived {rid} {SENT}'}), 2.0,
         json.dumps(cites), '{}', 'agent'))
    s._fts_index(s._lookup_record(rid))


def test_erasing_quarantined_original_cascades_to_derived(tmp_path):
    db = tmp_path / 'q.db'
    build_v6(db, [('bad', 'Source', 'a', 'stated', '{"text": 5}', 1.0, None),
                  ('good', 'Source', 'a', 'stated', '{"text": "hello"}', 1.0, None)])
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        assert s.list_quarantined(u)[0]['record_id'] == 'bad'
        _insert_derived(s, 'obs', ['bad'])
        assert s.delete('bad', u) is True
        assert s.get('obs', u) is None
        assert s.get('good', u) is not None
        data = s.export_scope('a', u)
        t = Substrate(tmp_path / 'fresh.db', ScopeEngine(known_scopes={'a'}))
        try:
            assert t.import_scope(data, u, strict=True) == 1
        finally:
            t.close()
    finally:
        s.close()


def test_purge_quarantined_cascades_to_derived(tmp_path):
    db = tmp_path / 'qp.db'
    build_v6(db, [('bad', 'Source', 'a', 'stated', '{"text": 5}', 1.0, None)])
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        _insert_derived(s, 'obs', ['bad'])
        assert s.purge_quarantined(u) == 1
        assert s.get('obs', u) is None
    finally:
        s.close()


def test_erasing_quarantined_original_refused_when_retained_record_cites_it(tmp_path):
    db = tmp_path / 'qr.db'
    build_v6(db, [('bad', 'Source', 'a', 'stated', '{"text": 5}', 1.0, None)])
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        s._conn.execute(
            'INSERT INTO records (record_id, record_type, scope, epistemic_status, payload, created_at, '
            "source_ids, provenance, attribution) VALUES ('keep','Source','a','stated','{\"text\":\"k\"}',2.0,"
            "'[\"bad\"]','{}','user')")
        assert s.delete('bad', u) is False
        assert s.list_quarantined(u)
    finally:
        s.close()


def test_v7_migration_cascades_derived_citing_quarantined_source(tmp_path):
    db = tmp_path / 'm.db'
    build_v6(db, [('bad', 'Source', 'a', 'stated', '{"text": 5}', 1.0, None),
                  ('obs', 'Observation', 'a', 'derived', '{"text": "derived obs"}', 2.0, None),
                  ('good', 'Source', 'a', 'stated', '{"text": "hello"}', 1.0, None)])
    c = sqlite3.connect(db)
    c.execute("UPDATE records SET source_ids='[\"bad\"]' WHERE record_id='obs'")
    c.commit()
    c.close()
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        assert 'bad' in s.migration_quarantined
        assert s.get('obs', u) is None  # no dangling citation survives the migration
        assert s.get('good', u) is not None
        t = Substrate(tmp_path / 'fresh2.db', ScopeEngine(known_scopes={'a'}))
        try:
            assert t.import_scope(s.export_scope('a', u), u, strict=True) == 1
        finally:
            t.close()
    finally:
        s.close()


# ---- (5) active-rule bundle refused atomically -------------------------------------------------

def test_bundle_with_identical_existing_active_rule_is_refused(tmp_path):
    s = new_store(tmp_path)
    u = USER('a')
    try:
        s.put(rec('r', 'ProposedRule', payload={'status': 'proposed', 'description': 'x'},
                  attribution='agent'), u)
        s.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': True}), u)
        promote_rule(s, u, 'r', 'd')
        data = s.export_scope('a', u)
        assert any(r['payload'].get('status') == 'active' for r in data['records'])
        data['records'].append(
            {'record_id': 'z', 'record_type': 'Source', 'scope': 'a', 'epistemic_status': 'stated',
             'payload': {'text': 'zed'}, 'attribution': 'user'})
        with pytest.raises(KnowledgeRefusal):
            s.import_scope(data, u, strict=True)
        assert s.import_scope(data, u) == 0
        assert 'active' in s.last_import_refusal
        assert s.get('z', u) is None
        assert s.get('r', u).payload['status'] == 'active'  # untouched
    finally:
        s.close()


# ---- (6) future schema version -----------------------------------------------------------------

def test_future_user_version_refused_before_any_write(tmp_path):
    db = tmp_path / 'fut.db'
    c = sqlite3.connect(db)
    c.execute('PRAGMA user_version = 999')
    c.commit()
    c.close()
    with pytest.raises(SubstrateError, match='newer|future|unsupported'):
        Substrate(db, ScopeEngine(known_scopes={'a'}))
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    assert c.execute('PRAGMA user_version').fetchone()[0] == 999
    c.close()


def test_future_version_on_existing_store_refused(tmp_path):
    db = tmp_path / 'fut2.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    s.put(rec('x'), USER('a'))
    s.close()
    c = sqlite3.connect(db)
    c.execute('PRAGMA user_version = 999')
    c.commit()
    c.close()
    with pytest.raises(SubstrateError):
        Substrate(db, ScopeEngine(known_scopes={'a'}))
    c = sqlite3.connect(db)
    assert c.execute('PRAGMA user_version').fetchone()[0] == 999
    assert c.execute('SELECT count(*) FROM records').fetchone()[0] == 1
    c.close()


# ---- (7) legacy target-less Corrections --------------------------------------------------------

def test_v6_targetless_correction_marked_and_restores(tmp_path):
    db = tmp_path / 'v6c.db'
    build_v6(db, [('fix1', 'Correction', 'a', 'stated', '{"text": "the fix"}', 1.0, None),
                  ('keep', 'Source', 'a', 'stated', '{"text": "kept"}', 1.0, None)])
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        data = s.export_scope('a', u)
        fix = [r for r in data['records'] if r['record_id'] == 'fix1'][0]
        assert fix['supersedes'] is None and fix['lineage_erased'] is True
        t = Substrate(tmp_path / 'fresh3.db', ScopeEngine(known_scopes={'a'}))
        try:
            assert t.import_scope(data, u, strict=True) == 2
        finally:
            t.close()
    finally:
        s.close()


def test_open_repairs_unmarked_targetless_correction_in_current_store(tmp_path):
    db = tmp_path / 'cur.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    s.put(rec('keep'), USER('a'))
    s._conn.execute(
        'INSERT INTO records (record_id, record_type, scope, epistemic_status, payload, created_at, '
        "source_ids, provenance, attribution) VALUES ('fixx','Correction','a','stated','{\"text\":\"f\"}',"
        "1.0,'[]','{}','user')")
    s.close()
    t = Substrate(db, ScopeEngine(known_scopes={'a'}))
    try:
        assert t.export_scope('a', USER('a'))['records'][0].get('lineage_erased') is True or any(
            r['record_id'] == 'fixx' and r.get('lineage_erased') is True
            for r in t.export_scope('a', USER('a'))['records'])
    finally:
        t.close()


# ---- (8) missing fts_rowmap hint -------------------------------------------------------------

def test_delete_without_rowmap_hint_still_erases_fts_row(tmp_path):
    db = tmp_path / 'h.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        seed_filler(s, u)
        s.put(rec('legacyw', payload={'text': f'{SENT} secret words'}), u)
        s._conn.execute("DELETE FROM fts_rowmap WHERE record_id='legacyw'")
        assert s.delete('legacyw', u) is True
        assert s._conn.execute("SELECT count(*) FROM fts WHERE record_id='legacyw'").fetchone()[0] == 0
        assert s.last_scrub_complete is True
        assert s.fts_index_complete('a', u)
    finally:
        s.close()
    assert SENT.encode() not in raw_bytes(db)


def test_scrub_not_complete_if_fts_row_remains(sub, monkeypatch):
    u = USER('a')
    sub.put(rec('d1', payload={'text': SENT}), u)
    monkeypatch.setattr(sub, '_fts_ids_remaining', lambda ids: list(ids))
    assert sub.delete('d1', u) is True
    assert sub.last_scrub_complete is not True
    assert sub.scrub_pending() is True


# ---- (9) visible, generic refusal --------------------------------------------------------------

def test_lineage_veto_is_visible_and_not_weakened(sub):
    user, agent = USER('a'), AGENT('a')
    sub.put(rec('victim'), user)
    sub.put(rec('pin', attribution='agent', source_ids=['victim']), agent)
    assert sub.delete('victim', user) is False
    assert sub.get('victim', user) is not None  # veto intact
    assert sub.last_refusal and 'cite' in sub.last_refusal.lower()
    denied = [e for e in sub.journal_entries(user) if e['op'] == 'delete_denied']
    assert denied and denied[-1]['scope'] == 'a'
    assert not [e for e in sub.journal_entries(USER('b')) if e['op'] == 'delete_denied']
    # after the citing record goes, the delete works and last_refusal resets
    assert sub.delete('pin', agent) is True
    assert sub.delete('victim', user) is True
    assert sub.last_refusal is None


def test_foreign_and_missing_delete_refusal_identical(sub):
    sub.put(rec('fb', scope='b'), USER('b'))
    ua = USER('a')
    assert sub.delete('fb', ua) is False
    foreign = sub.last_refusal
    assert sub.delete('nothing-here', ua) is False
    assert sub.last_refusal == foreign and foreign
    assert 'fb' not in foreign
    assert not [e for e in sub.journal_entries(ua) if e['op'] == 'delete_denied']


# ---- (10) deferred scrub and bounded cost ------------------------------------------------------

def _trace(s):
    stmts = []
    s._conn.set_trace_callback(stmts.append)
    return stmts


def test_deferred_scrub_runs_once_and_matches_individual_deletes(tmp_path):
    db1, db2 = tmp_path / 'one.db', tmp_path / 'two.db'
    results = []
    for db, deferred in ((db1, False), (db2, True)):
        s = Substrate(db, ScopeEngine(known_scopes={'a'}))
        u = USER('a')
        try:
            seed_filler(s, u)
            for i in range(4):
                s.put(rec(f'bulk{i}', payload={'text': f'{SENT} bulk {i}'}), u)
            stmts = _trace(s)
            if deferred:
                with s.deferred_scrub():
                    for i in range(4):
                        assert s.delete(f'bulk{i}', u) is True
                    assert s.scrub_pending() is True
                assert sum('wal_checkpoint' in x.lower() for x in stmts) == 1
                assert sum("'optimize'" in x for x in stmts) == 1
                assert s.scrub_pending() is False
            else:
                for i in range(4):
                    assert s.delete(f'bulk{i}', u) is True
            results.append(sorted(r.record_id for r in s.all_records(u)))
            assert s.last_scrub_complete is True
        finally:
            s.close()
        assert SENT.encode() not in raw_bytes(db)
    assert results[0] == results[1]


def test_nested_deferred_scrub_runs_at_outermost_exit(sub):
    u = USER('a')
    for i in range(3):
        sub.put(rec(f'n{i}', payload={'text': SENT}), u)
    stmts = _trace(sub)
    with sub.deferred_scrub():
        sub.delete('n0', u)
        with sub.deferred_scrub():
            sub.delete('n1', u)
        assert not any('wal_checkpoint' in x.lower() for x in stmts)
        sub.delete('n2', u)
    assert sum('wal_checkpoint' in x.lower() for x in stmts) == 1


def test_delete_touching_no_fts_rows_skips_optimize(tmp_path):
    db = tmp_path / 'sk.db'
    build_v6(db, [('bad1', 'Source', 'a', 'stated', '{"text": 5, "s": "%s"}' % SENT, 1.0, None)])
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    try:
        stmts = _trace(s)
        assert s.purge_quarantined(USER('a')) == 1
        assert not any("'optimize'" in x for x in stmts)
        assert any('wal_checkpoint' in x.lower() for x in stmts)
    finally:
        s.close()
    assert SENT.encode() not in raw_bytes(db)


def test_single_delete_with_fts_rows_still_optimizes(sub):
    u = USER('a')
    sub.put(rec('d1', payload={'text': SENT}), u)
    stmts = _trace(sub)
    assert sub.delete('d1', u) is True
    assert sum("'optimize'" in x for x in stmts) == 1


# ---- Round 10 (Terra R9-01, R9-03, R9-04, R9-05) ------------------------------------------------

def test_r9_01_interruption_between_commit_and_scrub_leaves_durable_pending_flag(tmp_path, monkeypatch):
    db = tmp_path / 'r9_01.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        seed_filler(s, u)
        s.put(rec('doc1', payload={'text': f'{SENT} crash_window_marker'}), u)

        def boom():
            raise RuntimeError('simulated crash right after commit')

        monkeypatch.setattr(s, '_scrub', boom)
        with pytest.raises(RuntimeError, match='simulated crash'):
            s.delete('doc1', u)
        assert s.get('doc1', u) is None
        assert s.scrub_pending() is True
    finally:
        s.close()
    # Second Substrate opening (or complete_scrub) discovers the durable flag and scrubs the WAL/FTS
    t = Substrate(db, ScopeEngine(known_scopes={'a'}))
    try:
        assert t.scrub_pending() is False
        assert t.complete_scrub() is True
    finally:
        t.close()
    assert SENT.encode() not in raw_bytes(db)


def test_r9_03_deferred_scrub_id_reuse_preserves_new_fts_row(tmp_path):
    db = tmp_path / 'r9_03.db'
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        seed_filler(s, u)
        s.put(rec('same', payload={'text': f'{SENT} old_incarnation'}), u)
        with s.deferred_scrub():
            assert s.delete('same', u) is True
            s.put(rec('same', payload={'text': 'brand_new_incarnation_text'}), u)
        got = s.get('same', u)
        assert got is not None and got.payload['text'] == 'brand_new_incarnation_text'
        assert s.last_scrub_complete is True
        assert s.fts_index_complete('a', u) is True
        rows = s._conn.execute("SELECT text FROM fts WHERE record_id='same'").fetchall()
        assert len(rows) == 1 and 'brand_new_incarnation_text' in rows[0][0]
    finally:
        s.close()
    assert SENT.encode() not in raw_bytes(db)


def test_r9_04_second_open_substrate_discovers_and_completes_scrub_on_write(tmp_path):
    db = tmp_path / 'r9_04.db'
    sa = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.1)
    sb = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.1)
    u = USER('a')
    try:
        seed_filler(sa, u)
        sa.put(rec('doc1', payload={'text': f'{SENT} multi_conn_marker'}), u)
        reader = hold_reader(db)
        assert sa.delete('doc1', u) is True
        assert sa.scrub_pending() is True
        reader.execute('COMMIT')
        reader.close()
        # Write through already-open instance B; B must discover meta.scrub_pending and complete it
        sb.put(rec('from_b', payload={'text': 'written by b'}), u)
        assert sa.scrub_pending() is False
        assert sb.scrub_pending() is False
        assert SENT.encode() not in raw_bytes(db)
    finally:
        sa.close()
        sb.close()


def test_r9_05_v7_quarantine_migration_strips_dangling_citation_from_retained_record(tmp_path):
    db = tmp_path / 'r9_05.db'
    build_v6(db, [('bad', 'Source', 'a', 'stated', '{"text": 5}', 1.0, None),
                  ('keep', 'Source', 'a', 'stated', '{"text": "retained stated source"}', 2.0, None)])
    c = sqlite3.connect(db)
    c.execute("UPDATE records SET source_ids='[\"bad\"]' WHERE record_id='keep'")
    c.commit()
    c.close()
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        assert 'bad' in s.migration_quarantined
        kept = s.get('keep', u)
        assert kept is not None
        assert 'bad' not in kept.source_ids
        data = s.export_scope('a', u)
        t = Substrate(tmp_path / 'r9_05_fresh.db', ScopeEngine(known_scopes={'a'}))
        try:
            assert t.import_scope(data, u, strict=True) == 1
        finally:
            t.close()
        assert s.delete('bad', u) is True
    finally:
        s.close()


# ---- Round 11 (Terra R10-01, R10-02, R10-03) -----------------------------------------------------

def test_r10_01_concurrent_scrub_clear_does_not_drop_newer_erase_flag(tmp_path, monkeypatch):
    db = tmp_path / 'r10_01.db'
    sa = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.05)
    sb = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.0)
    u = USER('a')
    try:
        seed_filler(sa, u)
        sa.put(rec('d1', payload={'text': 'first_erase'}), u)
        sb.put(rec('d2', payload={'text': f'{SENT} second_erase_marker'}), u)
        r1 = hold_reader(db)
        assert sa.delete('d1', u) is True
        assert sa.scrub_pending() is True
        r1.execute('COMMIT')
        r1.close()

        orig_attempts = sa._scrub_attempts
        interleaved = {'done': False, 'reader': None}

        def hooked_attempts(window):
            ok = orig_attempts(window)
            if ok and not interleaved['done']:
                interleaved['done'] = True
                # Before sa clears scrub_pending, sb commits a new erase while a reader blocks sb's scrub
                r2 = hold_reader(db)
                assert sb.delete('d2', u) is True
                assert sb.last_scrub_complete is False
                interleaved['reader'] = r2
            return ok

        monkeypatch.setattr(sa, '_scrub_attempts', hooked_attempts)
        sa.complete_scrub()
        # Even though sa's checkpoint succeeded before sb's erase, sb's newer erase must keep scrub_pending True!
        assert sb.scrub_pending() is True
        r2 = interleaved['reader']
        assert r2 is not None
        r2.execute('COMMIT')
        r2.close()
        assert sb.complete_scrub() is True
        assert sb.scrub_pending() is False
    finally:
        sa.close()
        sb.close()
    assert SENT.encode() not in raw_bytes(db)


def test_r10_02_cross_connection_id_reuse_during_deferred_scrub_preserves_fts(tmp_path):
    db = tmp_path / 'r10_02.db'
    sa = Substrate(db, ScopeEngine(known_scopes={'a'}))
    sb = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        seed_filler(sa, u)
        sa.put(rec('same', payload={'text': f'{SENT} old_conn_a'}), u)
        with sa.deferred_scrub():
            assert sa.delete('same', u) is True
            sb.put(rec('same', payload={'text': 'recreated_by_conn_b'}), u)
        got = sa.get('same', u)
        assert got is not None and got.payload['text'] == 'recreated_by_conn_b'
        assert sa.fts_index_complete('a', u) is True
        assert sa.last_scrub_complete is True
    finally:
        sa.close()
        sb.close()
    assert SENT.encode() not in raw_bytes(db)


def test_r10_03_v7_quarantine_migration_transitive_citation_repair(tmp_path):
    db = tmp_path / 'r10_03.db'
    build_v6(db, [
        ('bad', 'Source', 'a', 'stated', '{"text": 5}', 1.0, None),
        ('claim', 'Claim', 'a', 'verified', '{"text": "verified claim on bad"}', 2.0, None),
        ('keep', 'Source', 'a', 'stated', '{"text": "stated citing claim"}', 3.0, None),
    ])
    c = sqlite3.connect(db)
    c.execute("UPDATE records SET source_ids='[\"bad\"]' WHERE record_id='claim'")
    c.execute("UPDATE records SET source_ids='[\"claim\"]' WHERE record_id='keep'")
    c.commit()
    c.close()
    s = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        assert 'bad' in s.migration_quarantined
        assert s.get('claim', u) is None
        kept = s.get('keep', u)
        assert kept is not None and kept.source_ids == []
        data = s.export_scope('a', u)
        t = Substrate(tmp_path / 'r10_03_fresh.db', ScopeEngine(known_scopes={'a'}))
        try:
            assert t.import_scope(data, u, strict=True) == 1
        finally:
            t.close()
    finally:
        s.close()


# ---- Round 12 (Terra R11-01, R11-02) -------------------------------------------------------------

def test_r11_01_scrub_token_never_reuses_value_after_completed_scrub(tmp_path, monkeypatch):
    db = tmp_path / 'r11_01.db'
    sa = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.05)
    sb = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.0)
    u = USER('a')
    try:
        seed_filler(sa, u)
        sa.put(rec('d1', payload={'text': 'first_erase'}), u)
        sb.put(rec('d2', payload={'text': f'{SENT} second_erase_marker'}), u)
        r1 = hold_reader(db)
        assert sa.delete('d1', u) is True
        assert sa.scrub_pending() is True
        r1.execute('COMMIT')
        r1.close()

        orig_attempts = sa._scrub_attempts
        interleaved = {'done': False, 'reader': None}

        def hooked_attempts(window):
            ok = orig_attempts(window)
            if ok and not interleaved['done']:
                interleaved['done'] = True
                # While sa is paused between checkpoint and compare-and-delete:
                # 1) sb completes the same pending scrub (removing token 1 in the broken code)
                assert sb.complete_scrub() is True
                # 2) a new reader blocks sb's second delete, which creates a fresh pending scrub
                r2 = hold_reader(db)
                assert sb.delete('d2', u) is True
                assert sb.last_scrub_complete is False
                interleaved['reader'] = r2
            return ok

        monkeypatch.setattr(sa, '_scrub_attempts', hooked_attempts)
        sa.complete_scrub()
        # sa's stale compare-and-delete must NOT clear sb's second erase token!
        assert sb.scrub_pending() is True
        r2 = interleaved['reader']
        assert r2 is not None
        r2.execute('COMMIT')
        r2.close()
        assert sb.complete_scrub() is True
        assert sb.scrub_pending() is False
    finally:
        sa.close()
        sb.close()
    assert SENT.encode() not in raw_bytes(db)


def test_r11_02_record_receipt_and_register_verifier_retry_pending_scrub(tmp_path):
    db = tmp_path / 'r11_02.db'
    sa = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.05)
    sb = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.05)
    u = USER('a')
    try:
        seed_filler(sa, u)
        sa.put(rec('d1', payload={'text': f'{SENT} receipt_retry_marker'}), u)
        r1 = hold_reader(db)
        assert sa.delete('d1', u) is True
        assert sa.scrub_pending() is True
        r1.execute('COMMIT')
        r1.close()
        sb.record_receipt('test', ['a'], {'n': 1}, u)
        assert sb.scrub_pending() is False
        assert sa.scrub_pending() is False
    finally:
        sa.close()
        sb.close()
    assert SENT.encode() not in raw_bytes(db)


# ---- Round 13 (Terra R12-01, R12-02) -------------------------------------------------------------

def test_r12_01_auto_retry_on_second_connection_runs_fts_optimize_after_interrupted_erase(tmp_path):
    db = tmp_path / 'r12_01.db'
    sa = Substrate(db, ScopeEngine(known_scopes={'a'}))
    sb = Substrate(db, ScopeEngine(known_scopes={'a'}))
    u = USER('a')
    try:
        sa.put(rec('d1', payload={'text': f'{SENT} fts_shadow_residue'}), u)
        seed_filler(sa, u)
        # Simulate interruption immediately after _delete() commits and before delete() calls _scrub()
        assert sa._delete('d1', u, reason='test') is True
        assert sb.scrub_pending() is True
        # Second connection triggers _auto_retry_scrub() via record_receipt()
        sb.record_receipt('test', ['a'], {'n': 1}, u)
        assert sb.scrub_pending() is False
        fts_blobs = b"".join(bytes(r[0]) for r in sb._conn.execute("SELECT block FROM fts_data").fetchall() if r[0])
        assert b'quuxplurbleuniqu' not in fts_blobs
        assert b'quuxplurbleuniqu' not in raw_bytes(db)
    finally:
        sa.close()
        sb.close()


def test_r12_02_delete_and_purge_quarantined_call_auto_retry_scrub(tmp_path):
    db = tmp_path / 'r12_02.db'
    sa = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.05)
    sb = Substrate(db, ScopeEngine(known_scopes={'a'}), scrub_retry_seconds=0.05)
    u = USER('a')
    try:
        seed_filler(sa, u)
        sa.put(rec('d1', payload={'text': f'{SENT} delete_retry_marker'}), u)
        r1 = hold_reader(db)
        assert sa.delete('d1', u) is True
        assert sa.scrub_pending() is True
        r1.execute('COMMIT')
        r1.close()
        # Denied delete on missing record via B still starts a write transaction and must retry the pending scrub
        assert sb.delete('nonexistent-id', u) is False
        assert sb.scrub_pending() is False
        assert sa.scrub_pending() is False
    finally:
        sa.close()
        sb.close()
    assert SENT.encode() not in raw_bytes(db)




