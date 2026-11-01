"""Phase 13 — Procedural memory tests."""
from __future__ import annotations

import time

import pytest

from weft.knowledge import KnowledgeRefusal
from weft.procedural import (
    STATUS_ACTIVE,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_DEPRECATED,
    list_active_rules,
    propose_rule,
    promote_rule,
    reject_rule,
    deprecate_rule,
)
from weft.scope import ScopeContext
from weft.substrate import Record, Substrate


@pytest.fixture()
def substrate(tmp_path):
    engine = None  # minimal stub: ScopeEngine not needed for these tests
    from weft.scope import ScopeEngine
    engine = ScopeEngine(known_scopes={"test"})
    sub = Substrate(str(tmp_path / "test.db"), engine)
    ctx = ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True)
    return sub, ctx


def _user_ctx() -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset({"test"}), user_authored=True, actor="local-user")


def _agent_ctx() -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset({"test"}), user_authored=False, actor="agent")


def test_propose_rule_agent(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Test rule"))
    assert rule.record_type == "ProposedRule"
    assert rule.payload["status"] == STATUS_PROPOSED
    assert rule.attribution == "agent"


def test_propose_rule_user(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _user_ctx(), __rule("r1", "test", "Test rule"))
    assert rule.payload["status"] == STATUS_PROPOSED


def test_promote_rule_requires_user_decision(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Test rule"))
    # No Decision/Commitment exists yet — must refuse.
    with pytest.raises(KnowledgeRefusal):
        promote_rule(sub, _user_ctx(), "r1")


def test_promote_rule_with_user_decision(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Test rule"))
    # Create a user-attributed Decision that names the rule.
    decision = Record(
        record_id="dec-1",
        record_type="Decision",
        scope="test",
        epistemic_status="stated",
        payload={"decision": "approve rule r1", "rule_id": "r1"},
        attribution="user",
    )
    sub.put(decision, _user_ctx())
    promoted = promote_rule(sub, _user_ctx(), "r1", promotion_record_id="dec-1")
    assert promoted.payload["status"] == STATUS_ACTIVE
    assert promoted.payload["promoted_by"] == "dec-1"


def test_promote_rule_rejects_non_user_attribution(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Test rule"))
    # Create a user-attributed Decision that names the rule.
    decision = Record(
        record_id="dec-user",
        record_type="Decision",
        scope="test",
        epistemic_status="stated",
        payload={"decision": "approve rule r1", "rule_id": "r1"},
        attribution="user",
    )
    sub.put(decision, _user_ctx())
    # Agent caller cannot promote, even with a valid user Decision present.
    with pytest.raises(KnowledgeRefusal):
        promote_rule(sub, _agent_ctx(), "r1", promotion_record_id="dec-user")


def test_reject_rule_requires_user(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Test rule"))
    with pytest.raises(KnowledgeRefusal):
        reject_rule(sub, _agent_ctx(), "r1")
    rejected = reject_rule(sub, _user_ctx(), "r1", reason="noisy")
    assert rejected.payload["status"] == STATUS_REJECTED


def test_deprecate_active_rule(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Test rule"))
    decision = Record(
        record_id="dec-1",
        record_type="Decision",
        scope="test",
        epistemic_status="stated",
        payload={"decision": "approve rule r1", "rule_id": "r1"},
        attribution="user",
    )
    sub.put(decision, _user_ctx())
    promoted = promote_rule(sub, _user_ctx(), "r1", promotion_record_id="dec-1")
    assert promoted.payload["status"] == STATUS_ACTIVE
    deprecated = deprecate_rule(sub, _user_ctx(), "r1")
    assert deprecated.payload["status"] == STATUS_DEPRECATED


def test_list_active_rules(substrate):
    sub, ctx = substrate
    r1 = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Rule one"))
    r2 = propose_rule(sub, _agent_ctx(), __rule("r2", "test", "Rule two"))
    decision = Record(
        record_id="dec-1",
        record_type="Decision",
        scope="test",
        epistemic_status="stated",
        payload={"decision": "approve rule r1", "rule_id": "r1"},
        attribution="user",
    )
    sub.put(decision, _user_ctx())
    promote_rule(sub, _user_ctx(), "r1", promotion_record_id="dec-1")
    active = list_active_rules(sub, _user_ctx(), "test")
    assert len(active) == 1
    assert active[0].record_id == "r1"


def test_promote_rule_via_source_ids(substrate):
    sub, ctx = substrate
    rule = propose_rule(sub, _agent_ctx(), __rule("r1", "test", "Test rule"))
    decision = Record(
        record_id="dec-2",
        record_type="Commitment",
        scope="test",
        epistemic_status="stated",
        payload={"commitment": "activate r1"},
        source_ids=["r1"],
        attribution="user",
    )
    sub.put(decision, _user_ctx())
    promoted = promote_rule(sub, _user_ctx(), "r1")
    assert promoted.payload["status"] == STATUS_ACTIVE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def __rule(rid: str, scope: str, name: str) -> "ProposedRule":
    from weft.procedural import ProposedRule, STATUS_DEPRECATED
    return ProposedRule(rule_id=rid, scope=scope, name=name, description=name)
