"""Phase 2 knowledge rules (spec §3, D2-2, D2-3, D2-9).

A pure decision function over a record and the caller's context. Storage lookups
are passed in, so the rules stay testable and cannot reach around the store.
Returns a reason string when the write must be refused, or None when allowed.
Callers never show the reason to the writer (generic denial, G4/G10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class KnowledgeRefusal(Exception):
    """Raised inside a write transaction; the store turns it into a generic denial."""


@dataclass(frozen=True)
class Rule:
    # attribution -> statuses that attribution may produce for this type
    statuses: dict = field(default_factory=dict)
    needs_source: bool = False


def _s(*names: str) -> frozenset:
    return frozenset(names)


RULES: dict[str, Rule] = {
    "Person": Rule({"user": _s("stated"), "document": _s("stated")}),
    "Agent": Rule({"system": _s("stated")}),
    "Project": Rule({"user": _s("stated")}),
    # Phase 3 (spec §1): assistant turns are agent-attributed Sources; fenced
    # or system-injected text is system-attributed. Neither can yield Decisions.
    "Source": Rule({
        "document": _s("stated"), "media-derived": _s("stated"), "user": _s("stated"),
        "agent": _s("stated"), "system": _s("stated"),
    }),
    "Conversation": Rule({"system": _s("stated")}),
    "Observation": Rule(
        {"document": _s("stated"), "media-derived": _s("stated")}, needs_source=True
    ),
    "Claim": Rule({"agent": _s("derived"), "verifier": _s("verified")}),
    "Hypothesis": Rule({"agent": _s("derived")}),
    "Decision": Rule({"user": _s("stated")}),
    "Commitment": Rule({"user": _s("stated")}),
    "Correction": Rule({"user": _s("stated")}),
    "Verification": Rule({"verifier": _s("stated")}),
    "Deletion": Rule({"user": _s("stated")}),
    "Summary": Rule({"system": _s("derived")}),
    "RecallReceipt": Rule({"system": _s("stated")}),
    "ProposedRule": Rule({
        "agent": _s("stated"),
        "user": _s("stated"),
        "system": _s("stated"),
    }),
}


def check_record(
    record,
    ctx,
    *,
    is_verifier: Callable[[str | None], bool],
    lookup: Callable[[str], object | None],
) -> str | None:
    """Return a refusal reason, or None if the record may be written."""
    rule = RULES.get(record.record_type)
    if rule is None:
        return "unknown record type"
    attribution = record.attribution
    if not attribution:
        return "attribution required"
    allowed = rule.statuses.get(attribution)
    if allowed is None:
        return "attribution not permitted for this record type"
    if record.epistemic_status not in allowed:
        return "status not permitted for this attribution"
    if attribution == "user" and not ctx.user_authored:
        # D2-2 / D2-7: "user" means the user's own turn. An agent cannot claim it.
        return "user attribution requires a user-authored turn"
    if rule.needs_source and not record.source_ids:
        return "observation must cite a source"
    if rule.needs_source:
        for sid in record.source_ids:
            src = lookup(sid)
            # Same-scope only: a foreign or missing source is one generic refusal.
            if src is None or src.scope != record.scope:
                return "cited source not available"
    if attribution == "verifier" and not is_verifier(ctx.actor):
        return "actor is not a registered verifier"
    if record.epistemic_status == "verified" and not _names_claim(record, lookup):
        return "no verification names this claim"
    return None


def _names_claim(claim, lookup) -> bool:
    """D2-3: a verified Claim must cite a Verification that names it.

    The Verification must be in the same scope as the Claim, so the answer
    reveals nothing about records the writer cannot already see.
    """
    for vid in claim.source_ids:
        v = lookup(vid)
        if (
            v is not None
            and v.record_type == "Verification"
            and v.attribution == "verifier"
            and v.scope == claim.scope
            and v.payload.get("claim_id") == claim.record_id
        ):
            return True
    return False
