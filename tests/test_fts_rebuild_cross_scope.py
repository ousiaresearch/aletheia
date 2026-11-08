import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate, SubstrateError


@pytest.mark.parametrize('other_scope', [None, 'other'])
def test_rebuild_refuses_mislabelled_duplicate_without_deleting_it(tmp_path, other_scope):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast', 'other'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sid = Ingestor(sub).ingest_document('a.txt', b'tidal records', ctx, 'coast', 'user')
        assert isinstance(sid, str)
        sub.rebuild_fts('coast', ctx)  # valid positive control
        sub._conn.execute('INSERT INTO fts(record_id, scope, text) VALUES (?, ?, ?)',
                          (sid, other_scope, 'preserve marker'))
        with pytest.raises(SubstrateError):
            sub.rebuild_fts('coast', ctx)
        assert sub._conn.execute('SELECT count(*) FROM fts WHERE record_id=? AND text=?',
                                 (sid, 'preserve marker')).fetchone()[0] == 1
        assert sub.get(sid, ctx) is not None
    finally:
        sub.close()
