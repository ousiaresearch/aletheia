"""Round-3 substrate repairs (Terra/Sonnet R3), written red first.

Design notes:
* Approval is structured: a user Decision/Commitment binds a ProposedRule only when its payload
  is exactly {"rule_id": <rule>, "approved": True}. Naming a rule (source_ids, applies_to, free
  text, approved False) never creates a binding.
* reject/deprecate need a user-authored context only, and may change status fields only.
* Import of a proposed rule restores the proposal; its approval does NOT travel with the bundle
  and must be given again.
"""
import json
import sqlite3

import pytest

from aletheia.knowledge import KnowledgeRefusal
from aletheia.procedural import deprecate_rule, promote_rule, reject_rule
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import (_IMPORTABLE_SCHEMA_VERSIONS, SCHEMA_VERSION, Record, SchemaViolation, ScopeViolation,
                            Substrate)


@pytest.fixture
def env(tmp_path):
    s = Substrate(tmp_path / 's.db', ScopeEngine(known_scopes={'a', 'b'}))
    c = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True)
    yield s, c
    s.close()


def rec(id, typ='Source', scope='a', **kw):
    return Record(record_id=id, record_type=typ, scope=scope,
                  payload=kw.pop('payload', {'text': 'content'}),
                  attribution=kw.pop('attribution', 'user'), **kw)


def rule(id='r', **payload):
    return rec(id, 'ProposedRule', payload={'status': 'proposed', 'description': 'x', **payload},
               attribution='agent')


def bundle(*rs, scope='a'):
    return {'scope': scope, 'schema_version': SCHEMA_VERSION,
            'records': [Substrate._record_to_export(r) for r in rs]}


def bindings(s):
    return s._conn.execute('SELECT promoter_id, rule_id FROM rule_approvals').fetchall()


# ---- (1) structured, affirmative approval --------------------------------------------------

@pytest.mark.parametrize('payload,source_ids', [
    ({'rule_id': 'r', 'approved': False}, []),
    ({'rule_id': 'r', 'text': 'I reject this rule'}, []),
    ({'rule_id': 'r'}, []),
    ({'applies_to': 'r', 'approved': True}, []),
    ({'text': 'approve', 'approved': True}, ['r']),
    ({'rule_id': 'r', 'approved': 'yes'}, []),
    ({'rule_id': 'r', 'approved': 1}, []),
])
def test_only_affirmative_structured_decision_creates_binding(env, payload, source_ids):
    s, c = env
    s.put(rule(), c)
    s.put(rec('d', 'Decision', payload=payload, source_ids=source_ids), c)
    assert bindings(s) == []
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        promote_rule(s, c, 'r', 'd')


def test_affirmative_decision_binds_and_commitment_too(env):
    s, c = env
    s.put(rule(), c)
    s.put(rec('d', 'Commitment', payload={'rule_id': 'r', 'approved': True}), c)
    assert bindings(s) == [('d', 'r')]
    assert promote_rule(s, c, 'r', 'd').payload['status'] == 'active'


def test_later_rejection_overwrite_removes_binding(env):
    s, c = env
    s.put(rule(), c)
    s.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': True}), c)
    s.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': False}), c, overwrite=True)
    assert bindings(s) == []
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        promote_rule(s, c, 'r', 'd')


# ---- (1/policy a) reject/deprecate: user_authored only, status fields only ------------------

def test_reject_needs_user_context_not_a_decision(env):
    s, c = env
    s.put(rule(), c)
    assert reject_rule(s, c, 'r').payload['status'] == 'rejected'
    s.put(rule('r2'), c)
    agent = ScopeContext(granted_scopes=frozenset({'a'}))
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        reject_rule(s, agent, 'r2')


@pytest.mark.parametrize('target', ['rejected', 'deprecated'])
def test_proposed_transition_cannot_edit_content(env, target):
    s, c = env
    s.put(rule(), c)
    edited = s.get('r', c)
    edited.payload = {**edited.payload, 'status': target, 'description': 'EVIL'}
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        s.put(edited, c, overwrite=True)
    assert s.get('r', c).payload['description'] == 'x'
    assert s.get('r', c).payload['status'] == 'proposed'


def test_active_deprecation_cannot_edit_content(env):
    s, c = env
    s.put(rule(), c)
    s.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': True}), c)
    promote_rule(s, c, 'r', 'd')
    edited = s.get('r', c)
    edited.payload = {**edited.payload, 'status': 'deprecated', 'description': 'EVIL'}
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        s.put(edited, c, overwrite=True)
    assert deprecate_rule(s, c, 'r').payload['description'] == 'x'


def test_import_transition_cannot_edit_content(env):
    s, c = env
    s.put(rule(), c)
    edited = rule(description='EVIL')
    edited.payload['status'] = 'rejected'
    assert s.import_scope(bundle(edited), c) == 0
    assert s.get('r', c).payload['description'] == 'x'


# ---- (2) revocation is durable ------------------------------------------------------------

def test_supersession_removes_binding_itself(env):
    s, c = env
    s.put(rule(), c)
    s.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': True}), c)
    assert bindings(s) == [('d', 'r')]
    s.correct('d', rec('fix', 'Correction', payload={'text': 'withdrawn'}), c)
    assert bindings(s) == []


def test_withdrawn_approval_does_not_revive_after_correction_removed_and_decision_untouched(env):
    s, c = env
    agent = ScopeContext(granted_scopes=frozenset({'a'}))
    s.put(rule(), c)
    s.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': True}), c)
    s.correct('d', rec('fix', 'Correction', payload={'text': 'withdrawn'}), c)
    assert s.delete('fix', agent)
    assert s.get('d', c).supersedes is None
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        promote_rule(s, c, 'r', 'd')
    # a fresh, explicit approval is the only way back
    s.put(rec('d2', 'Decision', payload={'rule_id': 'r', 'approved': True}), c)
    assert promote_rule(s, c, 'r', 'd2').payload['promoted_by'] == 'd2'


# ---- (3) universal text / timestamp validation ----------------------------------------------

@pytest.mark.parametrize('path', ['put', 'put_no_schema', 'import'])
@pytest.mark.parametrize('kw', [
    {'valid_from': 10.0, 'valid_to': 5.0},
    {'created_at': 10 ** 400},
    {'effective_at': float('-inf')},
    {'valid_to': False},
])
def test_window_ordering_and_extreme_numbers_refused(env, path, kw):
    s, c = env
    r = rec('bad')
    for k, v in kw.items():
        setattr(r, k, v)
    with pytest.raises(SchemaViolation):
        if path == 'import':
            s.import_scope(bundle(rec('good'), r), c)
        else:
            s.put(r, c, validate_schema=path == 'put')
    assert not s.all_records(c)


@pytest.mark.parametrize('path', ['put', 'import'])
def test_equal_window_bounds_allowed(env, path):
    s, c = env
    r = rec('ok', valid_from=5.0, valid_to=5.0)
    if path == 'put':
        s.put(r, c)
    else:
        assert s.import_scope(bundle(r), c) == 1


@pytest.mark.parametrize('bad', [
    {'scope': 'a', 'schema_version': SCHEMA_VERSION, 'records': None},
    {'scope': 'a', 'schema_version': SCHEMA_VERSION, 'records': 'x'},
    {'scope': 'a', 'schema_version': SCHEMA_VERSION, 'records': [{}]},
    {'scope': 'a', 'schema_version': SCHEMA_VERSION, 'records': [5]},
])
def test_malformed_bundle_is_schema_violation(env, bad):
    s, c = env
    with pytest.raises(SchemaViolation):
        s.import_scope(bad, c)


# ---- (4) citation scope ---------------------------------------------------------------------

@pytest.mark.parametrize('typ,kw', [
    ('Decision', {}),
    ('Commitment', {}),
    ('Summary', {'epistemic_status': 'derived', 'attribution': 'system'}),
    ('Hypothesis', {'epistemic_status': 'derived', 'attribution': 'agent'}),
])
@pytest.mark.parametrize('path', ['put', 'import'])
def test_any_record_citing_foreign_scope_source_refused(env, typ, kw, path):
    s, c = env
    s.put(rec('src'), c)
    foreign = rec('f', typ, scope='b', source_ids=['src'], **kw)
    b = ScopeContext(granted_scopes=frozenset({'b'}), user_authored=True)
    if path == 'put':
        with pytest.raises((ScopeViolation, KnowledgeRefusal)):
            s.put(foreign, b)
    else:
        assert s.import_scope(bundle(foreign, scope='b'), b) == 0
    assert s.get('f', c) is None
    assert s.delete('src', ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True))


def test_preexisting_foreign_citation_does_not_obstruct_deletion(env):
    s, c = env
    s.put(rec('src'), c)
    s.put(rec('f', 'Decision', scope='b'), c)
    s._conn.execute("UPDATE records SET source_ids='[\"src\"]' WHERE record_id='f'")
    a = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    assert s.delete('src', a)
    assert s.get('f', c) is not None


def test_same_scope_citations_allowed_missing_or_foreign_refused_alike(env):
    s, c = env
    s.put(rec('src'), c)
    s.put(rec('d', 'Decision', source_ids=['src']), c)
    # Missing and foreign ids are the same refusal, so a writer cannot probe other scopes.
    with pytest.raises((ScopeViolation, KnowledgeRefusal)):
        s.put(rec('d2', 'Decision', source_ids=['not-yet']), c)


# ---- (5) import order and reasons ------------------------------------------------------------

def test_overwritten_source_export_order_round_trips(env, tmp_path):
    s, c = env
    s.put(rec('src'), c)
    s.put(rec('obs', 'Observation', attribution='document', source_ids=['src']), c)
    s.put(rec('src', payload={'text': 'changed'}), c, overwrite=True)
    data = s.export_scope('a', c)
    assert [r['record_id'] for r in data['records']] == ['obs', 'src']
    t = Substrate(tmp_path / 't.db', ScopeEngine(known_scopes={'a', 'b'}))
    try:
        assert t.import_scope(data, c) == 2
    finally:
        t.close()


def test_refusal_reason_is_reported_not_bare_zero(env):
    s, c = env
    active = rec('r', 'ProposedRule', attribution='agent',
                 payload={'status': 'active', 'promoted_by': 'd'})
    assert s.import_scope(bundle(rec('ok'), active), c) == 0
    assert s.last_import_refusal and "'r'" in s.last_import_refusal
    assert 'active' in s.last_import_refusal
    with pytest.raises(KnowledgeRefusal, match='active'):
        s.import_scope(bundle(rec('ok'), active), c, strict=True)
    assert s.import_scope(bundle(rec('ok')), c) == 1
    assert s.last_import_refusal is None


@pytest.mark.parametrize('make,word', [
    (lambda: [rec('x'), rec('x')], 'duplicate'),
    (lambda: [rec('p', 'Summary', attribution='system', epistemic_status='derived', source_ids=['q']),
              rec('q', 'Summary', attribution='system', epistemic_status='derived', source_ids=['p'])], 'cycle'),
    (lambda: [rec('o', scope='b')], 'scope'),
])
def test_refusal_reasons_name_the_cause(env, make, word):
    s, c = env
    assert s.import_scope(bundle(*make()), c) == 0
    assert word in s.last_import_refusal


def test_unwritable_scope_reason_is_generic(env):
    s, c = env
    a = ScopeContext(granted_scopes=frozenset({'b'}), user_authored=True)
    assert s.import_scope(bundle(rec('x')), a) == 0
    assert s.last_import_refusal == "write not permitted"


# ---- (6) transitive cascade ----------------------------------------------------------------

def test_cascade_covers_hypothesis_and_lineage_artifacts(env):
    s, c = env
    s.put(rec('src'), c)
    s.put(rec('sum', 'Summary', attribution='system', epistemic_status='derived', source_ids=['src']), c)
    s.put(rec('hyp', 'Hypothesis', attribution='agent', epistemic_status='derived', source_ids=['sum']), c)
    s.put(rec('cl', 'Claim', attribution='agent', epistemic_status='derived', source_ids=['hyp']), c)
    s.put(rec('obs', 'Observation', attribution='document', source_ids=['src']), c)
    s.put(rec('keep'), c)
    s.vector_put('hyp', 'a', [0.1] * 1024, c, model_id='m')
    assert s.delete('src', c)
    assert [r.record_id for r in s.all_records(c)] == ['keep']
    assert s.fts_candidates('content', c, 20, scopes={'a'}) == ['keep']
    assert s.fts_index_complete('a', c)
    assert s._vec_exists('a', 'm') and not s._conn.execute(
        f"SELECT 1 FROM {s._vec_table('a', 'm')} WHERE record_id='hyp'").fetchone()


def test_cascade_removes_entity_links_to_deleted_records(env):
    s, c = env
    s.put(rec('src'), c)
    s.put(rec('sum', 'Summary', attribution='system', epistemic_status='derived', source_ids=['src']), c)
    s.put(rec('keep'), c)
    for target in ('src', 'sum'):
        s._conn.execute("INSERT INTO entity_links(source_id,target_id,relation,scope) VALUES ('keep',?,'rel','a')", (target,))
    assert s.delete('src', c)
    assert s._conn.execute('SELECT count(*) FROM entity_links').fetchone()[0] == 0


def test_cascade_cleans_supersedes_links_of_survivors(env):
    s, c = env
    s.put(rec('src'), c)
    s.put(rec('sum', 'Summary', attribution='system', epistemic_status='derived', source_ids=['src']), c)
    s.put(rec('old'), c)
    s.correct('old', rec('fix', 'Correction'), c)
    s._conn.execute("UPDATE records SET supersedes='sum' WHERE record_id='fix'")
    assert s.delete('src', c)
    assert s.get('fix', c).supersedes is None


# ---- (7) migration -------------------------------------------------------------------------

def _head_store(tmp_path, active_ok=True):
    """Build a store the way HEAD (schema 6, no rule_* tables) wrote it."""
    path = tmp_path / 'old.db'
    s = Substrate(path, ScopeEngine(known_scopes={'a'}))
    c = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    s.put(rule('ract'), c)
    s.put(rule('rprop'), c)
    s.put(rec('d1', 'Decision', payload={'rule_id': 'ract'}), c)
    s.put(rec('d2', 'Decision', payload={'rule_id': 'rprop'}), c)
    row = s._conn.execute("SELECT payload FROM records WHERE record_id='ract'").fetchone()
    payload = json.loads(row[0])
    payload.update(status='active', promoted_by='d1', promoted_at=1.0)
    s._conn.execute("UPDATE records SET payload=? WHERE record_id='ract'", (json.dumps(payload, sort_keys=True),))
    s._conn.execute('DROP TABLE rule_approvals')
    s._conn.execute('DROP TABLE rule_incarnations')
    s._conn.execute('PRAGMA user_version = 6')
    s._conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    s.close()
    return path


def test_schema_version_bumped():
    # Bumping this means a migration was added: 8 adds the `relations` table (phase 12 / W2.1).
    # The migration path is exercised below, from a v6 store, and must land on SCHEMA_VERSION.
    assert SCHEMA_VERSION == 8


def test_head_store_migrates_failing_closed_with_reapproval(tmp_path):
    path = _head_store(tmp_path)
    c = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    s = Substrate(path, ScopeEngine(known_scopes={'a'}))
    try:
        assert s._conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        # The formerly active rule is not silently stranded: it is visibly back to proposed.
        act = s.get('ract', c)
        assert act.payload['status'] == 'proposed'
        assert 'promoted_by' not in act.payload and 'promoted_at' not in act.payload
        assert set(s.migration_reapproval) == {'ract'}
        assert any(e['op'] == 'migrate_rule_reapproval' and e['record_id'] == 'ract'
                   for e in s.journal_entries(c))
        # Old decisions carry no approval; promotion is refused until a fresh one is recorded.
        for rid, did in (('ract', 'd1'), ('rprop', 'd2')):
            with pytest.raises((KnowledgeRefusal, ScopeViolation)):
                promote_rule(s, c, rid, did)
        for rid, did in (('ract', 'n1'), ('rprop', 'n2')):
            s.put(rec(did, 'Decision', payload={'rule_id': rid, 'approved': True}), c)
            assert promote_rule(s, c, rid, did).payload['status'] == 'active'
        assert s.fts_index_complete('a', c)
    finally:
        s.close()


def test_migration_is_idempotent_and_leaves_new_stores_alone(tmp_path):
    path = _head_store(tmp_path)
    Substrate(path, ScopeEngine(known_scopes={'a'})).close()
    c = ScopeContext(granted_scopes=frozenset({'a'}), user_authored=True)
    s = Substrate(path, ScopeEngine(known_scopes={'a'}))
    try:
        s.put(rec('n1', 'Decision', payload={'rule_id': 'ract', 'approved': True}), c)
        promote_rule(s, c, 'ract', 'n1')
    finally:
        s.close()
    s = Substrate(path, ScopeEngine(known_scopes={'a'}))
    try:
        assert s.get('ract', c).payload['status'] == 'active'
        assert s.migration_reapproval == []
    finally:
        s.close()


def test_failed_rule_migration_rolls_back(tmp_path):
    path = _head_store(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT,'boom'); END")
    conn.commit()
    conn.close()
    with pytest.raises(Exception):
        Substrate(path, ScopeEngine(known_scopes={'a'})).close()
    conn = sqlite3.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 6
    conn.execute('DROP TRIGGER boom')
    conn.commit()
    conn.close()
    Substrate(path, ScopeEngine(known_scopes={'a'})).close()


def test_schema6_export_still_imports_but_not_unknown_versions(env):
    s, c = env
    data = bundle(rec('x'))
    data['schema_version'] = 6
    assert s.import_scope(data, c) == 1
    # A version this build does not know is refused; 8 is importable now that it exists, so the
    # example has to be one the table does not list.
    data['schema_version'] = max(_IMPORTABLE_SCHEMA_VERSIONS) + 1
    with pytest.raises(ValueError):
        s.import_scope(data, c)


# ---- policy (b): imported approvals are not restored --------------------------------------

def test_import_restores_proposal_but_requires_fresh_approval(env, tmp_path):
    s, c = env
    s.put(rule(), c)
    s.put(rec('d', 'Decision', payload={'rule_id': 'r', 'approved': True}), c)
    data = s.export_scope('a', c)
    t = Substrate(tmp_path / 't.db', ScopeEngine(known_scopes={'a', 'b'}))
    try:
        assert t.import_scope(data, c) == 2
        assert bindings(t) == []
        with pytest.raises((KnowledgeRefusal, ScopeViolation)):
            promote_rule(t, c, 'r', 'd')
        t.put(rec('d3', 'Decision', payload={'rule_id': 'r', 'approved': True}), c)
        assert promote_rule(t, c, 'r', 'd3').payload['status'] == 'active'
    finally:
        t.close()
