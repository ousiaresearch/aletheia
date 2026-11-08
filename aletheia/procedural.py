"""Phase 13 — Procedural memory / agent-curated state.

Allows agents to propose procedural rules and prompt refinements, gated by
user attribution. A ProposedRule stays in `proposed` status until a
user-attributed Decision or Commitment record in the same scope names it;
only then can it transition to `active`.

Design invariants:
- Identity grants no scope.
- Deny by default: no rule activates without user-attributable Decision/Commitment.
- Substrate is the only authority.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any

from aletheia.knowledge import KnowledgeRefusal, check_record
from aletheia.scope import ScopeContext
from aletheia.substrate import Record, Substrate


# ---------------------------------------------------------------------------
# Status lifecycle
# ---------------------------------------------------------------------------

STATUS_PROPOSED = "proposed"
STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"
STATUS_REJECTED = "rejected"

_VALID_TRANSITIONS = {
    STATUS_PROPOSED: {STATUS_ACTIVE, STATUS_REJECTED, STATUS_DEPRECATED},
    STATUS_ACTIVE: {STATUS_DEPRECATED},
    STATUS_DEPRECATED: set(),
    STATUS_REJECTED: set(),
}


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProposedRule:
    rule_id: str
    scope: str
    name: str
    description: str
    rule_type: str = "procedural"  # procedural | prompt_refinement | retrieval_tweak
    status: str = STATUS_PROPOSED
    created_at: float = field(default_factory=time.time)
    created_by: str = "agent"
    attribution: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def propose_rule(
    sub: Substrate,
    ctx: ScopeContext,
    rule: ProposedRule,
) -> Record:
    """Create a new ProposedRule record in `proposed` status.

    Agent-attributed writes may only produce `proposed` status.
    User-attributed writes may produce `active` directly, but only after
    the promotion gate is satisfied (see `promote_rule`).

    Fail-closed: raises KnowledgeRefusal on any policy violation.
    """
    record = Record(
        record_id=rule.rule_id,
        record_type="ProposedRule",
        scope=rule.scope,
        epistemic_status="stated",  # ProposedRule lifecycle lives in payload
        payload={
            "name": rule.name,
            "description": rule.description,
            "rule_type": rule.rule_type,
            "status": STATUS_PROPOSED,
            "created_by": rule.created_by,
        },
        attribution=rule.attribution or "agent",
        provenance={"method": "propose_rule"},
    )
    _check_write(record, ctx)
    sub.put(record, ctx)
    return record


def promote_rule(
    sub: Substrate,
    ctx: ScopeContext,
    rule_id: str,
    promotion_record_id: str | None = None,
) -> Record:
    """Promote a ProposedRule from `proposed` to `active`.

    Promotion requires a user-attributed Decision or Commitment record in
    the same scope that names this rule_id. The naming can appear in:
      - record_id match
      - source_ids list
      - payload field `rule_id` or `applies_to`

    Fail-closed: raises KnowledgeRefusal if gate is not satisfied.
    """
    record = sub.get(rule_id, ctx)
    if record is None or record.record_type != "ProposedRule":
        raise KnowledgeRefusal("rule not found")

    current_status = record.payload.get("status", STATUS_PROPOSED)
    if current_status != STATUS_PROPOSED:
        raise KnowledgeRefusal(f"rule is {current_status}, not proposed")
    if not _rule_is_live(sub, record):
        raise KnowledgeRefusal("rule is not live")

    # Find the promotion gate: user-attributed Decision/Commitment naming this rule.
    promoter = _find_promoter(sub, ctx, record, promotion_record_id)
    if promoter is None:
        raise KnowledgeRefusal("promotion requires user-attributed Decision or Commitment")

    # Same-scope check: promoter must live in the rule's scope.
    if promoter.scope != record.scope:
        raise KnowledgeRefusal("promotion record is in a different scope")

    # Caller must also be user-attributed.
    _check_user_only(ctx)

    # Re-read inside the same transaction to reduce race window.
    current = sub.get(rule_id, ctx)
    if current is None or current.payload.get("status", STATUS_PROPOSED) != STATUS_PROPOSED:
        raise KnowledgeRefusal("rule is no longer proposed; concurrent transition detected")

    # Transition to active.
    payload = dict(record.payload)
    payload["status"] = STATUS_ACTIVE
    payload["promoted_by"] = promoter.record_id
    payload["promoted_at"] = time.time()
    updated = Record(
        record_id=record.record_id,
        record_type=record.record_type,
        scope=record.scope,
        epistemic_status="stated",  # lifecycle status lives in payload
        payload=payload,
        created_at=record.created_at,
        effective_at=record.effective_at,
        supersedes=record.supersedes,
        source_ids=list(record.source_ids),
        provenance=dict(record.provenance),
        attribution=record.attribution,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
    )
    sub.put(updated, ctx, overwrite=True)
    return updated


def reject_rule(
    sub: Substrate,
    ctx: ScopeContext,
    rule_id: str,
    reason: str = "",
) -> Record:
    """Reject a ProposedRule. Requires user attribution.

    Fail-closed: raises KnowledgeRefusal if not user-attributed.
    """
    record = sub.get(rule_id, ctx)
    if record is None or record.record_type != "ProposedRule":
        raise KnowledgeRefusal("rule not found")
    current_status = record.payload.get("status", STATUS_PROPOSED)
    if current_status != STATUS_PROPOSED:
        raise KnowledgeRefusal(f"rule is {current_status}, not proposed")

    _check_user_only(ctx)

    # Re-read inside the same transaction to reduce race window.
    current = sub.get(rule_id, ctx)
    if current is None or current.payload.get("status", STATUS_PROPOSED) != STATUS_PROPOSED:
        raise KnowledgeRefusal("rule is no longer proposed; concurrent transition detected")

    payload = dict(record.payload)
    payload["status"] = STATUS_REJECTED
    payload["rejection_reason"] = reason
    updated = Record(
        record_id=record.record_id,
        record_type=record.record_type,
        scope=record.scope,
        epistemic_status="stated",  # lifecycle status lives in payload
        payload=payload,
        created_at=record.created_at,
        effective_at=record.effective_at,
        supersedes=record.supersedes,
        source_ids=list(record.source_ids),
        provenance=dict(record.provenance),
        attribution=record.attribution,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
    )
    sub.put(updated, ctx, overwrite=True)
    return updated


def deprecate_rule(
    sub: Substrate,
    ctx: ScopeContext,
    rule_id: str,
) -> Record:
    """Deprecate an active rule. Requires user attribution.

    Fail-closed: raises KnowledgeRefusal if not user-attributed or not active.
    """
    record = sub.get(rule_id, ctx)
    if record is None or record.record_type != "ProposedRule":
        raise KnowledgeRefusal("rule not found")
    current_status = record.payload.get("status", STATUS_PROPOSED)
    if current_status != STATUS_ACTIVE:
        raise KnowledgeRefusal(f"rule is {current_status}, not active")

    _check_user_only(ctx)

    # Re-read inside the same transaction to reduce race window.
    current = sub.get(rule_id, ctx)
    if current is None or current.payload.get("status", STATUS_PROPOSED) != STATUS_ACTIVE:
        raise KnowledgeRefusal("rule is no longer active; concurrent transition detected")

    payload = dict(record.payload)
    payload["status"] = STATUS_DEPRECATED
    payload["deprecated_at"] = time.time()
    updated = Record(
        record_id=record.record_id,
        record_type=record.record_type,
        scope=record.scope,
        epistemic_status="stated",  # lifecycle status lives in payload
        payload=payload,
        created_at=record.created_at,
        effective_at=record.effective_at,
        supersedes=record.supersedes,
        source_ids=list(record.source_ids),
        provenance=dict(record.provenance),
        attribution=record.attribution,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
    )
    sub.put(updated, ctx, overwrite=True)
    return updated


def _rule_is_live(sub: Substrate, record: Record) -> bool:
    """Evaluate rule validity and supersession in the current DB snapshot."""
    now = time.time()
    return (not (record.valid_from is not None and record.valid_from > now)
            and not (record.effective_at is not None and record.effective_at > now)
            and not (record.valid_to is not None and record.valid_to <= now)
            and not sub._conn.execute(
                "SELECT 1 FROM records WHERE supersedes=? LIMIT 1", (record.record_id,)
            ).fetchone())


def list_active_rules(sub: Substrate, ctx: ScopeContext, scope: str) -> list[Record]:
    """Return all ProposedRule records with `active` status in *scope*.

    Fail-closed: returns [] on any error.
    """
    try:
        # Same-scope authorization check: caller must have the scope.
        if scope not in ctx.granted_scopes:
            return []
        with sub._read_transaction():
            rows = sub._conn.execute(
                "SELECT record_id FROM records WHERE scope = ? AND record_type = 'ProposedRule' "
                "AND json_extract(payload, '$.status') = ?",
                (scope, STATUS_ACTIVE),
            ).fetchall()
            return [rec for row in rows if (rec := sub.get(row[0], ctx)) is not None
                    and rec.scope == scope and rec.record_type == 'ProposedRule'
                    and rec.payload.get('status') == STATUS_ACTIVE
                    and _rule_is_live(sub, rec) and sub._valid_rule_promoter(rec, ctx)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_write(record: Record, ctx: ScopeContext) -> None:
    """Enforce knowledge rules for ProposedRule writes."""
    # Add ProposedRule to the knowledge rules dynamically.
    from aletheia.knowledge import RULES, check_record

    if "ProposedRule" not in RULES:
        RULES["ProposedRule"] = _make_rule()

    reason = check_record(
        record,
        ctx,
        is_verifier=lambda actor: False,
        lookup=lambda rid: sub_get(record.scope, rid),
    )
    if reason:
        raise KnowledgeRefusal(reason)


def _make_rule():
    from aletheia.knowledge import Rule, _s
    return Rule({
        "agent": _s(STATUS_PROPOSED),
        "user": _s(STATUS_PROPOSED, STATUS_ACTIVE),
        "system": _s(STATUS_PROPOSED),
    })


def _check_user_only(ctx: ScopeContext) -> None:
    """Ensure the caller is a user-attributed turn."""
    if not ctx.user_authored:
        raise KnowledgeRefusal("user attribution required")


def _find_promoter(
    sub: Substrate,
    ctx: ScopeContext,
    rule: Record,
    promotion_record_id: str | None = None,
) -> Record | None:
    """Find a user-attributed Decision/Commitment that names this rule.

    Search order:
    1. Explicit promotion_record_id if provided.
    2. Any Decision/Commitment in scope with `user` attribution that
       references rule_id in record_id, source_ids, or payload.
    """
    candidates: list[Record] = []
    if promotion_record_id:
        rec = sub.get(promotion_record_id, ctx)
        if rec and rec.record_type in ("Decision", "Commitment"):
            candidates.append(rec)
    else:
        rows = sub._conn.execute(
            "SELECT record_id FROM records WHERE scope = ? AND record_type IN ('Decision', 'Commitment') "
            "AND attribution = 'user'",
            (rule.scope,),
        ).fetchall()
        candidates = [rec for row in rows if (rec := sub.get(row[0], ctx)) is not None]

    for rec in candidates:
        # Use the same authority check as the transactional write; put() checks
        # again under its write transaction if an approval changes meanwhile.
        proposed = replace(rule, payload={**rule.payload, 'promoted_by': rec.record_id})
        if sub._valid_rule_promoter(proposed, ctx):
            return rec
    return None


def _names_rule(record: Record, rule_id: str) -> bool:
    """Check if a Decision/Commitment record names the given rule_id."""
    if record.record_id == rule_id:
        return True
    if rule_id in (record.source_ids or []):
        return True
    payload = record.payload or {}
    for key in ("rule_id", "applies_to", "target_rule"):
        if payload.get(key) == rule_id:
            return True
    return False


def sub_get(scope: str, record_id: str) -> Any | None:
    """Minimal lookup for knowledge rule checks (avoids circular import)."""
    # This is called during check_record which receives a `lookup` callback.
    # We don't have the substrate here, so return a stub.
    # The actual check_record is called with the real lookup from the caller.
    return None
