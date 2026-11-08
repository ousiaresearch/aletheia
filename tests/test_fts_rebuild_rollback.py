import sqlite3

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


@pytest.mark.parametrize('failure', ['ABORT', 'ROLLBACK'])
def test_rebuild_failure_rolls_back_index_and_journal(tmp_path, failure):
    db = tmp_path / 'db'
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    sub = Substrate(db, ScopeEngine(known_scopes={'coast'}))
    try:
        sid = Ingestor(sub).ingest_document('a.txt', b'coastal tides', ctx, 'coast', 'user')
        assert isinstance(sid, str)
        sub._conn.execute('UPDATE fts SET text=? WHERE record_id=?', ('old projection', sid))
        before = [tuple(r) for r in sub._conn.execute('SELECT record_id,scope,text FROM fts ORDER BY record_id')]
        journal_before = sub._conn.execute('SELECT count(*) FROM journal').fetchone()[0]
        sub._conn.execute(f"CREATE TRIGGER inject_rebuild_failure BEFORE INSERT ON journal "
                          f"WHEN NEW.op='fts_rebuilt' BEGIN SELECT RAISE({failure}, 'injected failure'); END")
        with pytest.raises(sqlite3.DatabaseError):
            sub.rebuild_fts('coast', ctx)
        reader = sqlite3.connect(db)
        try:
            after = reader.execute('SELECT record_id,scope,text FROM fts ORDER BY record_id').fetchall()
            assert after == before
            assert reader.execute('SELECT count(*) FROM journal').fetchone()[0] == journal_before
            assert reader.execute('SELECT count(*) FROM records').fetchone()[0] >= 1
        finally:
            reader.close()
        sub._conn.execute('DROP TRIGGER inject_rebuild_failure')
        sub.rebuild_fts('coast', ctx)
        assert sub.fts_index_complete('coast', ctx)
    finally:
        sub.close()
