import copy
import pytest
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate, SCHEMA_VERSION, ScopeViolation

@pytest.fixture
def store(tmp_path):
    s = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'a', 'b'}))
    c = ScopeContext(granted_scopes=frozenset({'a','b'}), user_authored=True)
    yield s,c
    s.close()

def rec(id, typ='Source', payload=None, **kw):
    return Record(record_id=id, record_type=typ, scope='a', epistemic_status='stated', payload=payload or {}, attribution='user', **kw)

def bundle(*rs):
    return {'schema_version': SCHEMA_VERSION, 'scope': 'a', 'records': [Substrate._record_to_export(r) for r in rs]}

def setup_rule(s,c):
    r=rec('r','ProposedRule',{'status':'proposed','description':'original'})
    p=rec('p','Decision',{'rule_id':'r','approved':True})
    s.put(r,c); s.put(p,c)
    return r,p

@pytest.mark.parametrize('path',['put','import'])
def test_activation_cannot_change_proposal(store,path):
    s,c=store; r,p=setup_rule(s,c)
    r.payload.update(status='active',promoted_by='p',description='changed')
    if path=='put':
        with pytest.raises(ScopeViolation): s.put(r,c,overwrite=True)
    else: assert s.import_scope(bundle(r),c)==0
    assert s.get('r',c).payload['status']=='proposed'

@pytest.mark.parametrize('path',['put','import'])
@pytest.mark.parametrize('kind',['expired','superseded'])
def test_inactive_promoter_cannot_activate(store,path,kind):
    s,c=store; r,p=setup_rule(s,c)
    if kind=='expired':
        p.valid_to=2; s.put(p,c,overwrite=True)
    else:
        s.correct('p',rec('fix','Correction',{'text':'revoked'}),c)
    r.payload.update(status='active',promoted_by='p')
    if path=='put':
        with pytest.raises(ScopeViolation): s.put(r,c,overwrite=True)
    else: assert s.import_scope(bundle(r),c)==0

@pytest.mark.parametrize('op',['delete','overwrite','correct','import'])
def test_active_promoter_is_immutable(store,op):
    s,c=store; r,p=setup_rule(s,c)
    r.payload.update(status='active',promoted_by='p'); s.put(r,c,overwrite=True)
    if op=='delete': assert not s.delete('p',c)
    elif op=='correct':
        with pytest.raises(ScopeViolation): s.correct('p',rec('fix','Correction',{'text':'revoked'}),c)
    elif op=='overwrite':
        p.payload={'rule_id':'other'}
        with pytest.raises(ScopeViolation): s.put(p,c,overwrite=True)
    else:
        p.payload={'rule_id':'other'}
        assert s.import_scope(bundle(p),c)==0
    assert s.get('p',c).payload['rule_id']=='r'

def test_bundle_scope_restricts_records(store):
    s,c=store; r=rec('x'); r.scope='b'
    assert s.import_scope(bundle(r),c)==0
    assert s.get('x',c) is None

def test_temporal_export_roundtrip(store,tmp_path):
    s,c=store; r=rec('x',valid_from=1,valid_to=5); s.put(r,c)
    data=s.export_scope('a',c)
    t=Substrate(tmp_path/'second',ScopeEngine(known_scopes={'a','b'}))
    try:
        assert t.import_scope(data,c)==1
        assert (t.get('x',c).valid_from,t.get('x',c).valid_to)==(1,5)
    finally: t.close()

def test_import_rejects_noncorrection_supersession(store):
    s,c=store; x=rec('x'); s.put(x,c); y=rec('y',supersedes='x')
    assert s.import_scope(bundle(y),c)==0

def test_import_rejects_self_supersession(store):
    s,c=store; x=rec('x'); s.put(x,c); x.supersedes='x'
    assert s.import_scope(bundle(x),c)==0

def test_import_invalidates_vector(store):
    s,c=store; x=rec('x',payload={'text':'old'}); s.put(x,c)
    s.vector_put('x','a',[1.0]+[0.0]*1023,c,model_id='m')
    assert s.vector_has_record('x','a',c,model_id='m')
    x.payload={'text':'new'}; assert s.import_scope(bundle(x),c)==1
    assert not s.vector_has_record('x','a',c,model_id='m')

@pytest.mark.parametrize('which',['claim','verification'])
def test_import_cannot_replace_verified_support(store,which):
    s,c=store; s.register_verifier('v',c)
    vc=ScopeContext(granted_scopes=c.granted_scopes,actor='v')
    v=Record(record_id='v1',record_type='Verification',scope='a',payload={'claim_id':'claim'},attribution='verifier')
    claim=Record(record_id='claim',record_type='Claim',scope='a',payload={'text':'x'},epistemic_status='verified',attribution='verifier',source_ids=['v1'])
    s.put(v,vc); s.put(claim,vc)
    if which=='claim':
        claim.epistemic_status='derived'; claim.attribution='agent'; target=claim
    else:
        v.payload={'claim_id':'other'}; target=v
    assert s.import_scope(bundle(target),vc)==0

def test_import_cannot_replace_foreign_incumbent(store):
    s,c=store; x=rec('x'); x.scope='b'; s.put(x,c)
    replacement=rec('x',payload={'text':'replacement'})
    assert s.import_scope(bundle(replacement),c)==0
    assert s.get('x',c).scope=='b'

def test_non_user_cannot_delete_active_rule(store):
    s,c=store; r,p=setup_rule(s,c)
    r.payload.update(status='active',promoted_by='p'); s.put(r,c,overwrite=True)
    agent=ScopeContext(granted_scopes=c.granted_scopes)
    assert not s.delete('r',agent)
    assert s.get('r',c) is not None

def test_import_rejects_cycle_over_existing_records(store):
    s,c=store; x=rec('x'); y=rec('y'); s.put(x,c); s.put(y,c)
    x.supersedes='y'; y.supersedes='x'
    assert s.import_scope(bundle(x,y),c)==0
    assert s.get('x',c).supersedes is None
    assert s.get('y',c).supersedes is None

def test_import_accepts_authorized_forward_correction(store):
    s,c=store; target=rec('x'); correction=rec('fix','Correction',{'text':'fixed'},supersedes='x')
    assert s.import_scope(bundle(correction,target),c)==2
    assert s.get('fix',c).supersedes=='x'

def test_import_rejects_agent_correction(store):
    s,c=store; s.put(rec('x'),c)
    agent=ScopeContext(granted_scopes=c.granted_scopes)
    correction=rec('fix','Correction',{'text':'fixed'},supersedes='x')
    correction.attribution='agent'
    assert s.import_scope(bundle(correction),agent)==0

def test_future_effective_promoter_cannot_activate(store):
    s,c=store; r,p=setup_rule(s,c)
    p.effective_at=99999999999; s.put(p,c,overwrite=True)
    r.payload.update(status='active',promoted_by='p')
    with pytest.raises(ScopeViolation): s.put(r,c,overwrite=True)
