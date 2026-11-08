import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, ScopeViolation, Substrate


def test_unjournaled_denials_do_not_claim_audit(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    try:
        with pytest.raises(ScopeViolation) as error:
            sub.register_verifier('actor', None)
        assert error.value.audit_recorded is False
        assert sub._conn.execute('SELECT count(*) FROM journal').fetchone()[0] == 0
    finally:
        sub.close()


def test_unrelated_buffered_audit_does_not_mark_another_denial(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    try:
        with pytest.raises(ScopeViolation) as error:
            with sub.ingestion_transaction():
                try:
                    sub.put(Record(record_id='r', record_type='Source', scope='coast',
                                   payload={'text': 'x'}, attribution='document'), ScopeContext())
                except ScopeViolation:
                    pass
                sub.register_verifier('actor', None)
        assert error.value.audit_recorded is False
        assert sub._conn.execute("SELECT count(*) FROM journal WHERE op='write_denied'").fetchone()[0] == 1
    finally:
        sub.close()


def test_nested_audit_is_not_durable_before_outer_commit(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    record = Record(record_id='r', record_type='Source', scope='coast', payload={'text': 'x'}, attribution='document')
    try:
        sub._conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ScopeViolation) as error:
            sub.put(record, ScopeContext())
        assert error.value.audit_recorded is False
        sub._conn.execute('ROLLBACK')
        assert sub._conn.execute('SELECT count(*) FROM journal').fetchone()[0] == 0
    finally:
        sub.close()
