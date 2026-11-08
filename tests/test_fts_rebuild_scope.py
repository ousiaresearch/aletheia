import pytest

from aletheia import cli
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import ScopeViolation, Substrate


def test_unknown_granted_scope_cannot_delete_orphan_index(tmp_path):
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    try:
        sub._conn.execute('INSERT INTO fts(record_id, scope, text) VALUES (?, ?, ?)',
                          ('orphan', 'unknown', 'preserve'))
        ctx = ScopeContext(granted_scopes=frozenset({'unknown'}))
        with pytest.raises(ScopeViolation):
            sub.rebuild_fts('unknown', ctx)
        assert sub._conn.execute('SELECT text FROM fts WHERE record_id=?', ('orphan',)).fetchone()[0] == 'preserve'
    finally:
        sub.close()


def test_cli_refuses_scope_with_no_authoritative_records(tmp_path, capsys):
    db = tmp_path / 'db'
    sub = Substrate(db, ScopeEngine(known_scopes={'coast'}))
    sub.close()
    assert cli.main(['--db', str(db), 'rebuild-fts', '--scope', 'coaast']) != 0
    assert 'rebuilt' not in capsys.readouterr().out
