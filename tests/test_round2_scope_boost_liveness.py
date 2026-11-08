import pytest
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate
from aletheia.procedural import ProposedRule, propose_rule, promote_rule, list_active_rules
from aletheia.knowledge import KnowledgeRefusal


def setup_rule(tmp_path):
    engine = ScopeEngine(known_scopes={'a', 'b'})
    sub = Substrate(str(tmp_path / 'db'), engine)
    ctx = ScopeContext(granted_scopes={'a'}, user_authored=True)
    propose_rule(sub, ctx, ProposedRule('rule', 'a', 'name', 'description'))
    sub.put(Record(record_id='approval', record_type='Decision', scope='a', payload={'rule_id': 'rule', 'approved': True}, attribution='user'), ctx)
    return sub, ctx, engine


def test_active_rule_requires_current_readable_promoter(tmp_path):
    sub, ctx, engine = setup_rule(tmp_path)
    promote_rule(sub, ctx, 'rule', 'approval')
    assert len(list_active_rules(sub, ctx, 'a')) == 1
    engine.register_scope('b', {'approved'})
    assert list_active_rules(sub, ctx, 'a') == []


def test_superseded_rule_is_not_active(tmp_path):
    sub, ctx, _ = setup_rule(tmp_path)
    promote_rule(sub, ctx, 'rule', 'approval')
    assert len(list_active_rules(sub, ctx, 'a')) == 1
    sub.correct('rule', Record(record_id='correction', record_type='Correction', scope='a', payload={'text': 'withdrawn'}, attribution='user'), ctx)
    assert list_active_rules(sub, ctx, 'a') == []


def test_superseded_proposal_cannot_be_promoted(tmp_path):
    sub, ctx, _ = setup_rule(tmp_path)
    sub.correct('rule', Record(record_id='correction', record_type='Correction', scope='a', payload={'text': 'withdrawn'}, attribution='user'), ctx)
    with pytest.raises(KnowledgeRefusal):
        promote_rule(sub, ctx, 'rule', 'approval')


def test_entity_link_requires_scope_and_record_authority(tmp_path):
    from aletheia.entity_link import link_entities
    engine = ScopeEngine(known_scopes={'a', 'b'})
    sub = Substrate(str(tmp_path / 'db'), engine)
    both = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True)
    only_a = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    for rid, scope, text in [('visible', 'a', 'Zanzibar'), ('hidden', 'a', 'Zanzibar secretmarker'), ('other', 'b', 'Zanzibar')]:
        sub.put(Record(record_id=rid, record_type='Source', scope=scope, payload={'text': text}, attribution='user'), both)
    engine.register_scope('b', {'secretmarker'})
    assert link_entities(sub, only_a, 'b') == {}
    result = link_entities(sub, only_a, 'a')
    assert result == {'records_scanned': 1, 'links_created': 1}
    assert sub._conn.execute('SELECT source_id FROM entity_links').fetchall() == [('visible',)]


def test_entity_frequency_ignores_unreadable_and_other_scope(tmp_path):
    from aletheia.entity_link import link_entities, entity_boost
    engine = ScopeEngine(known_scopes={'a', 'b'})
    sub = Substrate(str(tmp_path / 'db'), engine)
    both = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True)
    only_a = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    for rid, scope, text in [('visible', 'a', 'Zanzibar'), ('hidden', 'a', 'Zanzibar secretmarker'), ('other', 'b', 'Zanzibar')]:
        sub.put(Record(record_id=rid, record_type='Source', scope=scope, payload={'text': text}, attribution='user'), both)
    link_entities(sub, both, 'a')
    link_entities(sub, both, 'b')
    assert entity_boost(sub, both, 'Zanzibar', ['visible'], min_doc_freq=3)
    engine.register_scope('b', {'secretmarker'})
    assert entity_boost(sub, only_a, 'Zanzibar', ['visible'], min_doc_freq=3) == {}


@pytest.mark.parametrize('layer', ['graph_boost', 'temporal_boost'])
def test_boost_reorders_and_receipt_reports_applied_layer(tmp_path, monkeypatch, layer):
    import aletheia.retrieval as retrieval
    import aletheia.temporal as temporal
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    for rid in ('first', 'second'):
        sub.put(Record(record_id=rid, record_type='Source', scope='a', payload={'text': 'needle'}, attribution='user'), ctx)
    initial, _ = retrieval.recall(sub, 'needle', ctx, mode='lexical')
    target = initial[-1].record_id
    def scores(*args, **kwargs):
        return {target: 1.0}
    if layer == 'graph_boost':
        monkeypatch.setattr(retrieval, 'entity_boost', scores)
    else:
        monkeypatch.setattr(temporal, 'temporal_boost', scores)
    items, receipt = retrieval.recall(sub, 'needle', ctx, mode='lexical')
    assert items[0].record_id == target
    assert layer in receipt.layers


def test_failed_boost_is_not_reported(tmp_path, monkeypatch):
    import aletheia.retrieval as retrieval
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    sub.put(Record(record_id='r', record_type='Source', scope='a', payload={'text': 'needle'}, attribution='user'), ctx)
    monkeypatch.setattr(retrieval, 'entity_boost', lambda *a, **k: {'r': object()})
    _, receipt = retrieval.recall(sub, 'needle', ctx, mode='lexical')
    assert 'graph_boost' not in receipt.layers


def test_confidence_sort_reorders_claims(tmp_path):
    from aletheia.retrieval import recall
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    sub.put(Record(record_id='source', record_type='Source', scope='a', payload={'text': 'source'}, attribution='user'), ctx)
    for rid, confidence in [('first', 0.1), ('second', 0.9)]:
        sub.put(Record(record_id=rid, record_type='Claim', scope='a', epistemic_status='derived', source_ids=['source'], payload={'text': 'needle', 'confidence': confidence}, attribution='agent'), ctx)
    items, _ = recall(sub, 'needle', ctx, mode='lexical')
    assert [item.record_id for item in items] == ['second', 'first']
