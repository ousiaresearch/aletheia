import copy

import pytest

from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate, SCHEMA_VERSION, ScopeViolation


@pytest.fixture
def store(tmp_path):
    sub = Substrate(tmp_path / 'authority.db', ScopeEngine(known_scopes={'a', 'b'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True)
    yield sub, ctx
    sub.close()


def rule():
    return Record(record_id='rule', record_type='ProposedRule', scope='a',
                  epistemic_status='stated', payload={'status': 'proposed'}, attribution='agent')


def bundle(*records):
    return {'schema_version': SCHEMA_VERSION, 'scope': 'a',
            'records': [Substrate._record_to_export(r) for r in records]}


@pytest.mark.parametrize('path', ['put', 'import'])
@pytest.mark.parametrize('promoter', ['missing', 'wrong_type', 'wrong_target', 'agent', 'other_scope'])
def test_activation_rejects_invalid_promoter(store, path, promoter):
    sub, ctx = store
    original = rule()
    sub.put(original, ctx)
    if promoter != 'missing':
        approval = Record(record_id='approval', record_type='Source' if promoter == 'wrong_type' else 'Decision',
                          scope='b' if promoter == 'other_scope' else 'a', epistemic_status='stated',
                          payload={'rule_id': 'other' if promoter == 'wrong_target' else 'rule'},
                          attribution='agent' if promoter == 'agent' else 'user')
        if promoter == 'agent':
            sub._conn.execute("INSERT INTO records (record_id, record_type, scope, epistemic_status, payload, created_at, source_ids, provenance, attribution) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ('approval', 'Decision', 'a', 'stated', '{"rule_id":"rule"}', 0, '[]', '{}', 'agent'))
        else:
            sub.put(approval, ctx)
    active = copy.deepcopy(original)
    active.payload.update(status='active', promoted_by='approval')
    if path == 'put':
        with pytest.raises(ScopeViolation):
            sub.put(active, ctx, overwrite=True)
    else:
        assert sub.import_scope(bundle(active), ctx) == 0
    assert sub.get('rule', ctx).payload['status'] == 'proposed'


@pytest.mark.parametrize('target', ['missing', 'foreign'])
def test_import_rejects_invalid_supersedes(store, target):
    sub, ctx = store
    if target == 'foreign':
        sub.put(Record(record_id='target', record_type='Source', scope='b',
                       epistemic_status='stated', payload={'text': 'target'}, attribution='user'), ctx)
    record = Record(record_id='dependent', record_type='Source', scope='a',
                    epistemic_status='stated', payload={'text': 'dependent'}, attribution='user', supersedes='target')
    assert sub.import_scope(bundle(record), ctx) == 0
    assert sub.get('dependent', ctx) is None


def test_import_accepts_forward_same_scope_supersedes(store):
    sub, ctx = store
    target = Record(record_id='target', record_type='Source', scope='a',
                    epistemic_status='stated', payload={'text': 'target'}, attribution='user')
    dependent = Record(record_id='dependent', record_type='Correction', scope='a',
                       epistemic_status='stated', payload={'text': 'dependent'}, attribution='user', supersedes='target')
    assert sub.import_scope(bundle(dependent, target), ctx) == 2
    assert sub.get('dependent', ctx).supersedes == 'target'


def test_import_commits_for_an_independent_connection(store):
    sub, ctx = store
    record = Record(record_id='durable', record_type='Source', scope='a',
                    epistemic_status='stated', payload={'text': 'saved'}, attribution='user')
    assert sub.import_scope(bundle(record), ctx) == 1
    import sqlite3
    db_path = sub._conn.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(db_path) as other:
        assert other.execute("SELECT count(*) FROM records WHERE record_id='durable'").fetchone()[0] == 1


@pytest.mark.parametrize('path', ['put', 'import'])
@pytest.mark.parametrize('kind,link', [('Decision', 'rule_id'), ('Commitment', 'source_ids')])
def test_activation_with_valid_approval_put_only_import_refuses(store, path, kind, link):
    sub, ctx = store
    original = rule()
    sub.put(original, ctx)
    approval = Record(record_id='approval', record_type=kind, scope='a', epistemic_status='stated',
                      payload={'rule_id': 'rule', 'approved': True},
                      source_ids=['rule'] if link == 'source_ids' else [], attribution='user')
    sub.put(approval, ctx)
    active = copy.deepcopy(original)
    active.payload.update(status='active', promoted_by='approval')
    if path == 'put':
        sub.put(active, ctx, overwrite=True)
        assert sub.get('rule', ctx).payload['status'] == 'active'
    else:
        # Round 6 policy: import never activates or transitions an existing rule, even with a
        # valid local approval; the rule stays proposed.
        assert sub.import_scope(bundle(active), ctx) == 0
        assert sub.get('rule', ctx).payload['status'] == 'proposed'
