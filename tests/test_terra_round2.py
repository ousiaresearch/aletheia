import hashlib

import pytest

from aletheia.ingest import Ingestor, _digest
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, RecordConflict, ScopeViolation, Substrate

CTX = ScopeContext(granted_scopes=frozenset({'coast'}))

class Embedder:
    model_id = 'original'
    def __init__(self):
        self.calls = []
    def embed_document(self, text):
        self.calls.append(text)
        return [1.0] + [0.0] * 1023


def test_done_repair_embedding_failure_leaves_no_partial_changes(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    try:
        sid = Ingestor(sub, Embedder()).ingest_document('a.md', b'one\n\ntwo', CTX, 'coast', 'a')
        # The different model has no index; keep the completed bundle intact.
        before = {table: sub._conn.execute(f'SELECT * FROM {table}').fetchall() for table in ('records', 'fts', 'journal', 'jobs')}
        version = sub._store_version()
        class Broken(Embedder):
            model_id = 'different'
            def embed_document(self, text):
                raise RuntimeError('model failed')
        with pytest.raises(RuntimeError, match='model failed'):
            Ingestor(sub, Broken()).ingest_document('a.md', b'one\n\ntwo', CTX, 'coast', 'a')
        assert {table: sub._conn.execute(f'SELECT * FROM {table}').fetchall() for table in before} == before
        assert sub._store_version() == version
    finally:
        sub.close()


def test_hidden_incumbent_conflict_is_generic_denial(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast', 'inland'}))
    data = b'ordinary'
    sid = 'doc-' + hashlib.sha256(f'coast\0{_digest(data)}'.encode()).hexdigest()[:24]
    incumbent = Record(record_id=sid, record_type='Source', scope='coast', payload={'text': 'secretmarker'}, attribution='document')
    try:
        sub.put(incumbent, CTX)
        assert sub.get(sid, CTX) is not None
        sub.scope_engine = ScopeEngine(known_scopes={'coast', 'inland'}, markers={'inland': {'secretmarker'}})
        assert sub.get(sid, CTX) is None
        with pytest.raises(ScopeViolation, match='write not permitted'):
            Ingestor(sub).ingest_document('a.md', data, CTX, 'coast', 'a')
    finally:
        sub.close()


@pytest.mark.parametrize('kind', ['turn', 'document'])
@pytest.mark.parametrize('denial', ['grant', 'content'])
def test_write_denial_precedes_embedding(tmp_path, kind, denial):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast', 'inland'}, markers={'inland': {'secretmarker'}}))
    model = Embedder()
    ctx = ScopeContext() if denial == 'grant' else CTX
    text = 'secretmarker' if denial == 'content' else 'ordinary'
    try:
        with pytest.raises(ScopeViolation):
            if kind == 'turn':
                Ingestor(sub, model).ingest_turn(text, 'user', ctx, 'coast', 'chat')
            else:
                Ingestor(sub, model).ingest_document('a.md', text.encode(), ctx, 'coast', 'a')
        assert model.calls == []
    finally:
        sub.close()


def test_vector_search_beyond_knn_limit_preserves_visible_results(tmp_path):
    import sqlite_vec
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    try:
        sub.put(Record(record_id='visible', record_type='Source', scope='coast', payload={'text': 'visible'}, attribution='document'), CTX)
        far = [0.0, 1.0] + [0.0] * 1022
        query = [1.0] + [0.0] * 1023
        sub.vector_put('visible', 'coast', far, CTX, model_id='m')
        table = sub._vec_table('coast', 'm')
        blob = sqlite_vec.serialize_float32(query)
        sub._conn.executemany(f'INSERT INTO {table}(record_id, embedding) VALUES (?, ?)',
                              ((f'orphan-{i}', blob) for i in range(4096)))
        assert sub.vector_candidates(query, CTX, 1, model_id='m') == ['visible']
    finally:
        sub.close()


def test_vector_has_record_does_not_observe_rolled_back_ingestion(tmp_path):
    import threading
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ready, release, finished = threading.Event(), threading.Event(), threading.Event()
    results = []
    sub.scope_engine.register_scope('inland')
    sub.put(Record(record_id='temporary', record_type='Source', scope='coast', payload={'text': 'test'}, attribution='document'), CTX)
    sub.vector_put('temporary', 'coast', [1.0] + [0.0] * 1023, CTX, model_id='m')
    def writer():
        try:
            with sub.ingestion_transaction():
                sub._conn.execute("UPDATE records SET scope='inland' WHERE record_id='temporary'")
                ready.set()
                assert release.wait(5)
                raise RuntimeError('rollback')
        except RuntimeError:
            pass
    def reader():
        results.append(sub.vector_has_record('temporary', 'coast', CTX, model_id='m'))
        finished.set()
    a = threading.Thread(target=writer)
    b = threading.Thread(target=reader)
    try:
        a.start()
        assert ready.wait(5)
        b.start()
        assert not finished.wait(0.2)
        release.set()
        a.join(5); b.join(5)
        assert not a.is_alive() and not b.is_alive()
        assert results == [True]
    finally:
        release.set(); a.join(5); b.join(5); sub.close()
