import sqlite3

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


def test_document_database_failure_rolls_back_all_outputs_and_can_retry(tmp_path):
    sub = Substrate(tmp_path / 'test.db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sub._conn.execute("CREATE TRIGGER fail_observation BEFORE INSERT ON records WHEN NEW.record_type='Observation' BEGIN SELECT RAISE(ABORT, 'injected'); END")
        version = sub._store_version()
        with pytest.raises(sqlite3.IntegrityError, match='injected'):
            Ingestor(sub).ingest_document('a.md', b'first paragraph\n\nsecond paragraph', ctx, 'coast', 'operator')
        assert sub._conn.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 0
        assert sub._conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        assert sub._conn.execute('SELECT COUNT(*) FROM fts').fetchone()[0] == 0
        assert sub._store_version() == version
        sub._conn.execute('DROP TRIGGER fail_observation')
        Ingestor(sub).ingest_document('a.md', b'first paragraph\n\nsecond paragraph', ctx, 'coast', 'operator')
        assert sub._conn.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 3
    finally:
        sub.close()


def test_media_invalid_vector_rolls_back_source_and_marks_failure(tmp_path):
    class BadVector:
        model_id = 'bad'
        def embed_document(self, text):
            return [1.0]

    sub = Substrate(tmp_path / 'media.db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        with pytest.raises(ValueError, match='dimensions'):
            Ingestor(sub, embedder=BadVector()).ingest_media(
                'photo.png', b'picture', 'image', ctx, 'coast', describer=lambda _: 'a lighthouse')
        assert sub._conn.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 0
        assert sub._conn.execute('SELECT state, reason FROM jobs').fetchall() == [('failed', 'embedding_failed')]
    finally:
        sub.close()


def test_media_failed_job_write_does_not_mask_embedding_failure(tmp_path):
    class Broken:
        model_id = 'broken'
        def embed_document(self, text):
            raise RuntimeError('model broke')

    sub = Substrate(tmp_path / 'failure.db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sub._conn.execute("CREATE TRIGGER fail_job BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT, 'job update broke'); END")
        with pytest.raises(RuntimeError, match='model broke') as error:
            Ingestor(sub, embedder=Broken()).ingest_media(
                'x.png', b'picture', 'image', ctx, 'coast', describer=lambda _: 'lighthouse')
        assert any('job update broke' in note for note in error.value.__notes__)
        assert sub._conn.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 0
    finally:
        sub.close()
