"""Phase 14 round 7: extraction read gate and egress opt-in, neighbor determinism, adapters, docs."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from weft import cli, extraction
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate

ROOT = Path(__file__).resolve().parent.parent


def _store(tmp_path):
    engine = ScopeEngine(known_scopes={'a', 'b'})
    sub = Substrate(tmp_path / 'w.db', engine)
    root = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True, actor='u')
    sub.put(Record(record_id='b-src', record_type='Source', scope='b', attribution='user',
                   payload={'text': 'CLASSIFIED: the vault code of Orion is violet.'}), root)
    sub.put(Record(record_id='b-cl1', record_type='Claim', scope='b', attribution='agent',
                   epistemic_status='derived', source_ids=['b-src'], payload={'text': 'orion is violet'}), root)
    sub.put(Record(record_id='b-cl2', record_type='Claim', scope='b', attribution='agent',
                   epistemic_status='derived', source_ids=['b-src'], payload={'text': 'Orion is violet'}), root)
    return sub, ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True, actor='u'), root


def _no_network(monkeypatch):
    import urllib.request
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError('external endpoint must not be called')
    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    return calls


# ---- R6-01 -----------------------------------------------------------------------------------

def test_extract_facts_ungranted_caller_gets_nothing_and_sends_nothing(tmp_path, monkeypatch):
    sub, ctx_a, _ = _store(tmp_path)
    monkeypatch.setenv('TOKENHARBOR_API_KEY', 'dummy')
    monkeypatch.setenv('WEFT_EXTRACT_ALLOW_EXTERNAL', '1')
    calls = _no_network(monkeypatch)
    try:
        assert extraction.extract_facts(sub, ctx_a, 'b') == []
        assert calls == []
    finally:
        sub.close()


def test_consolidate_scope_ungranted_caller_gets_nothing(tmp_path):
    sub, ctx_a, root = _store(tmp_path)
    try:
        assert extraction.consolidate_scope(sub, ctx_a, 'b') in ({}, {'claims_reviewed': 0, 'duplicate_count': 0, 'kept': 0})
        assert extraction.consolidate_scope(sub, root, 'b')['claims_reviewed'] == 2
    finally:
        sub.close()


def test_extract_facts_respects_content_marker_gate(tmp_path, monkeypatch):
    """A record the read gate withholds is never sent, even inside a granted scope."""
    sub, _, root = _store(tmp_path)
    monkeypatch.setenv('TOKENHARBOR_API_KEY', 'dummy')
    monkeypatch.setenv('WEFT_EXTRACT_ALLOW_EXTERNAL', '1')
    sent = []
    monkeypatch.setattr(extraction, '_extract_facts', lambda text, scope: sent.append(text) or [])
    try:
        readable = {r.record_id for r in sub.query(root, scope='b')[0]}
        extraction.extract_facts(sub, root, 'b')
        assert any('Orion' in t for t in sent) == ('b-src' in readable)
    finally:
        sub.close()


# ---- R6-02 -----------------------------------------------------------------------------------

def test_library_extraction_requires_explicit_opt_in(tmp_path, monkeypatch):
    sub, _, root = _store(tmp_path)
    monkeypatch.setenv('TOKENHARBOR_API_KEY', 'dummy')
    monkeypatch.delenv('WEFT_EXTRACT_ALLOW_EXTERNAL', raising=False)
    calls = _no_network(monkeypatch)
    try:
        assert extraction.extract_facts(sub, root, 'b') == []
        assert calls == []
    finally:
        sub.close()


def test_cli_extract_refuses_without_opt_in(tmp_path, monkeypatch, capsys):
    sub, _, _ = _store(tmp_path)
    sub.close()
    monkeypatch.setenv('TOKENHARBOR_API_KEY', 'dummy')
    monkeypatch.delenv('WEFT_EXTRACT_ALLOW_EXTERNAL', raising=False)
    calls = _no_network(monkeypatch)
    rc = cli.main(['--db', str(tmp_path / 'w.db'), 'extract', '--scope', 'b'])
    assert rc != 0
    assert calls == []
    assert 'allow-external' in capsys.readouterr().err


def test_cli_extract_with_flag_prints_destination_host_first(tmp_path, monkeypatch, capsys):
    sub, _, _ = _store(tmp_path)
    sub.close()
    monkeypatch.setenv('TOKENHARBOR_API_KEY', 'dummy')
    monkeypatch.delenv('WEFT_EXTRACT_ALLOW_EXTERNAL', raising=False)
    monkeypatch.setattr(extraction, '_extract_facts', lambda text, scope: [])
    rc = cli.main(['--db', str(tmp_path / 'w.db'), 'extract', '--scope', 'b', '--allow-external'])
    assert rc == 0
    assert 'tokenharbor.ai' in capsys.readouterr().err


def test_no_personal_credential_path(tmp_path, monkeypatch):
    assert 'vireth' not in (ROOT / 'weft' / 'extraction.py').read_text()
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.config' / 'vireth').mkdir(parents=True)
    (tmp_path / '.config' / 'vireth' / 'th_token').write_text('personal')
    monkeypatch.delenv('TOKENHARBOR_API_KEY', raising=False)
    assert extraction._get_token() == ''
    monkeypatch.setenv('TOKENHARBOR_API_KEY', 'envkey')
    assert extraction._get_token() == 'envkey'


# ---- R6-08 -----------------------------------------------------------------------------------

_PROBE = r'''
import json, sys, tempfile
from pathlib import Path
from weft.retrieval import recall
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate
d = tempfile.mkdtemp()
sub = Substrate(Path(d) / 'w.db', ScopeEngine(known_scopes={'a'}))
ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True, actor='u')
for i in range(9):
    text = 'harbor ledger' if i == 4 else f'unrelated note {i}'
    sub.put(Record(record_id=f't{i}', record_type='Source', scope='a', attribution='user',
                   payload={'text': text, 'created_at': i, 'provenance': {'conversation_id': 'c'}}), ctx)
sub.put(Record(record_id='late', record_type='Source', scope='a', attribution='user',
               payload={'text': 'harbor tide', 'created_at': 99}), ctx)
items, _ = recall(sub, 'harbor', ctx, limit=int(sys.argv[1]), mode='lexical')
print(json.dumps([i.record_id for i in items]))
'''


def _run_probe(seed, limit):
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env['PYTHONHASHSEED'] = str(seed)
    out = subprocess.run([sys.executable, '-c', _PROBE, str(limit)], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_neighbor_expansion_deterministic_across_hash_seeds():
    results = {tuple(_run_probe(seed, 3)) for seed in (1, 2, 3, 4, 5, 6)}
    assert len(results) == 1, results


def test_neighbors_never_displace_matching_records():
    got = _run_probe(7, 2)
    assert set(got) == {'t4', 'late'}, got  # both matches outrank neighbors t3/t5


def test_neighbor_expand_ties_and_missing_created_at_are_stable(tmp_path):
    from weft.retrieval import _neighbor_expand
    sub = Substrate(tmp_path / 'w.db', ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True, actor='u')
    try:
        for i, stamp in enumerate([None, 3, 3, 0, 1e300]):
            sub.put(Record(record_id=f'h{i}', record_type='Source', scope='a', attribution='user',
                           payload={'text': 't', 'created_at': stamp,
                                    'provenance': {'conversation_id': 'c'}}), ctx)
        out = _neighbor_expand(sub, ['h2'], 5, ['a'])
        assert out[0] == 'h2' and len(out) > 1
        assert out == _neighbor_expand(sub, ['h2'], 5, ['a'])
    finally:
        sub.close()


# ---- R6-10 -----------------------------------------------------------------------------------

def test_adapters_no_hardcoded_profile_and_no_import_time_mkdir(tmp_path):
    src = (ROOT / 'weft' / 'adapters.py').read_text()
    assert 'hermes' not in src and 'arien' not in src
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    target = tmp_path / 'scratch'
    env['WEFT_SCRATCH_DIR'] = str(target)
    code = ('import os, weft.adapters as a; '
            f'assert not os.path.exists({str(target)!r}); '
            'print(a._scratch_dir())')
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(target) and target.is_dir()


def test_adapters_default_is_tempdir(monkeypatch):
    import tempfile
    from weft import adapters
    monkeypatch.delenv('WEFT_SCRATCH_DIR', raising=False)
    assert adapters._scratch_dir().startswith(tempfile.gettempdir()) or 'weft' in adapters._scratch_dir()
    assert 'hermes' not in adapters._scratch_dir()


# ---- R6-09 / docs ----------------------------------------------------------------------------

def test_docs_state_real_restore_and_extract_facts():
    doc = (ROOT / 'docs' / 'turnkey-deployment.md').read_text()
    readme = (ROOT / 'README.md').read_text()
    assert 'added in this round' not in doc
    assert 'import_scope' in doc and 'Python API' in doc
    assert 'registered verifier' in doc and 'user-authored' in doc
    for text in (doc, readme):
        assert 'allow-external' in text and 'TOKENHARBOR_API_KEY' in text
