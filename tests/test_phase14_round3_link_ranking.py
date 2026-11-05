import sqlite3

import pytest

from weft.entity_link import link_entities
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate


def setup_store(tmp_path):
    path = tmp_path / 'weft.db'
    sub = Substrate(str(path), ScopeEngine(known_scopes={'a', 'b'}))
    ctx = ScopeContext(granted_scopes={'a', 'b'}, user_authored=True)
    return path, sub, ctx


def test_nested_linking_rolls_back_with_outer_transaction(tmp_path):
    path, sub, ctx = setup_store(tmp_path)
    with pytest.raises(RuntimeError):
        with sub.ingestion_transaction():
            sub.put(Record('source', 'Source', 'a', {'text': 'Zanzibar'}, attribution='user'), ctx)
            assert link_entities(sub, ctx, 'a')['links_created'] == 1
            raise RuntimeError('abort')
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM records WHERE record_id='source'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM entity_links').fetchone()[0] == 0
    sub.put(Record('source', 'Source', 'a', {'text': 'Zanzibar'}, attribution='user'), ctx)
    assert link_entities(sub, ctx, 'a')['links_created'] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM entity_links').fetchone()[0] == 1


def test_link_failure_rolls_back_all_links(tmp_path):
    path, sub, ctx = setup_store(tmp_path)
    sub.put(Record('source', 'Source', 'a', {'text': 'Zanzibar Harbor'}, attribution='user'), ctx)
    sub._conn.execute("CREATE TRIGGER fail_link BEFORE INSERT ON entity_links WHEN NEW.target_id='harbor' BEGIN SELECT RAISE(ABORT, 'failed'); END")
    assert link_entities(sub, ctx, 'a') == {}
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM entity_links').fetchone()[0] == 0
    sub._conn.execute('DROP TRIGGER fail_link')
    assert link_entities(sub, ctx, 'a')['links_created'] > 1


def test_weak_confident_claim_does_not_outrank_strong_source(tmp_path):
    from weft.retrieval import recall
    _, sub, ctx = setup_store(tmp_path)
    sub.put(Record('strong', 'Source', 'a', {'text': 'Zanzibar harbor Zanzibar harbor Zanzibar harbor'}, attribution='user'), ctx)
    sub.put(Record('weak', 'Claim', 'a', {'text': 'harbor', 'confidence': 1000000000}, epistemic_status='derived', source_ids=['strong'], attribution='agent'), ctx)
    assert sub.fts_candidates('"Zanzibar" OR "harbor"', ctx, 10, scopes=['a'])[0] == 'strong'
    items, receipt = recall(sub, 'Zanzibar harbor', ctx, scope_hint='a', mode='lexical', limit=1)
    assert [x.record_id for x in items] == ['strong']
    assert receipt.selected == ['strong']


def test_distant_boosts_cannot_overtake_strongest_and_receipt_tier_is_selected(tmp_path, monkeypatch):
    import weft.retrieval as retrieval
    import weft.temporal as temporal
    _, sub, ctx = setup_store(tmp_path)
    for i in range(6):
        sub.put(Record(f'r{i}', 'Source', 'a', {'text': ('needle ' * (8-i)) + ('x' * 450 + ' needle' if i == 5 else '')}, attribution='user'), ctx)
    baseline = sub.fts_candidates('"needle"', ctx, 6, scopes=['a'])
    assert len(baseline) == 6
    target = baseline[-1]
    monkeypatch.setattr(retrieval, 'entity_boost', lambda *a, **k: {target: 1e100})
    monkeypatch.setattr(temporal, 'temporal_boost', lambda *a, **k: {target: 1e100})
    items, receipt = retrieval.recall(sub, 'needle', ctx, scope_hint='a', mode='lexical', limit=1)
    assert items[0].record_id == baseline[0]
    assert receipt.selected == [baseline[0]]
    assert receipt.tier == items[0].tier
