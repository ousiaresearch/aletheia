"""Phase 14 round 9: the 'Deletion and erasure' docs are strictly accurate (R8-05, R8-01/03)."""
import re
import shlex
from pathlib import Path

import pytest

from aletheia import cli

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / 'docs' / 'turnkey-deployment.md').read_text()
README = (ROOT / 'README.md').read_text()


def _norm(text):
    return ' '.join(text.lower().split())


def _section(text, title):
    m = re.search(r'^## ' + re.escape(title) + r'\n(.*?)(?=^## |\Z)', text, re.S | re.M)
    assert m, title
    return m.group(1)


ERASURE = _norm(_section(DOC, 'Deletion and erasure'))
BACKUP = _norm(_section(DOC, 'Backup and restore'))


def test_a_logical_erasure_commits_and_leaves_live_db_and_fts():
    assert 'commits logical erasure immediately' in ERASURE
    assert 'live database' in ERASURE and 'fts index' in ERASURE


def test_b_wal_truncate_is_conditional_and_deferred():
    for p in ('truncate checkpoint', 'concurrent reader', 'delay', 'bounded backoff',
              'scrub-pending flag', 'complete_scrub()', 'next open', '-wal` file'):
        assert p in ERASURE, p
    assert 'deleted plaintext may remain' in ERASURE


def test_c_journal_redaction_and_non_erased_copies():
    for p in ('audit rows', 'type, time, actor, scope', 'record id', 'free-text reason',
              '[erased]', 'before this round', 'backups', 'exports already written',
              'snapshots', 'storage-media remanence'):
        assert p in ERASURE, p


def test_d_content_derived_ids():
    for p in ('content-derived', 'doc-', 'media-', 'extracted-', 'sha256', 'reveal content'):
        assert p in ERASURE, p


def test_e_refusal_when_cited():
    for p in ('refused', 'cites the target', "scope's journal", 'substrate.last_refusal'):
        assert p in ERASURE, p


def test_f_cost_and_deferred_scrub():
    for p in ('o(index)', 'fts optimize', '0.18 s', '20k records', '0.85 s', '100k records',
              'bulk deletes', 'deferred scrub'):
        assert p in ERASURE, p


def test_g_backup_migration_and_newer_schema():
    for p in ('lineage_erased', 'before this version', 'newer schema version', 'refused'):
        assert p in BACKUP, p


def test_no_unconditional_wal_truncation_claim():
    for text in (_norm(DOC), _norm(README)):
        for bad in ('does not remain in those live files', 'removes a record from the live database, '
                    'the full-text (fts) index and the write-ahead log',
                    'delete compacts the fts index and truncates the wal, so',
                    'wal is always truncated', 'wal is truncated on every delete',
                    'always truncates the wal', 'unconditionally'):
            assert bad not in text, bad
    # every sentence mentioning WAL truncation in the section is conditioned
    for s in re.split(r'(?<=[.!?])\s+', ERASURE):
        if 'truncates the wal' in s or 'truncated' in s and 'wal' in s:
            assert any(w in s for w in ('only', 'unless', 'can ', 'delay', 'if ', 'when')), s


def test_readme_consistent_no_stale_counts():
    low = _norm(README)
    assert not re.search(r'\b\d{3,} (tests|passing)', low)
    assert 'wal' not in low or 'turnkey-deployment' in low


def _commands(text):
    out = []
    for block in re.findall(r'```(?:bash|sh)?\n(.*?)```', text, re.S):
        for line in re.sub(r'\\\n', ' ', block).splitlines():
            line = line.strip()
            if line.startswith('aletheia ') and not line.startswith('aletheia-server'):
                out.append(line)
    out += re.findall(r'`(aletheia [^`]*--[^`]*)`', text)
    return out


@pytest.mark.parametrize('text', [DOC, README], ids=['docs', 'readme'])
def test_every_documented_command_parses_with_real_parser(text):
    cmds = _commands(text)
    assert cmds
    for cmd in cmds:
        argv = shlex.split(cmd.replace('<path>', '/tmp/x.db').replace('<scope>', 'coast'))[1:]
        try:
            cli._parser().parse_args(argv)
        except SystemExit:
            pytest.fail(f'documented command does not parse: {cmd}')
