import sqlite3

import pytest

from aletheia import cli


@pytest.mark.parametrize('kind', ['foreign', 'empty', 'missing'])
def test_maintenance_rejects_non_aletheia_store_without_mutation(tmp_path, capsys, kind):
    path = tmp_path / 'foreign.sqlite'
    if kind == 'foreign':
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)')
        con.execute('INSERT INTO notes(body) VALUES (?)', ('preserve',))
        con.commit()
        con.close()
    elif kind == 'empty':
        path.touch()
    before = path.read_bytes() if path.exists() else None
    assert cli.main(['--db', str(path), 'rebuild-fts', '--scope', 'coast']) != 0
    assert (path.read_bytes() if path.exists() else None) == before
    assert not list(tmp_path.glob('foreign.sqlite-*'))
