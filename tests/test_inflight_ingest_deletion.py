import threading

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import RecordConflict, Substrate


def test_inflight_initial_ingest_does_not_recreate_deleted_observation(tmp_path):
    path = tmp_path / 'db'
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    first = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    second = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    data = b'first paragraph\n\nsecond paragraph'
    entered, release = threading.Event(), threading.Event()
    errors, results = [], []

    class PausedEmbedder:
        model_id = 'synthetic'

        def embed_document(self, text):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test did not release embedder')
            return [1.0] + [0.0] * 1023

    def ingest():
        try:
            results.append(Ingestor(first, PausedEmbedder()).ingest_document(
                'a.md', data, ctx, 'coast', 'operator'))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=ingest)
    try:
        thread.start()
        assert entered.wait(10), 'first ingestion never reached embedding'
        sid = Ingestor(second).ingest_document('a.md', data, ctx, 'coast', 'operator')
        assert isinstance(sid, str)
        oid = sid + '-p0000'
        assert second.get(oid, ctx) is not None
        assert second.delete(oid, ctx, reason='privacy')
        assert second.get(oid, ctx) is None
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RecordConflict), (errors, results)
        assert second.get(oid, ctx) is None
    finally:
        release.set()
        thread.join(10)
        first.close()
        second.close()
