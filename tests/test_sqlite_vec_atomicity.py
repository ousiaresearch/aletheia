import sqlite3

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


class Embedder:
    model_id = 'transaction-probe'
    def embed_document(self, text):
        return [1.0] + [0.0] * 1023


def test_sqlite_vec_first_table_and_row_roll_back_with_document(tmp_path):
    path = tmp_path / 'db'
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    sub = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    table = sub._vec_table('coast', 'transaction-probe')
    try:
        sub._conn.execute("CREATE TRIGGER fail_second BEFORE INSERT ON records WHEN NEW.record_type='Observation' AND (SELECT COUNT(*) FROM records WHERE record_type='Observation')=1 BEGIN SELECT RAISE(ABORT, 'second observation failed'); END")
        version = sub._store_version()
        with pytest.raises(sqlite3.IntegrityError, match='second observation failed'):
            Ingestor(sub, Embedder()).ingest_document('a.md', b'first\n\nsecond', ctx, 'coast', 'operator')
        with sqlite3.connect(path) as verify:
            for name in ('records', 'fts', 'jobs', 'journal'):
                assert verify.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] == 0
            assert verify.execute('SELECT COUNT(*) FROM sqlite_master WHERE name=?', (table,)).fetchone()[0] == 0
        assert sub._store_version() == version
        sub._conn.execute('DROP TRIGGER fail_second')
        sid = Ingestor(sub, Embedder()).ingest_document('a.md', b'first\n\nsecond', ctx, 'coast', 'operator')
        assert sub.vector_has_record(sid, 'coast', ctx, model_id='transaction-probe')
        assert sub.vector_has_record(sid + '-p0000', 'coast', ctx, model_id='transaction-probe')
        assert sub.vector_has_record(sid + '-p0001', 'coast', ctx, model_id='transaction-probe')
    finally:
        sub.close()


def test_existing_vector_replacements_roll_back_when_completion_write_fails(tmp_path):
    path = tmp_path / 'db'
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    sub = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    data = b'first\n\nsecond'
    class DifferentEmbedder:
        model_id = 'transaction-probe'
        def embed_document(self, text):
            return [0.0, 1.0] + [0.0] * 1022
    try:
        sid = Ingestor(sub, Embedder()).ingest_document('a.md', data, ctx, 'coast', 'operator')
        table = sub._vec_table('coast', 'transaction-probe')
        ids = (sid, sid + '-p0000', sid + '-p0001')
        def vectors():
            return {rid: sub._conn.execute(f'SELECT embedding FROM {table} WHERE record_id=?', (rid,)).fetchone()[0] for rid in ids}
        before = vectors()
        version = sub._store_version()
        sub._conn.execute("CREATE TRIGGER fail_done BEFORE UPDATE ON jobs WHEN NEW.state='done' BEGIN SELECT RAISE(ABORT, 'completion failed'); END")
        with pytest.raises(sqlite3.IntegrityError, match='completion failed'):
            Ingestor(sub, DifferentEmbedder()).ingest_document('a.md', data, ctx, 'coast', 'operator')
        assert vectors() == before
        # Verify persisted rows through a distinct connection, not the writer's cache.
        import sqlite_vec
        with sqlite3.connect(path) as verify:
            verify.enable_load_extension(True)
            sqlite_vec.load(verify)
            verify.enable_load_extension(False)
            persisted = {rid: verify.execute(f'SELECT embedding FROM {table} WHERE record_id=?', (rid,)).fetchone()[0] for rid in ids}
            assert persisted == before
        assert sub._store_version() == version
        sub._conn.execute('DROP TRIGGER fail_done')
        Ingestor(sub, DifferentEmbedder()).ingest_document('a.md', data, ctx, 'coast', 'operator')
        assert all(vectors()[rid] != before[rid] for rid in ids)
    finally:
        sub.close()
