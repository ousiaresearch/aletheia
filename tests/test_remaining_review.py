import hashlib
import threading
import pytest
from aletheia.ingest import Ingestor, _digest
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, RecordConflict, ScopeViolation, Substrate

CTX = ScopeContext(granted_scopes=frozenset({'coast'}))
def record(rid, text):
    return Record(record_id=rid, record_type='Source', scope='coast', payload={'text': text}, attribution='document', source_ids=[])
def vec(i):
    v = [0.0] * 1024
    v[i] = 1.0
    return v

def test_hidden_vectors_do_not_suppress_visible_results(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast', 'inland'}))
    try:
        sub.put(record('visible', 'ordinary'), CTX)
        sub.vector_put('visible', 'coast', vec(1), CTX, model_id='m')
        assert sub.vector_candidates(vec(0), CTX, 1, model_id='m') == ['visible']
        for i in range(4):
            rid = f'hidden{i}'
            sub.put(record(rid, 'secretmarker'), CTX)
            sub.vector_put(rid, 'coast', vec(0), CTX, model_id='m')
        sub.scope_engine = ScopeEngine(known_scopes={'coast', 'inland'}, markers={'inland': {'secretmarker'}})
        assert sub.vector_candidates(vec(0), CTX, 1, model_id='m') == ['visible']
    finally:
        sub.close()


def test_vector_write_reserves_database_before_authorization(tmp_path):
    path = tmp_path / 'db'
    sub = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    other = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    sub.put(record('victim', 'ordinary'), CTX)
    entered, release, deleted = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = sub._lookup_record
    def paused(rid):
        rec = original(rid)
        entered.set()
        assert release.wait(5)
        return rec
    sub._lookup_record = paused
    def write():
        try:
            sub.vector_put('victim', 'coast', vec(0), CTX, model_id='m')
        except BaseException as exc:
            errors.append(exc)
    def delete():
        try:
            other.delete('victim', CTX, reason='test')
            deleted.set()
        except BaseException as exc:
            errors.append(exc)
    a = threading.Thread(target=write); a.start()
    assert entered.wait(5)
    b = threading.Thread(target=delete); b.start()
    try:
        assert not deleted.wait(0.3), 'delete committed during vector authorization'
    finally:
        release.set(); a.join(5); b.join(5)
        sub.close(); other.close()
    assert not a.is_alive() and not b.is_alive()
    assert not errors
    assert deleted.is_set()


def test_document_conflicting_incumbent_is_not_accepted(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    data = b'actual'
    sid = 'doc-' + hashlib.sha256(f'coast\0{_digest(data)}'.encode()).hexdigest()[:24]
    try:
        sub.put(record(sid, 'unrelated'), CTX)
        with pytest.raises(RecordConflict):
            Ingestor(sub).ingest_document('a.md', data, CTX, 'coast', 'a')
        assert sub._lookup_record(sid + '-p0000') is None
    finally:
        sub.close()


def test_media_conflicting_incumbent_is_not_accepted(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    data = b'picture'
    sid = 'media-' + hashlib.sha256(f'coast\0{_digest(data)}'.encode()).hexdigest()[:24]
    try:
        sub.put(record(sid, 'unrelated'), CTX)
        with pytest.raises(RecordConflict):
            Ingestor(sub).ingest_media('a.png', data, 'image', CTX, 'coast', describer=lambda _: 'derived')
    finally:
        sub.close()


def test_late_failed_media_attempt_does_not_overwrite_done(tmp_path):
    path = tmp_path / 'db'
    first = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    second = Substrate(path, ScopeEngine(known_scopes={'coast'}))
    entered, release = threading.Event(), threading.Event()
    results = []
    def adapter(_):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('failed')
    def run():
        results.append(Ingestor(first).ingest_media('a.png', b'picture', 'image', CTX, 'coast', describer=adapter))
    thread = threading.Thread(target=run); thread.start()
    try:
        assert entered.wait(5)
        sid = Ingestor(second).ingest_media('a.png', b'picture', 'image', CTX, 'coast', describer=lambda _: 'good')
        release.set(); thread.join(5)
        assert not thread.is_alive()
        assert first.job_state(sid, 'derive', _digest(b'picture'), scope='coast', ctx=CTX) == 'done'
    finally:
        release.set(); thread.join(5); first.close(); second.close()


@pytest.mark.parametrize('kind', ['document', 'media'])
def test_done_shortcut_requires_write_authority(tmp_path, kind):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}, global_allowlist={'coast'}))
    ing = Ingestor(sub)
    def run(ctx):
        if kind == 'document':
            return ing.ingest_document('a.md', b'actual', ctx, 'coast', 'a')
        return ing.ingest_media('a.png', b'picture', 'image', ctx, 'coast', describer=lambda _: 'derived')
    try:
        assert isinstance(run(CTX), str)
        with pytest.raises(ScopeViolation):
            run(ScopeContext())
    finally:
        sub.close()


def test_done_document_rejects_altered_source(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ing = Ingestor(sub)
    try:
        sid = ing.ingest_document('a.md', b'actual', CTX, 'coast', 'a')
        sub.put(record(sid, 'altered'), CTX, overwrite=True)
        with pytest.raises(RecordConflict):
            ing.ingest_document('a.md', b'actual', CTX, 'coast', 'a')
    finally:
        sub.close()


def test_done_media_rejects_altered_source(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ing = Ingestor(sub)
    try:
        sid = ing.ingest_media('a.png', b'picture', 'image', CTX, 'coast', describer=lambda _: 'derived')
        sub.put(record(sid, 'altered'), CTX, overwrite=True)
        with pytest.raises(RecordConflict):
            ing.ingest_media('a.png', b'picture', 'image', CTX, 'coast', describer=lambda _: 'derived')
    finally:
        sub.close()


def test_media_done_retry_is_refused_without_trusted_provenance(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ing = Ingestor(sub)
    calls = []
    def adapter(_):
        calls.append(1)
        return 'derived' if len(calls) == 1 else 'different'
    try:
        sid = ing.ingest_media('a.png', b'picture', 'image', CTX, 'coast', describer=adapter)
        assert sub.get(sid, CTX).payload['text'] == 'derived'
        with pytest.raises(RecordConflict, match='reuse is unavailable'):
            ing.ingest_media('a.png', b'picture', 'image', CTX, 'coast', describer=adapter)
        assert sub.get(sid, CTX).payload['text'] == 'derived'
        assert len(calls) == 1
    finally:
        sub.close()
