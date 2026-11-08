import threading

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import RecordConflict, Substrate


def test_completed_document_retry_does_not_recreate_deleted_observation(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    data = b'first paragraph\n\nsecond paragraph'
    try:
        ingestor = Ingestor(sub)
        sid = ingestor.ingest_document('a.md', data, ctx, 'coast', 'operator')
        oid = sid + '-p0000'
        assert sub._lookup_record(oid) is not None
        assert sub.delete(oid, ctx, reason='privacy')
        with pytest.raises(RecordConflict):
            ingestor.ingest_document('a.md', data, ctx, 'coast', 'operator')
        assert sub._lookup_record(oid) is None
    finally:
        sub.close()


def test_concurrent_delete_during_retry_embedding_is_not_recreated(tmp_path):
    path = tmp_path / 'db'
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    sub = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    other = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    data = b'first paragraph\n\nsecond paragraph'
    entered, release = threading.Event(), threading.Event()
    errors = []

    class PausedEmbedder:
        model_id = 'synthetic'
        def embed_document(self, text):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test failed to release embedder')
            return [1.0] + [0.0] * 1023

    def retry():
        try:
            Ingestor(sub, PausedEmbedder()).ingest_document('a.md', data, ctx, 'coast', 'operator')
        except BaseException as exc:
            errors.append(exc)

    thread = None
    try:
        sid = Ingestor(sub).ingest_document('a.md', data, ctx, 'coast', 'operator')
        oid = sid + '-p0000'
        assert other.get(oid, ctx) is not None
        thread = threading.Thread(target=retry)
        thread.start()
        assert entered.wait(5), 'retry never reached embedding'
        assert other.delete(oid, ctx, reason='privacy')
        assert other.get(oid, ctx) is None
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RecordConflict)
        assert other.get(oid, ctx) is None
    finally:
        release.set()
        if thread is not None:
            thread.join(5)
        sub.close()
        other.close()
