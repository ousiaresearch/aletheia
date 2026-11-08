from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate
from aletheia.retrieval import recall
from aletheia.procedural import ProposedRule, list_active_rules, propose_rule, promote_rule


def test_neighbor_expansion_obeys_scope_hint_and_receipt(tmp_path):
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a', 'b'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True)
    for rid, scope, text, stamp in [('hit', 'a', 'needle', 1), ('neighbor', 'b', 'other', 2)]:
        sub.put(Record(record_id=rid, record_type='Source', scope=scope, epistemic_status='stated',
                       payload={'text': text, 'created_at': stamp, 'provenance': {'conversation_id': 'shared'}}, attribution='user'), ctx)
    unrestricted, _ = recall(sub, 'needle', ctx, mode='lexical', limit=10)
    assert {r.record_id for r in unrestricted} == {'hit', 'neighbor'}
    items, receipt = recall(sub, 'needle', ctx, scope_hint='a', mode='lexical', limit=10)
    assert [r.record_id for r in items] == ['hit']
    assert receipt.scope_consulted == ['a']
    assert receipt.selected == ['hit']


def test_active_rules_obey_per_record_content_gate(tmp_path):
    engine = ScopeEngine(known_scopes={'a', 'b'})
    sub = Substrate(str(tmp_path / 'db'), engine)
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    propose_rule(sub, ctx, ProposedRule('rule', 'a', 'name', 'secretmarker'))
    sub.put(Record(record_id='approval', record_type='Decision', scope='a', epistemic_status='stated',
                   payload={'rule_id': 'rule', 'approved': True}, attribution='user'), ctx)
    promote_rule(sub, ctx, 'rule', 'approval')
    assert [r.record_id for r in list_active_rules(sub, ctx, 'a')] == ['rule']
    engine.register_scope('b', {'secretmarker'})
    assert sub.get('rule', ctx) is None
    assert list_active_rules(sub, ctx, 'a') == []


def test_automatic_promotion_search_uses_rule_scope(tmp_path):
    ctx = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True)
    scope = list(ctx.granted_scopes)[1]
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a', 'b'}))
    propose_rule(sub, ctx, ProposedRule('rule', scope, 'name', 'description'))
    sub.put(Record(record_id='approval', record_type='Decision', scope=scope, epistemic_status='stated',
                   payload={'rule_id': 'rule', 'approved': True}, attribution='user'), ctx)
    assert promote_rule(sub, ctx, 'rule').payload['promoted_by'] == 'approval'


def test_automatic_promotion_skips_unreadable_approval(tmp_path):
    engine = ScopeEngine(known_scopes={'a', 'b'})
    sub = Substrate(str(tmp_path / 'db'), engine)
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    propose_rule(sub, ctx, ProposedRule('rule', 'a', 'name', 'description'))
    for rid, note in [('hidden', 'secretmarker'), ('visible', 'normal')]:
        sub.put(Record(record_id=rid, record_type='Decision', scope='a', epistemic_status='stated',
                       payload={'rule_id': 'rule', 'approved': True}, provenance={'note': note}, attribution='user'), ctx)
    engine.register_scope('b', {'secretmarker'})
    assert sub.get('hidden', ctx) is None
    assert sub.get('visible', ctx) is not None
    assert promote_rule(sub, ctx, 'rule').payload['promoted_by'] == 'visible'
