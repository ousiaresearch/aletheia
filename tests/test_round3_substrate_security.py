import pytest
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate, ScopeViolation, SchemaViolation, SCHEMA_VERSION
from weft.knowledge import KnowledgeRefusal
from weft.procedural import promote_rule

@pytest.fixture
def env(tmp_path):
    s = Substrate(tmp_path/'s.db', ScopeEngine(known_scopes={'a','b'}))
    c = ScopeContext(granted_scopes=frozenset({'a','b'}), user_authored=True)
    yield s,c
    s.close()

def record(id, typ='Source', scope='a', **kw):
    return Record(record_id=id, record_type=typ, scope=scope, payload=kw.pop('payload', {'text':'content'}), attribution=kw.pop('attribution','user'), **kw)

def bundle(*rs):
    return {'scope':'a', 'schema_version':SCHEMA_VERSION, 'records':[Substrate._record_to_export(r) for r in rs]}

@pytest.mark.parametrize('payload', [{'rule_id':'r','text':'I reject this rule'}, {'rule_id':'r','approved':True,'text':'I reject this rule'}, {'rule_id':'r'}, {'rule_id':'r','approved':False}, {'rule_id':'other','approved':True}])
def test_no_implicit_or_wrong_target_approval(env,payload):
    s,c=env
    s.put(record('r','ProposedRule',payload={'status':'proposed'},attribution='agent'),c)
    s.put(record('d','Decision',payload=payload),c)
    with pytest.raises((KnowledgeRefusal,ScopeViolation)): promote_rule(s,c,'r','d')

@pytest.mark.parametrize('correction_path', ['native', 'import'])
@pytest.mark.parametrize('removal', ['delete', 'overwrite', 'import'])
def test_withdrawal_survives_removal_of_correction(env, correction_path, removal):
    s,c=env
    s.put(record('r','ProposedRule',payload={'status':'proposed'},attribution='agent'),c)
    s.put(record('d','Decision',payload={'rule_id':'r','approved':True}),c)
    corr=record('fix','Correction',payload={'text':'withdraw'})
    if correction_path == 'native': s.correct('d',corr,c)
    else:
        corr.supersedes='d'
        assert s.import_scope(bundle(corr),c)==1
    with pytest.raises((KnowledgeRefusal,ScopeViolation)): promote_rule(s,c,'r','d')
    agent=ScopeContext(granted_scopes=frozenset({'a'}))
    if removal == 'delete': assert s.delete('fix',agent)
    elif removal == 'overwrite': s.put(record('fix',attribution='agent'),agent,overwrite=True)
    else: assert s.import_scope(bundle(record('fix',attribution='agent')),agent)==1
    with pytest.raises((KnowledgeRefusal,ScopeViolation)): promote_rule(s,c,'r','d')

@pytest.mark.parametrize('path', ['put','put_no_schema','import'])
@pytest.mark.parametrize('field,value', [('payload',{'text':123}), ('created_at',True), ('created_at',None), ('effective_at',float('inf')), ('valid_from','tomorrow'), ('valid_to',float('nan'))])
def test_invalid_text_and_timestamps_refused(env,path,field,value):
    s,c=env
    r=record('bad'); setattr(r,field,value)
    with pytest.raises(SchemaViolation):
        if path=='import': s.import_scope(bundle(record('good'),r),c)
        else: s.put(r,c,validate_schema=path=='put')
    assert not s.all_records(c)

@pytest.mark.parametrize('path',['put','import'])
def test_foreign_summary_cannot_obstruct_deletion(env,path):
    s,c=env
    s.put(record('source'),c)
    foreign=record('foreign','Summary',scope='b',attribution='system',epistemic_status='derived',source_ids=['source'])
    b=ScopeContext(granted_scopes=frozenset({'b'}))
    if path=='put':
        with pytest.raises(ScopeViolation): s.put(foreign,b)
    else:
        data=bundle(foreign); data['scope']='b'
        assert s.import_scope(data,b)==0
    a=ScopeContext(granted_scopes=frozenset({'a'}))
    assert s.delete('source',a)

def test_transitive_derived_deletion_removes_searchable_content(env):
    s,c=env
    s.put(record('source'),c)
    s.put(record('s1','Summary',attribution='system',epistemic_status='derived',source_ids=['source']),c)
    s.put(record('s2','Summary',attribution='system',epistemic_status='derived',source_ids=['s1']),c)
    s.put(record('obs','Observation',attribution='document',source_ids=['s1']),c)
    s.put(record('claim','Claim',attribution='agent',epistemic_status='derived',source_ids=['source']),c)
    assert set(s.fts_candidates('content',c,20,scopes={'a'})) == {'source','s1','s2','obs','claim'}
    assert s.delete('source',c)
    assert not s.all_records(c)
    assert not s.fts_candidates('content',c,20,scopes={'a'})

@pytest.mark.parametrize('path',['put','import'])
def test_foreign_claim_citation_refused(env,path):
    s,c=env
    s.put(record('source'),c)
    foreign=record('foreign','Claim',scope='b',attribution='agent',epistemic_status='derived',source_ids=['source'])
    b=ScopeContext(granted_scopes=frozenset({'b'}))
    if path=='put':
        with pytest.raises(ScopeViolation): s.put(foreign,b)
    else:
        data=bundle(foreign); data['scope']='b'
        assert s.import_scope(data,b)==0

def test_structured_approval_works(env):
    s,c=env
    s.put(record('r','ProposedRule',payload={'status':'proposed'},attribution='agent'),c)
    s.put(record('d','Decision',payload={'rule_id':'r','approved':True}),c)
    assert promote_rule(s,c,'r','d').payload['status']=='active'


def test_import_orders_source_dependencies(env):
    s,c=env
    obs=record('obs','Observation',attribution='document',source_ids=['src'])
    assert s.import_scope(bundle(obs,record('src')),c)==2
    assert s.get('obs',c).source_ids == ['src']

@pytest.mark.parametrize('records', [
    lambda: [record('a','Summary',attribution='system',epistemic_status='derived',source_ids=['b']), record('b','Summary',attribution='system',epistemic_status='derived',source_ids=['a'])],
    lambda: [record('same'), record('same')],
])
def test_import_refuses_cycles_and_duplicate_ids_atomically(env,records):
    s,c=env
    assert s.import_scope(bundle(record('ok'), *records()),c)==0
    assert s.get('ok',c) is None


def test_delete_refuses_verified_transitive_dependency(env):
    s,c=env
    s.register_verifier('v',c)
    vc=ScopeContext(granted_scopes=c.granted_scopes,actor='v')
    s.put(record('src'),c)
    s.put(record('middle','Summary',attribution='system',epistemic_status='derived',source_ids=['src']),c)
    s.put(record('verify','Verification',attribution='verifier',payload={'claim_id':'claim'}),vc)
    s.put(record('claim','Claim',attribution='verifier',epistemic_status='verified',source_ids=['verify','middle']),vc)
    assert s.get('claim',c) and s.get('middle',c)
    assert not s.delete('src',c)
    assert s.get('src',c) and s.get('middle',c) and s.get('claim',c)


def test_cascade_refuses_unreadable_same_scope_descendant(env):
    s,c=env
    s.put(record('src'),c)
    s.put(record('hidden','Summary',attribution='system',epistemic_status='derived',source_ids=['src'],payload={'text':'secretmarker'}),c)
    assert s.get('hidden',c) is not None
    s.scope_engine.register_scope('b', {'secretmarker'})
    a=ScopeContext(granted_scopes={'a'},user_authored=True)
    assert s.get('hidden',a) is None
    assert not s.delete('src',a)
    assert s.get('src',a) is not None

def test_cascade_sql_failure_rolls_back_all_records(env):
    import sqlite3
    s,c=env
    s.put(record('src'),c)
    s.put(record('mid','Summary',attribution='system',epistemic_status='derived',source_ids=['src']),c)
    s.put(record('end','Summary',attribution='system',epistemic_status='derived',source_ids=['mid']),c)
    s._conn.execute("CREATE TRIGGER fail_cascade BEFORE DELETE ON records WHEN OLD.record_id='src' BEGIN SELECT RAISE(ABORT, 'fault'); END")
    with pytest.raises(sqlite3.IntegrityError): s.delete('src',c)
    assert {r.record_id for r in s.all_records(c)} == {'src','mid','end'}
    assert s.fts_index_complete('a',c)
    s._conn.execute('DROP TRIGGER fail_cascade')
    assert s.delete('src',c)
    assert not s.all_records(c)


def test_strict_restore_reports_refusal_without_partial_write(env):
    s,c=env
    active=record('r','ProposedRule',attribution='agent',payload={'status':'active','promoted_by':'d'})
    with pytest.raises(KnowledgeRefusal, match='active'):
        s.import_scope(bundle(record('ok'), active),c,strict=True)
    assert s.get('ok',c) is None
    assert s.import_scope(bundle(record('ok')),c,strict=True)==1
