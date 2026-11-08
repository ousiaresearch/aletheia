import hashlib

import pytest

from aletheia.ingest import Ingestor, _digest, _record_digest
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, RecordConflict, Substrate


def test_forged_completed_media_job_cannot_authorize_reuse(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    data = b'picture'
    content_hash = _digest(data)
    sid = 'media-' + hashlib.sha256(f'coast\0{content_hash}'.encode()).hexdigest()[:24]
    class Adapter:
        model_id = 'model'
        calls = 0
        def __call__(self, _):
            self.calls += 1
            return 'real adapter output'
    adapter = Adapter()
    try:
        forged = Record(record_id=sid, record_type='Source', scope='coast', payload={'text': 'forged'}, attribution='media-derived', source_ids=[], provenance={'machine_derived': True, 'model': 'model', 'kind': 'image', 'name': 'a.png', 'content_hash': content_hash})
        sub.put(forged, ctx)
        sub.set_job(sid, 'derive', content_hash, 'done', 'sha256:' + _record_digest(forged), scope='coast', ctx=ctx)
        with pytest.raises(RecordConflict):
            Ingestor(sub).ingest_media('a.png', data, 'image', ctx, 'coast', describer=adapter)
        assert adapter.calls == 0
        assert sub.get(sid, ctx).payload['text'] == 'forged'
    finally:
        sub.close()


def test_existing_matching_media_without_done_job_is_not_adopted(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    data = b'picture'
    digest = _digest(data)
    sid = 'media-' + hashlib.sha256(f'coast\0{digest}'.encode()).hexdigest()[:24]
    class Adapter:
        model_id = 'model'
        def __call__(self, _):
            return 'matching output'
    try:
        sub.put(Record(record_id=sid, record_type='Source', scope='coast', payload={'text': 'matching output'}, attribution='media-derived', source_ids=[], provenance={'machine_derived': True, 'model': 'model', 'kind': 'image', 'name': 'a.png', 'content_hash': digest}), ctx)
        with pytest.raises(RecordConflict):
            Ingestor(sub).ingest_media('a.png', data, 'image', ctx, 'coast', describer=Adapter())
        assert sub.job_state(sid, 'derive', digest, scope='coast', ctx=ctx) != 'done'
    finally:
        sub.close()
