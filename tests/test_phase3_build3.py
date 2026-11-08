"""Phase 3 review round 1 fixes (review f1cedd4, G-P3-1 and G-P3-2). Tests first.

fts_candidates, job_state, set_job and record_receipt are public paths and must
consult the Phase 1 gate. A caller holding only memory-providers must not learn
ids, state, or counts for acs-project.
"""

import pytest

from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import ScopeViolation, Substrate

KNOWN = {"acs-project", "memory-providers"}
MARKERS = {"acs-project": {"ACS", "Listening Commonwealth"}}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
MEM = ScopeContext(granted_scopes=frozenset({"memory-providers"}), actor="arien")


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "aletheia.db", scope_engine=ScopeEngine(known_scopes=KNOWN, markers=MARKERS))
    yield s
    s.close()


@pytest.fixture
def acs_source(sub):
    return Ingestor(sub).ingest_turn("the quasar anchor is in the ACS archive", "user", USER, "acs-project", "c1")


def test_fts_candidates_hides_ids_from_unreadable_scope(sub, acs_source):
    assert sub.fts_candidates("quasar", MEM, 10) == []


def test_fts_candidates_returns_readable_ids(sub, acs_source):
    assert acs_source in sub.fts_candidates("quasar", USER, 10)


def test_fts_candidates_without_context_is_empty(sub, acs_source):
    assert sub.fts_candidates("quasar", None, 10) == []


def test_set_job_forging_in_unwritable_scope_refused(sub, acs_source):
    with pytest.raises(ScopeViolation):
        sub.set_job(acs_source, "ingest", "h", "done", scope="acs-project", ctx=MEM)
    assert sub.job_state(acs_source, "ingest", "h", scope="acs-project", ctx=USER) is None


def test_job_state_reveals_nothing_to_unreadable_scope(sub, acs_source):
    sub.set_job(acs_source, "ingest", "h", "done", scope="acs-project", ctx=USER)
    assert sub.job_state(acs_source, "ingest", "h", scope="acs-project", ctx=MEM) is None
    assert sub.job_state(acs_source, "ingest", "h", scope="acs-project", ctx=USER) == "done"


def test_job_state_unknown_source_is_none_for_everyone(sub):
    assert sub.job_state("no-such-source", "ingest", "h", scope="acs-project", ctx=USER) is None


def test_receipt_cannot_name_ungranted_scope(sub):
    with pytest.raises(ScopeViolation):
        sub.record_receipt("recall", ["acs-project"], {"selected": 0}, MEM)


def test_receipt_requires_context(sub):
    with pytest.raises(ScopeViolation):
        sub.record_receipt("recall", ["memory-providers"], {"selected": 0}, None)


def test_receipt_for_granted_scope_is_written(sub):
    sub.record_receipt("recall", ["memory-providers"], {"selected": 0}, MEM)
