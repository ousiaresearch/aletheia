import pytest

from weft.ingest import Ingestor
from weft.retrieval import recall
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Substrate


class Embedder:
    model_id = 'test/model'

    def embed_document(self, text):
        return [1.0] + [0.0] * 1023

    embed_query = embed_document


# Round 5: ownership comes from the row's own scope column. A row naming no scope is
# unattributable and fails every scope; a row naming another scope is that scope's problem
# (see tests/test_phase14_round5_substrate.py).
@pytest.mark.parametrize('orphan_scope', [None])
@pytest.mark.parametrize('mode', ['lexical', 'hybrid'])
def test_recall_refuses_orphan_with_unowned_scope(tmp_path, orphan_scope, mode):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    opts = {'mode': mode}
    if mode == 'hybrid':
        opts['embedder'] = Embedder()
    try:
        sid = Ingestor(sub, Embedder()).ingest_document('a.txt', b'beacon tide records', ctx, 'coast', 'user')
        assert isinstance(sid, str)
        assert recall(sub, 'beacon', ctx, **opts)[0]
        sub._conn.execute('INSERT INTO fts(record_id, scope, text) VALUES (?,?,?)',
                          ('orphan', orphan_scope, 'beacon secret'))
        with pytest.raises(ValueError, match='complete'):
            recall(sub, 'beacon', ctx, **opts)
        assert sub._conn.execute('SELECT count(*) FROM fts WHERE record_id=?', ('orphan',)).fetchone()[0] == 1
    finally:
        sub.close()
