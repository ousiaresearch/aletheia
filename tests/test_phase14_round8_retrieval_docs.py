"""Phase 14 round 8: neighbors never displace matches (R7-04), docs/README truthfulness (R7-05/07/08)."""
import argparse
import re
import shlex
from pathlib import Path

import pytest

from weft import cli
from weft.retrieval import recall
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate

ROOT = Path(__file__).resolve().parents[1]
DIM = 1024


def _vec(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


class Emb:
    """Query -> e0. Text containing NEARQUERY -> e0 (cosine 1); matches -> e1 (cosine 0);
    other -> e5."""
    model_id = 'fake/m@1'

    def _v(self, text):
        if 'NEARQUERY' in text:
            return _vec(0)
        return _vec(1) if 'harbor' in text else _vec(5)

    def embed_query(self, text):
        return _vec(0)

    def embed_document(self, text):
        return self._v(text)

    def embed_documents(self, texts):
        return [self._v(t) for t in texts]


def _store(tmp_path, neighbor_type):
    sub = Substrate(str(tmp_path / 'w.db'), ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True, actor='u')
    prov = {'conversation_id': 'c1'}
    for rid, stamp in (('m1', 1), ('m2', 3)):
        sub.put(Record(record_id=rid, record_type='Source', scope='a', attribution='user',
                       payload={'text': f'harbor {rid}', 'created_at': stamp, 'provenance': prov}), ctx)
    if neighbor_type == 'Source':
        sub.put(Record(record_id='near', record_type='Source', scope='a', attribution='user',
                       payload={'text': 'NEARQUERY unrelated', 'created_at': 2, 'provenance': prov}), ctx)
    else:
        sub.put(Record(record_id='near', record_type='Decision', scope='a', attribution='user',
                       payload={'text': 'NEARQUERY unrelated', 'created_at': 2, 'provenance': prov}), ctx)
    return sub, ctx


def _vectorise(sub, ctx, emb):
    for rid in ('m1', 'm2'):
        rec = sub.get(rid, ctx)
        sub.vector_put(rid, 'a', emb.embed_document(rec.payload['text']), ctx, model_id=emb.model_id)
    if sub.get('near', ctx).record_type == 'Source':
        sub.vector_put('near', 'a', emb.embed_document('NEARQUERY unrelated'), ctx, model_id=emb.model_id)


@pytest.mark.parametrize('rerank', [True, False])
@pytest.mark.parametrize('limit', [1, 2])
def test_lexical_neighbor_never_displaces_match(tmp_path, rerank, limit):
    sub, ctx = _store(tmp_path, 'Source')
    items, _ = recall(sub, 'harbor', ctx, limit=limit, mode='lexical', rerank=rerank, embedder=Emb())
    ids = [i.record_id for i in items]
    matches = [i for i in ids if i in ('m1', 'm2')]
    assert len(matches) == limit, ids
    assert all(not i.neighbor for i in items if i.record_id in ('m1', 'm2'))


@pytest.mark.parametrize('rerank', [True, False])
@pytest.mark.parametrize('limit', [1, 2])
def test_hybrid_neighbor_never_displaces_match(tmp_path, rerank, limit):
    sub, ctx = _store(tmp_path, 'Decision')
    emb = Emb()
    _vectorise(sub, ctx, emb)
    items, _ = recall(sub, 'harbor', ctx, limit=limit, mode='hybrid', rerank=rerank, embedder=emb)
    ids = [i.record_id for i in items]
    assert 'near' not in ids, ids
    assert len(ids) == limit and set(ids) <= {'m1', 'm2'}


def test_neighbor_fills_remaining_slot_after_matches(tmp_path):
    sub, ctx = _store(tmp_path, 'Source')
    items, _ = recall(sub, 'harbor', ctx, limit=3, mode='lexical', rerank=True, embedder=Emb())
    ids = [i.record_id for i in items]
    assert set(ids[:2]) == {'m1', 'm2'} and ids[2] == 'near' and items[2].neighbor


def test_matches_keep_ranked_order_through_rerank(tmp_path):
    sub, ctx = _store(tmp_path, 'Source')
    a, _ = recall(sub, 'harbor', ctx, limit=2, mode='lexical', rerank=False)
    b, _ = recall(sub, 'harbor', ctx, limit=2, mode='lexical', rerank=True, embedder=Emb())
    assert {i.record_id for i in a} == {i.record_id for i in b} == {'m1', 'm2'}


def test_requested_cross_encoder_reranks_even_when_all_matches_fit(tmp_path, monkeypatch):
    import weft.retrieval as retrieval
    sub, ctx = _store(tmp_path, 'Decision')
    class Reranker:
        def rerank(self, query, items, top_k):
            return list(reversed(items))[:top_k]
    monkeypatch.setattr(retrieval, 'CrossEncoderReranker', Reranker)
    original, _ = recall(sub, 'harbor', ctx, limit=5, mode='lexical')
    ranked, _ = recall(sub, 'harbor', ctx, limit=5, mode='lexical', use_cross_encoder=True)
    assert [i.record_id for i in ranked] == ['m2', 'm1', 'near']
    assert [i.record_id for i in original] == ['m1', 'm2', 'near']


def test_requested_cross_encoder_constructor_failure_propagates(tmp_path, monkeypatch):
    import weft.retrieval as retrieval
    sub, ctx = _store(tmp_path, 'Decision')
    def broken():
        raise RuntimeError('reranker unavailable')
    monkeypatch.setattr(retrieval, 'CrossEncoderReranker', broken)
    with pytest.raises(RuntimeError, match='reranker unavailable'):
        recall(sub, 'harbor', ctx, limit=5, mode='lexical', use_cross_encoder=True)


# ---- docs ----------------------------------------------------------------------------------

DOC = (ROOT / 'docs' / 'turnkey-deployment.md').read_text()
README = (ROOT / 'README.md').read_text()


def _commands(text):
    out = []
    for block in re.findall(r'```(?:bash|sh)?\n(.*?)```', text, re.S):
        joined = re.sub(r'\\\n', ' ', block)
        for line in joined.splitlines():
            line = line.strip()
            if line.startswith('weft ') and not line.startswith('weft-server'):
                out.append(line)
    for m in re.findall(r'`(weft [^`]*--[^`]*)`', text):  # inline snippets carrying flags
        out.append(m)
    return out


def _parse(cmd):
    cmd = cmd.replace('<path>', '/tmp/x.db').replace('<scope>', 'coast')
    argv = shlex.split(cmd)[1:]
    return cli._parser().parse_args(argv)


@pytest.mark.parametrize('text', [DOC, README], ids=['docs', 'readme'])
def test_documented_cli_commands_parse(text):
    cmds = _commands(text)
    assert cmds
    for cmd in cmds:
        try:
            _parse(cmd)
        except SystemExit:
            pytest.fail(f'documented command does not parse: {cmd}')


def test_documented_commands_cover_reembed_and_rebuild():
    names = {_parse(c).command for c in _commands(DOC)}
    assert {'reembed', 'rebuild-fts'} <= names


def test_docs_reembed_needs_local_embedder_not_container():
    low = ' '.join(DOC.lower().split())
    assert 'darwin/arm64' in low and 'mlx' in low
    assert 'lexical-only by design' in low
    assert 'desktop' in low and 'no vectors' in low
    assert 'works inside the container' not in low
    assert 'use `local-recall`' not in DOC
    assert 'use local-recall' not in low
    # no instruction to run reembed in the container
    assert 'docker compose exec' not in low and 'docker exec' not in low


def test_readme_current_state():
    assert '430 tests' not in README
    assert not re.search(r'\b\d{3} tests passing', README)
    assert 'scripts/test.sh' in README
    assert 'WEFT_API_KEY' in README and 'WEFT_KNOWN_SCOPES' in README
    assert '401' in README and '403' in README
    assert '--allow-external' in README
    assert 'unverified' in README.lower() and 'docker' in README.lower()
    assert 'sentence-transformers' not in README or 'does not install' in README


def test_docs_erasure_section_is_honest():
    low = ' '.join(DOC.lower().split())
    assert 'compacts the fts index' in low and 'truncates the wal' in low
    for phrase in ('backups', 'exports already written', 'snapshots', 'storage-media remanence'):
        assert phrase in low, phrase


def test_docs_lineage_erased_marker_under_backup():
    section = ' '.join(DOC.split('## Backup and restore', 1)[1].split('\n## ', 1)[0].lower().split())
    assert 'lineage-erased' in section and 'user-authored import' in section
    assert 'tombstone' not in section
