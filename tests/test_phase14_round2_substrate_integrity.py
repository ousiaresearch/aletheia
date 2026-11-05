import copy
import pytest
from weft.scope import ScopeContext, ScopeEngine
from weft.substrate import Record, Substrate, SCHEMA_VERSION, SchemaViolation, ScopeViolation

@pytest.fixture
def store(tmp_path):
    sub = Substrate(tmp_path / 's.db', ScopeEngine(known_scopes={'a', 'b'}))
    ctx = ScopeContext(granted_scopes=frozenset({'a', 'b'}), user_authored=True)
    yield sub, ctx
    sub.close()

def rec(rid='r', **kw):
    return Record(record_id=rid, record_type='Source', scope='a', payload={'text':'hello'}, attribution='user', **kw)

def bundle(*records):
    return {'schema_version': SCHEMA_VERSION, 'scope':'a', 'records':[Substrate._record_to_export(r) for r in records]}

@pytest.mark.parametrize('path', ['put', 'import', 'put_no_schema'])
@pytest.mark.parametrize('field,value', [('record_id','../bad'), ('payload','bad'), ('provenance',[]), ('source_ids','bad'), ('source_ids',[1]), ('payload',{'nested':[float('nan')]}), ('provenance',{'nested':'\ud800'}), ('payload',{1:'bad'})])
def test_safe_record_validation(store,path,field,value):
    sub, ctx = store
    r = rec(); setattr(r,field,value)
    with pytest.raises(SchemaViolation):
        if path == 'import': sub.import_scope(bundle(r),ctx)
        else: sub.put(r,ctx,validate_schema=path != 'put_no_schema')
    assert not sub.all_records(ctx)


def test_old_approval_cannot_promote_recreated_proposal(store):
    from weft.procedural import promote_rule
    from weft.knowledge import KnowledgeRefusal
    sub, ctx = store
    rule = Record(record_id="rule", record_type="ProposedRule", scope="a", payload={"status":"proposed", "description":"original"}, attribution="agent")
    sub.put(rule, ctx)
    sub.put(Record(record_id="approval", record_type="Decision", scope="a", payload={"rule_id":"rule"}, attribution="user"), ctx)
    assert sub.delete("rule", ctx)
    rule.payload["description"] = "changed"
    sub.put(rule, ctx)
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        promote_rule(sub,ctx,"rule")
    assert sub.get("rule",ctx).payload["status"] == "proposed"


def test_rejected_import_rolls_back_whole_bundle(store):
    sub, ctx = store
    active = Record(record_id="rule", record_type="ProposedRule", scope="a", payload={"status":"active", "promoted_by":"fake"}, attribution="agent")
    assert sub.import_scope(bundle(rec("good"), active),ctx) == 0
    assert sub.get("good",ctx) is None
    assert sub.get("rule",ctx) is None


def test_imported_approval_cannot_inherit_prior_binding(store):
    from weft.procedural import promote_rule
    from weft.knowledge import KnowledgeRefusal
    sub, ctx = store
    rule = Record(record_id="rule", record_type="ProposedRule", scope="a", payload={"status":"proposed"}, attribution="agent")
    sub.put(rule, ctx)
    approval = Record(record_id="approval", record_type="Decision", scope="a", payload={"rule_id":"rule"}, attribution="user")
    sub.put(approval,ctx)
    approval.payload["text"] = "not approved"
    assert sub.import_scope(bundle(approval),ctx) == 1
    with pytest.raises((KnowledgeRefusal, ScopeViolation)):
        promote_rule(sub,ctx,"rule")


def test_corrected_rule_cannot_be_activated_by_direct_write(store):
    sub, ctx = store
    rule = Record(record_id="rule", record_type="ProposedRule", scope="a", payload={"status":"proposed"}, attribution="agent")
    sub.put(rule,ctx)
    sub.put(Record(record_id="approval", record_type="Decision", scope="a", payload={"rule_id":"rule"}, attribution="user"),ctx)
    sub.correct("rule", Record(record_id="correction", record_type="Correction", scope="a", payload={"text":"withdrawn"}, attribution="user"),ctx)
    active = copy.deepcopy(rule); active.payload.update(status="active",promoted_by="approval")
    with pytest.raises(ScopeViolation): sub.put(active,ctx,overwrite=True)


@pytest.mark.parametrize("path", ["delete", "prune"])
def test_removal_of_derived_clears_supersession(store,path):
    sub,ctx=store
    sub.put(rec("source"),ctx)
    sub.put(Record(record_id="summary", record_type="Summary", scope="a", payload={"text":"summary"}, source_ids=["source"], epistemic_status="derived", attribution="system"),ctx)
    sub.correct("summary",Record(record_id="correction", record_type="Correction", scope="a", payload={"text":"fixed"}, attribution="user"),ctx)
    if path == "delete": assert sub.delete("source",ctx)
    else: assert sub.prune_derived("a",float("inf"),ctx) == 1
    assert sub.get("summary",ctx) is None
    assert sub.get("correction",ctx).supersedes is None


def test_fts_rank_ignores_unreadable_corpus(store):
    sub,ctx=store
    a = ScopeContext(granted_scopes=frozenset({"a"}), user_authored=True)
    sub.put(Record(record_id="first", record_type="Source", scope="a", payload={"text":"harbor report harbor report harbor"}, attribution="user"),ctx)
    sub.put(Record(record_id="second", record_type="Source", scope="a", payload={"text":"Zanzibar harbor note"}, attribution="user"),ctx)
    before = sub.fts_candidates("Zanzibar OR harbor",a,10,scopes={"a"})
    assert before == ["second","first"]
    for i in range(3):
        sub.put(Record(record_id=f"hidden{i}", record_type="Source", scope="b", payload={"text":f"Zanzibar secret dossier {i}"}, attribution="user"),ctx)
    assert sub.fts_candidates("Zanzibar OR harbor",a,10,scopes={"a"}) == before


def test_rule_correction_remains_withdrawn_after_correction_deleted(store):
    sub, ctx = store
    rule = Record(record_id="rule", record_type="ProposedRule", scope="a", payload={"status":"proposed"}, attribution="agent")
    sub.put(rule,ctx)
    sub.put(Record(record_id="approval", record_type="Decision", scope="a", payload={"rule_id":"rule"}, attribution="user"),ctx)
    sub.correct("rule",Record(record_id="correction", record_type="Correction", scope="a", payload={"text":"withdrawn"}, attribution="user"),ctx)
    assert sub.delete("correction",ctx)
    from weft.procedural import promote_rule
    from weft.knowledge import KnowledgeRefusal
    with pytest.raises((KnowledgeRefusal, ScopeViolation)): promote_rule(sub,ctx,"rule")


def test_imported_rule_correction_cannot_withdraw_existing_rule(store):
    sub,ctx=store
    rule = Record(record_id="rule", record_type="ProposedRule", scope="a", payload={"status":"proposed"}, attribution="agent")
    sub.put(rule,ctx)
    correction = Record(record_id="corr",record_type="Correction",scope="a",payload={"text":"withdrawn"},attribution="user",supersedes="rule")
    # Round 6 policy: an import never transitions an existing rule, not even via a Correction.
    assert sub.import_scope(bundle(correction),ctx)==0
    assert sub.get("rule",ctx).payload["status"] == "proposed"
    assert sub.get("corr",ctx) is None


@pytest.mark.parametrize("path", ["put", "import"])
def test_id_trailing_newline_rejected(store,path):
    sub,ctx=store
    r=rec("valid\n")
    with pytest.raises(SchemaViolation):
        if path == "put": sub.put(r,ctx)
        else: sub.import_scope(bundle(r),ctx)
