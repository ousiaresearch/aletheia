import sqlite3

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, ScopeViolation, Substrate


def test_ingest_denial_audit_survives_rollback(tmp_path):
    sub = Substrate(tmp_path / 'store.db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}), user_authored=False)
    try:
        with pytest.raises(ScopeViolation) as error:
            Ingestor(sub).ingest_turn('hello', 'user', ctx, 'coast', 'c1')
        assert error.value.audit_recorded is True
        assert sub._conn.execute("SELECT COUNT(*) FROM journal WHERE op='write_denied'").fetchone()[0] == 1
        assert sub._conn.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 0
    finally:
        sub.close()


def test_denial_reports_failed_audit_when_journal_rejects_it(tmp_path):
    sub = Substrate(tmp_path / 'store.db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}), user_authored=False)
    try:
        sub._conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON journal WHEN NEW.op='write_denied' BEGIN SELECT RAISE(ABORT, 'audit rejected'); END")
        with pytest.raises(ScopeViolation) as error:
            Ingestor(sub).ingest_turn('hello', 'user', ctx, 'coast', 'c1')
        assert error.value.audit_recorded is False
        assert sub._conn.execute("SELECT COUNT(*) FROM journal WHERE op='write_denied'").fetchone()[0] == 0
    finally:
        sub.close()


def test_standalone_write_reserves_writer_before_read(tmp_path):
    sub = Substrate(tmp_path / 'store.db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    sql = []
    sub._conn.set_trace_callback(sql.append)
    try:
        sub.put(Record(record_id='r1', record_type='Source', scope='coast',
                       payload={'text': 'hello'}, attribution='document', source_ids=[], provenance={}), ctx)
        assert 'BEGIN IMMEDIATE' in sql
        assert sql.index('BEGIN IMMEDIATE') < next(i for i, s in enumerate(sql) if 'SELECT scope, record_type' in s)
    finally:
        sub.close()


def test_turn_inference_does_not_hold_writer_lock(tmp_path):
    path = tmp_path / 'store.db'
    sub = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}), user_authored=True)
    class Embedder:
        model_id = 'test'
        def embed_document(self, text):
            assert not sub._conn.in_transaction
            with sqlite3.connect(path, timeout=0) as other:
                other.execute('BEGIN IMMEDIATE')
            return [0.0] * 1024
    try:
        Ingestor(sub, embedder=Embedder()).ingest_turn('hello', 'user', ctx, 'coast', 'c1')
    finally:
        sub.close()


def test_rollback_trigger_cannot_allow_later_autocommit(tmp_path):
    sub = Substrate(tmp_path / 'store.db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    def record(rid):
        return Record(record_id=rid, record_type='Source', scope='coast',
                      payload={'text': rid}, attribution='document', source_ids=[], provenance={})
    try:
        sub._conn.execute("CREATE TRIGGER rollback_insert BEFORE INSERT ON records WHEN NEW.record_id='bad' BEGIN SELECT RAISE(ROLLBACK, 'rollback injected'); END")
        with pytest.raises(Exception):
            with sub.ingestion_transaction():
                sub.put(record('first'), ctx)
                try:
                    sub.put(record('bad'), ctx)
                except sqlite3.IntegrityError:
                    pass
                sub.put(record('later'), ctx)
        assert sub._conn.execute('SELECT record_id FROM records').fetchall() == []
    finally:
        sub.close()
