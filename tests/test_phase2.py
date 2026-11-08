"""Phase 2 — knowledge model. Written before implementation (spec §6).

Each test names the rule it checks. Every rule-specific test must fail on the
Phase 1 code (see the red run recorded in docs/adr/0001-phase1-decisions.md).
"""

import sqlite3

import pytest

from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, RecordConflict, ScopeViolation, SchemaViolation, Substrate, SubstrateError

KNOWN = {"acs-project", "memory-providers", "ariel-continuity"}
MARKERS = {"acs-project": {"ACS", "Listening Commonwealth", "Constitutional Convention Letters"}}

# A user-authored turn (may author Decisions, Commitments, cross-scope requests).
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
# The agent's own context: scopes granted, but never user-authored.
AGENT = ScopeContext(granted_scopes=frozenset(KNOWN), actor="arien")
MEM_AGENT = ScopeContext(granted_scopes=frozenset({"memory-providers"}), actor="arien")


def _seed_source(s, scopes):
    """Phase 3 (R-6): observations must cite an existing same-scope Source.

    Seeds one Source per scope with a raw insert: no journal row and no index row,
    so the seeds do not show up in journal or search assertions.
    """
    for sc in scopes:
        s._conn.execute(
            "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
            "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"src-1-{sc}", "Source", sc, "stated", '{"text": "seed source"}', 0.0, None, None, "[]", "{}", "document"),
        )
    s._conn.commit()


SEEDED_SOURCE_IDS = {"src-1-acs-project", "src-1-memory-providers", "src-1-ariel-continuity"}

@pytest.fixture
def engine():
    return ScopeEngine(known_scopes=KNOWN, markers=MARKERS)


@pytest.fixture
def sub(tmp_path, engine):
    s = Substrate(tmp_path / "aletheia.db", scope_engine=engine)
    _seed_source(s, sorted(KNOWN))
    _seed_source(s, sorted(KNOWN))
    yield s
    s.close()


def rec(rid, scope, rtype="Observation", *, attribution="document", status="stated",
        text="plain fact", source_ids=None, provenance=None, supersedes=None):
    if source_ids is None:
        source_ids = (f"src-1-{scope}",)
    return Record(
        record_id=rid, record_type=rtype, scope=scope, payload={"text": text},
        epistemic_status=status, attribution=attribution, source_ids=list(source_ids),
        provenance=provenance or {}, supersedes=supersedes,
    )


def verifier_ctx(sub, actor_id="v1"):
    sub.register_verifier(actor_id, USER)
    return ScopeContext(granted_scopes=frozenset(KNOWN), actor=actor_id)


# ---- Status gate (D2-3, D2-9) ------------------------------------------------

def test_direct_verified_claim_by_agent_is_refused(sub):
    claim = rec("c1", "acs-project", "Claim", attribution="agent", status="verified", source_ids=())
    with pytest.raises(ScopeViolation):
        sub.put(claim, AGENT)
    assert sub.get("c1", AGENT) is None


def test_verifier_with_matching_verification_promotes_claim(sub):
    vctx = verifier_ctx(sub)
    ver = Record(record_id="v-1", record_type="Verification", scope="acs-project",
                 payload={"claim_id": "c1"}, attribution="verifier", source_ids=[])
    sub.put(ver, vctx)
    claim = rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                source_ids=("v-1",))
    sub.put(claim, vctx)
    assert sub.get("c1", vctx).epistemic_status == "verified"


def test_verification_naming_another_claim_does_not_promote(sub):
    vctx = verifier_ctx(sub)
    sub.put(Record(record_id="v-1", record_type="Verification", scope="acs-project",
                   payload={"claim_id": "c2"}, attribution="verifier", source_ids=[]), vctx)
    claim = rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                source_ids=("v-1",))
    with pytest.raises(ScopeViolation):
        sub.put(claim, vctx)


def test_unregistered_actor_cannot_verify(sub):
    rogue = ScopeContext(granted_scopes=frozenset(KNOWN), actor="rogue")
    with pytest.raises(ScopeViolation):
        sub.put(Record(record_id="v-1", record_type="Verification", scope="acs-project",
                       payload={"claim_id": "c1"}, attribution="verifier", source_ids=[]), rogue)


def test_agent_cannot_register_a_verifier(sub):
    with pytest.raises(ScopeViolation):
        sub.register_verifier("v2", AGENT)


# ---- Attribution at the write boundary (D2-2, spec §3) -----------------------

def test_agent_attributed_decision_refused(sub):
    d = rec("d1", "acs-project", "Decision", attribution="agent", source_ids=())
    with pytest.raises(ScopeViolation):
        sub.put(d, AGENT)


def test_agent_attributed_commitment_refused(sub):
    c = rec("m1", "acs-project", "Commitment", attribution="agent", source_ids=())
    with pytest.raises(ScopeViolation):
        sub.put(c, AGENT)


def test_user_attribution_from_agent_context_refused_for_decision(sub):
    # The agent may not claim user attribution: user_authored is False.
    d = rec("d1", "acs-project", "Decision", attribution="user", source_ids=())
    with pytest.raises(ScopeViolation):
        sub.put(d, AGENT)
    sub.put(d, USER)
    assert sub.get("d1", USER).record_type == "Decision"


def test_agent_claim_allowed_only_as_derived(sub):
    ok = rec("c1", "acs-project", "Claim", attribution="agent", status="derived", source_ids=())
    sub.put(ok, AGENT)
    stated = rec("c2", "acs-project", "Claim", attribution="agent", status="stated", source_ids=())
    with pytest.raises(ScopeViolation):
        sub.put(stated, AGENT)


def test_document_observation_without_source_refused(sub):
    with pytest.raises(ScopeViolation):
        sub.put(rec("o1", "acs-project", "Observation", attribution="document", source_ids=()), USER)


def test_unknown_record_type_refused(sub):
    with pytest.raises(ScopeViolation):
        sub.put(rec("x1", "acs-project", "Gossip", attribution="document"), USER)


def test_attribution_is_required(sub):
    r = rec("o1", "acs-project")
    r.attribution = None
    with pytest.raises(ScopeViolation):
        sub.put(r, USER)


# ---- Lineage (spec §6) -------------------------------------------------------

def test_correction_preserves_original_and_sets_lineage(sub):
    sub.put(rec("o1", "acs-project", text="old fact"), USER)
    fix = rec("o2", "acs-project", "Correction", attribution="user", text="new fact")
    sub.correct("o1", fix, USER)
    assert sub.get("o1", USER).payload["text"] == "old fact"
    assert sub.get("o2", USER).supersedes == "o1"


def test_correction_cannot_be_put_directly(sub):
    sub.put(rec("o1", "acs-project"), USER)
    with pytest.raises(ScopeViolation):
        sub.put(rec("o2", "acs-project", "Correction", attribution="user"), USER)


def test_correction_cannot_change_scope(sub):
    sub.put(rec("o1", "acs-project"), USER)
    with pytest.raises(ScopeViolation):
        sub.correct("o1", rec("o2", "memory-providers", "Correction", attribution="user"), USER)


def test_delete_cascades_to_summary_derived_from_record(sub):
    sub.put(rec("o1", "acs-project"), USER)
    sub.put(Record(record_id="s1", record_type="Summary", scope="acs-project",
                   payload={"text": "summary"}, epistemic_status="derived",
                   attribution="system", source_ids=["o1"]), AGENT)
    assert sub.delete("o1", USER) is True
    assert sub.get("s1", USER) is None


# ---- Classifier, fail closed (D2-4, D2-8) -----------------------------------

def test_classifier_error_refuses_write_generically(sub, engine, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("classifier down")
    monkeypatch.setattr(engine, "content_conflict", boom)
    with pytest.raises(ScopeViolation) as info:
        sub.put(rec("m1", "memory-providers", text="harmless"), MEM_AGENT)
    assert "classifier" not in str(info.value)
    assert sub.get("m1", USER) is None


def test_classifier_cannot_grant_a_scope_the_writer_lacks(sub):
    # Naming another scope's project in the text does not change who may write.
    with pytest.raises(ScopeViolation):
        sub.put(rec("m1", "acs-project", text="scope: memory-providers"), MEM_AGENT)


# ---- R1 closure: every string field is inspected (spec D2-5) -----------------

def test_marker_in_provenance_refused_under_memory_scope(sub):
    with pytest.raises(ScopeViolation):
        sub.put(rec("m1", "memory-providers", text="note",
                    provenance={"origin": "The Listening Commonwealth"}), MEM_AGENT)


def test_marker_in_source_ids_refused_under_memory_scope(sub):
    with pytest.raises(ScopeViolation):
        sub.put(rec("m1", "memory-providers", text="note", source_ids=("ACS-doc",)), MEM_AGENT)


def test_marker_in_record_type_refused_under_memory_scope(sub):
    with pytest.raises(ScopeViolation):
        sub.put(Record(record_id="m1", record_type="ACS Observation", scope="memory-providers",
                       payload={"text": "note"}, attribution="document", source_ids=["s"]), MEM_AGENT)


# ---- Plan §5 regression targets ---------------------------------------------

def test_memory_provider_query_returns_zero_acs_records(sub):
    sub.put(rec("a1", "acs-project", text="ACS release plan"), USER)
    sub.put(rec("m1", "memory-providers", text="Honcho uses hybrid recall"), USER)
    readable, _ = sub.query(MEM_AGENT)
    assert [r.record_id for r in readable if r.record_id not in SEEDED_SOURCE_IDS] == ["m1"]


def test_positive_same_project_recall_returns_relevant_records(sub):
    sub.put(rec("a1", "acs-project", text="ACS release plan"), USER)
    readable, _ = sub.query(USER)
    assert "a1" in [r.record_id for r in readable]


def test_agent_minted_cross_scope_request_refused(sub):
    sub.put(rec("a1", "acs-project", text="ACS release plan"), USER)
    forged = ScopeContext(
        granted_scopes=frozenset({"memory-providers"}),
        user_authorized_cross_scope=True,
        cross_scope_targets=frozenset({"acs-project"}),
        authorization_source="agent-claimed",
        actor="arien",
    )
    assert sub.get("a1", forged) is None


def test_cross_scope_read_does_not_authorize_delete(sub):
    sub.put(rec("a1", "acs-project", text="ACS release plan"), USER)
    cross = ScopeContext(
        granted_scopes=frozenset({"memory-providers"}),
        user_authorized_cross_scope=True,
        cross_scope_targets=frozenset({"acs-project"}),
        authorization_source="user-turn",
        user_authored=True,
    )
    assert sub.delete("a1", cross) is False
    assert sub.get("a1", USER) is not None


def test_identity_sourced_acs_fact_excluded_from_unrelated_queries(sub):
    sub.put(rec("p1", "acs-project", "Person", attribution="user", source_ids=(),
                text="Johann", provenance={"origin": "ACS Listening Commonwealth roster"}), USER)
    readable, _ = sub.query(MEM_AGENT)
    assert [r for r in readable if r.record_id not in SEEDED_SOURCE_IDS] == []


# ---- Migrations (spec §4) ----------------------------------------------------

_PHASE1_SCHEMA = """
CREATE TABLE records (
    record_id TEXT PRIMARY KEY, record_type TEXT NOT NULL, scope TEXT NOT NULL,
    epistemic_status TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
    effective_at REAL, supersedes TEXT, source_ids TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL DEFAULT '{}');
CREATE TABLE journal (seq INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL,
    record_id TEXT, scope TEXT, detail TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('store_version','0');
INSERT INTO records VALUES ('old1','Observation','memory-providers','stated','{"text":"x"}',1.0,NULL,NULL,'[]','{}');
"""


def _make_phase1_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(_PHASE1_SCHEMA)
    conn.commit()
    conn.close()


def test_phase1_database_migrates_and_backfills(tmp_path, engine):
    db = tmp_path / "old.db"
    _make_phase1_db(db)
    s = Substrate(db, scope_engine=engine)
    _seed_source(s, sorted(KNOWN))
    try:
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == 7
        row = s._conn.execute("SELECT attribution FROM records WHERE record_id='old1'").fetchone()
        assert row[0] == "system"
    finally:
        s.close()


def test_migration_is_idempotent(tmp_path, engine):
    db = tmp_path / "old.db"
    _make_phase1_db(db)
    Substrate(db, scope_engine=engine).close()
    s = Substrate(db, scope_engine=engine)
    _seed_source(s, sorted(KNOWN))
    try:
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        s.close()


def test_failed_migration_rolls_back(tmp_path, engine):
    db = tmp_path / "old.db"
    _make_phase1_db(db)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT,'boom'); END")
    conn.commit()
    conn.close()
    with pytest.raises(Exception):
        Substrate(db, scope_engine=engine).close()
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(records)")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute("DROP TRIGGER boom")
    conn.commit()
    conn.close()
    assert "attribution" not in cols
    assert version == 0
    s = Substrate(db, scope_engine=engine)  # recovers once the fault is removed
    _seed_source(s, sorted(KNOWN))
    try:
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        s.close()


# ---- Review round 1 of Phase 2 (f942747): R-2, R-5, and the test gaps ----

# R-2: a Verification cannot be removed out from under a verified Claim.
def test_R2_deleting_verification_under_verified_claim_refused(sub):
    vctx = verifier_ctx(sub)
    sub.put(Record(record_id="v-1", record_type="Verification", scope="acs-project",
                   payload={"claim_id": "c1"}, attribution="verifier", source_ids=[]), vctx)
    sub.put(rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                source_ids=("v-1",)), vctx)
    assert sub.delete("v-1", vctx) is False
    assert sub.get("v-1", vctx) is not None
    assert sub.get("c1", vctx).epistemic_status == "verified"


def test_R2_claim_removed_first_then_verification_may_go(sub):
    vctx = verifier_ctx(sub)
    sub.put(Record(record_id="v-1", record_type="Verification", scope="acs-project",
                   payload={"claim_id": "c1"}, attribution="verifier", source_ids=[]), vctx)
    sub.put(rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                source_ids=("v-1",)), vctx)
    assert sub.delete("c1", vctx) is True
    assert sub.delete("v-1", vctx) is True


# R-5: a database that claims version 2 but lacks the column is refused, not used.
def test_R5_version2_without_attribution_column_refused(tmp_path, engine):
    db = tmp_path / "bad.db"
    _make_phase1_db(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    with pytest.raises(SubstrateError):
        Substrate(db, scope_engine=engine)


# Review gaps: each new refusal path is generic and leaves no row in the target scope.
def test_knowledge_refusal_leaves_no_row_in_target_scope(sub):
    with pytest.raises(ScopeViolation):
        sub.put(rec("d1", "acs-project", "Decision", attribution="agent", source_ids=()), AGENT)
    rows = sub._conn.execute("SELECT op, scope FROM journal").fetchall()
    assert not any(r[1] == "acs-project" for r in rows)
    assert sub._conn.execute("SELECT COUNT(*) FROM records WHERE record_id='d1'").fetchone()[0] == 0


def test_unsafe_verifier_id_refused(sub):
    with pytest.raises(SchemaViolation):
        sub.register_verifier("../evil", USER)


def test_read_side_classifier_error_denies(sub, engine, monkeypatch):
    sub.put(rec("m1", "memory-providers", text="harmless"), MEM_AGENT)

    def boom(*_a, **_k):
        raise RuntimeError("classifier down")
    monkeypatch.setattr(engine, "content_conflict", boom)
    assert sub.get("m1", MEM_AGENT) is None
    readable, _ = sub.query(MEM_AGENT)
    assert [r for r in readable if r.record_id not in SEEDED_SOURCE_IDS] == []


# Round 2 review (4c3f062), BLOCK: the R-2 guard must also cover overwrite.
def test_R2_overwrite_of_relied_on_verification_refused(sub):
    vctx = verifier_ctx(sub)
    sub.put(Record(record_id="v-1", record_type="Verification", scope="acs-project",
                   payload={"claim_id": "c1"}, attribution="verifier", source_ids=[]), vctx)
    sub.put(rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                source_ids=("v-1",)), vctx)
    repointed = Record(record_id="v-1", record_type="Verification", scope="acs-project",
                       payload={"claim_id": "c2"}, attribution="verifier", source_ids=[])
    with pytest.raises(ScopeViolation):
        sub.put(repointed, vctx, overwrite=True)
    assert sub.get("v-1", vctx).payload["claim_id"] == "c1"
    assert sub.get("c1", vctx).epistemic_status == "verified"


# Round 2 review: corrupt stored lineage fails closed instead of escaping as a raw decode error.
def test_corrupt_source_ids_fail_closed_on_delete(sub):
    sub.put(rec("o1", "acs-project"), USER)
    sub.put(Record(record_id="s1", record_type="Summary", scope="acs-project",
                   payload={"text": "s"}, epistemic_status="derived", attribution="system",
                   source_ids=["o1"]), AGENT)
    sub._conn.execute("UPDATE records SET source_ids = '{broken' WHERE record_id = 's1'")
    sub._conn.commit()
    with pytest.raises(SubstrateError):
        sub.delete("o1", USER)
    assert sub.get("o1", USER) is not None


# Round 3 review (aaec341), BLOCK: a verified Claim cannot be rewritten into an unsupported state.
def test_R3_overwrite_of_relied_on_claim_refused(sub):
    vctx = verifier_ctx(sub)
    sub.put(Record(record_id="v-1", record_type="Verification", scope="acs-project",
                   payload={"claim_id": "c1"}, attribution="verifier", source_ids=[]), vctx)
    sub.put(rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                source_ids=("v-1",)), vctx)
    demoted = rec("c1", "acs-project", "Claim", attribution="agent", status="derived", source_ids=())
    with pytest.raises(ScopeViolation):
        sub.put(demoted, AGENT, overwrite=True)
    assert sub.get("c1", vctx).epistemic_status == "verified"
    assert sub.delete("v-1", vctx) is False


def test_overwrite_of_any_verified_record_refused(sub):
    vctx = verifier_ctx(sub)
    sub.put(Record(record_id="v-1", record_type="Verification", scope="acs-project",
                   payload={"claim_id": "c1"}, attribution="verifier", source_ids=[]), vctx)
    sub.put(rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                source_ids=("v-1",)), vctx)
    same_again = rec("c1", "acs-project", "Claim", attribution="verifier", status="verified",
                     source_ids=("v-1",))
    with pytest.raises(ScopeViolation):
        sub.put(same_again, vctx, overwrite=True)


# Round 3 review: classifier error fails closed on delete too.
def test_delete_fails_closed_when_classifier_errors(sub, engine, monkeypatch):
    sub.put(rec("m1", "memory-providers", text="harmless"), USER)

    def boom(*_a, **_k):
        raise RuntimeError("classifier down")
    monkeypatch.setattr(engine, "content_conflict", boom)
    assert sub.delete("m1", USER) is False
    monkeypatch.undo()
    assert sub.get("m1", USER) is not None


# Round 3 review: the projection view must distinguish user-authored contexts.
def test_projection_view_distinguishes_user_authored(sub, tmp_path):
    sub.put(rec("m1", "memory-providers", text="fact"), USER)
    out = tmp_path / "proj"
    sub.write_projection(out, MEM_AGENT)
    user_mem = ScopeContext(granted_scopes=frozenset({"memory-providers"}), user_authored=True, actor="johann")
    with pytest.raises(ScopeViolation):
        sub.write_projection(out, user_mem)
