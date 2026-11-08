import time

import pytest

from aletheia.knowledge import KnowledgeRefusal
from aletheia.procedural import ProposedRule, propose_rule, promote_rule
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, ScopeViolation, Substrate


@pytest.mark.parametrize('invalid', ['expired', 'bounded', 'future_start', 'future_effective', 'superseded'])
def test_auto_selection_skips_invalid_approval(tmp_path, invalid):
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    propose_rule(sub, ctx, ProposedRule('rule', 'a', 'name', 'description'))
    now = time.time()
    fields = {
        'expired': {'valid_to': now - 100},
        'bounded': {'valid_to': now + 10000},
        'future_start': {'valid_from': now + 10000},
        'future_effective': {'effective_at': now + 10000},
        'superseded': {},
    }[invalid]
    sub.put(Record(record_id='bad', record_type='Decision', scope='a', epistemic_status='stated',
                   payload={'rule_id': 'rule', 'approved': True}, attribution='user', **fields), ctx)
    if invalid == 'superseded':
        sub.correct('bad', Record(record_id='replacement', record_type='Correction', scope='a', epistemic_status='stated',
                                  payload={'note': 'withdrawn'}, attribution='user'), ctx)
    sub.put(Record(record_id='good', record_type='Commitment', scope='a', epistemic_status='stated',
                   payload={'rule_id': 'rule', 'approved': True}, attribution='user'), ctx)
    # An explicit invalid choice must not fall back to the valid approval.
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        promote_rule(sub, ctx, 'rule', 'bad')
    assert sub.get('rule', ctx).payload['status'] == 'proposed'
    result = promote_rule(sub, ctx, 'rule')
    assert result.payload['status'] == 'active'
    assert result.payload['promoted_by'] == 'good'
    assert sub.get('rule', ctx).payload['promoted_by'] == 'good'


def test_auto_selection_refuses_when_only_invalid_approval_exists(tmp_path):
    sub = Substrate(str(tmp_path / 'db'), ScopeEngine(known_scopes={'a'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    propose_rule(sub, ctx, ProposedRule('rule', 'a', 'name', 'description'))
    sub.put(Record(record_id='bad', record_type='Decision', scope='a', epistemic_status='stated',
                   payload={'rule_id': 'rule', 'approved': True}, attribution='user', valid_to=time.time() - 100), ctx)
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        promote_rule(sub, ctx, 'rule')
    assert sub.get('rule', ctx).payload['status'] == 'proposed'
