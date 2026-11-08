import json

from aletheia import cli
from aletheia.ingest import Ingestor
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate


class Embedder:
    model_id = 'test/model'

    def embed_document(self, text):
        return [1.0] + [0.0] * 1023

    def embed_query(self, text):
        return [1.0] + [0.0] * 1023


def test_cli_rebuilds_old_fts_from_records_without_loading_model(tmp_path, monkeypatch, capsys):
    db = tmp_path / 'db'
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    sub = Substrate(db, ScopeEngine(known_scopes={'coast'}))
    sid = Ingestor(sub, Embedder()).ingest_document('a.txt', b'the coastal tide', ctx, 'coast', 'user')
    assert isinstance(sid, str)
    # Before canonical indexing, the in-memory Source was indexed in insertion
    # order (text, name), while the durable JSON sorts keys (name, text).
    old_source = Record(sid, 'Source', 'coast', {'text': 'the coastal tide', 'name': 'a.txt'})
    old_text = sub._payload_only_text(old_source)
    durable = sub.get(sid, ctx)
    assert durable is not None
    assert old_text != sub._payload_only_text(durable)
    sub._conn.execute('UPDATE fts SET text=? WHERE record_id=?', (old_text, sid))
    assert not sub.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
    sub.close()
    monkeypatch.setattr(cli, 'default_text_embedder', lambda: (_ for _ in ()).throw(AssertionError('model loaded')))
    assert cli.main(['--db', str(db), 'rebuild-fts', '--scope', 'coast']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'rebuilt'
    reopened = Substrate(db, ScopeEngine(known_scopes={'coast'}))
    try:
        assert reopened.vector_index_complete('coast', ctx, model_id=Embedder.model_id)
        stored = reopened.get(sid, ctx)
        assert stored is not None
        indexed = reopened._conn.execute('SELECT text FROM fts WHERE record_id=?', (sid,)).fetchone()
        assert indexed[0] == reopened._payload_only_text(stored)
        items, _ = recall(reopened, 'coastal', ctx, mode='lexical')
        assert any('coastal tide' in item.text for item in items)
    finally:
        reopened.close()
