"""A compact, auditable card of what one scope holds — assembled at call time, never stored.

The plan called this "peer/user representation" and expected a new record type. This does not store
one, deliberately. A stored representation is a snapshot: it goes stale without anything failing,
it cannot cite the records it was built from once they change, and reading it is indistinguishable
from reading a fact somebody asserted. So the card is a *projection assembled on demand* from
records in one scope — architecture rule 4, applied literally — and every field carries the ids it
came from.

What a card contains is rule-based and boring on purpose:

* ``commitments`` and ``decisions`` — stated records of those types, with their status;
* ``facts`` — Claims above a confidence floor, newest first, each citing its record and sources;
* ``graph`` — relation counts by predicate, so "how connected is this" is answerable;
* ``communities`` — label sizes from the projection in ``graph``;
* ``recent`` — the most recently created records, as ids only.

Nothing here infers a personality, a mood or a preference. That would be an interpretation dressed
as data, and the first reader of a wrong interpretation would be a model that then acted on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from aletheia.scope import ScopeContext
from aletheia.substrate import Substrate

#: Claims below this confidence are not worth a card line: a projection that includes everything
#: says nothing. The floor is a constant rather than a parameter so two callers cannot disagree
#: about what "a fact" means while both claiming to show the same thing.
FACT_CONFIDENCE_FLOOR = 0.5


@dataclass
class Card:
    """What one scope holds, with the record ids every line came from."""

    scope: str
    commitments: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    graph: dict[str, int] = field(default_factory=dict)
    communities: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    built_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "commitments": self.commitments,
            "decisions": self.decisions,
            "facts": self.facts,
            "recent": self.recent,
            "graph": self.graph,
            "communities": self.communities,
            "counts": self.counts,
            "built_at": self.built_at,
        }

    def record_ids(self) -> set[str]:
        """Every id the card cites — the set a caller can check the card against."""
        cited: set[str] = set(self.recent)
        for group in (self.commitments, self.decisions, self.facts):
            for line in group:
                cited.add(line["record_id"])
                cited.update(line.get("source_ids") or ())
        return cited


def _text(record) -> str:
    payload = record.payload if isinstance(record.payload, dict) else {}
    return str(payload.get("text") or "")


def _line(record, *, text_limit: int = 200) -> dict:
    """One card line: the id first, because a line that cannot be traced is not evidence."""
    return {
        "record_id": record.record_id,
        "text": _text(record)[:text_limit],
        "epistemic_status": record.epistemic_status,
        "source_ids": list(record.source_ids or ()),
    }


def build_card(sub: Substrate, ctx: ScopeContext, scope: str, *, limit: int = 20,
               at: float | None = None) -> Card:
    """Assemble the card for one scope. Returns an empty card when the scope is not readable.

    An unreadable scope is not an error and not an empty truth claim: the card says ``counts`` are
    zero and carries nothing. Callers that need to distinguish "empty" from "not allowed" ask the
    scope engine themselves, which is the only place that answer lives.
    """
    if ctx is None or scope not in ctx.granted_scopes:
        return Card(scope=scope, built_at=time.time())
    if not sub.scope_engine.authorize_read(scope, ctx, "").allowed:
        return Card(scope=scope, built_at=time.time())

    card = Card(scope=scope, built_at=time.time())
    rows = sub._conn.execute(
        "SELECT record_id, record_type, epistemic_status FROM records WHERE scope = ? "
        "ORDER BY created_at DESC", (scope,),
    ).fetchall()

    # A superseded record stays in the store as history but must not appear as current: the card is
    # about what this scope holds now, and a correction that left both versions looking equal would
    # be worse than no card at all.
    superseded = {row[0] for row in sub._conn.execute(
        "SELECT supersedes FROM records WHERE scope = ? AND supersedes IS NOT NULL", (scope,)).fetchall()}

    for record_id, record_type, status in rows:
        if record_id in superseded:
            continue
        record = sub.get(record_id, ctx)
        if record is None:
            continue
        if record_type == "Commitment" and len(card.commitments) < limit:
            card.commitments.append({**_line(record), "status": status})
        elif record_type == "Decision" and len(card.decisions) < limit:
            card.decisions.append({**_line(record), "status": status})
        elif record_type == "Claim" and len(card.facts) < limit:
            payload = record.payload if isinstance(record.payload, dict) else {}
            try:
                confidence = float(payload.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence >= FACT_CONFIDENCE_FLOOR:
                card.facts.append({**_line(record), "confidence": confidence})
        if len(card.recent) < limit:
            card.recent.append(record_id)

    card.counts = {"records": len(rows), "current": len([r for r in rows if r[0] not in superseded]),
                   "superseded": len(superseded & {r[0] for r in rows})}

    for predicate, count in sub._conn.execute(
            "SELECT predicate, COUNT(*) FROM relations WHERE scope = ? GROUP BY predicate "
            "ORDER BY COUNT(*) DESC, predicate", (scope,)).fetchall():
        card.graph[str(predicate)] = int(count)

    for label, size in sub._conn.execute(
            "SELECT label, COUNT(*) FROM communities WHERE scope = ? GROUP BY label "
            "ORDER BY COUNT(*) DESC, label LIMIT ?", (scope, limit)).fetchall():
        card.communities.append({"label": label, "size": int(size)})

    return card


def rebuild_card(sub: Substrate, ctx: ScopeContext, scope: str, **kwargs) -> Card:
    """Named for the CLI's symmetry with ``rebuild_communities``; nothing is persisted.

    Kept as a separate name rather than an alias so the docstring can say the thing a reader needs:
    calling this twice returns two cards and stores nothing, which is the property that makes the
    card safe to inject.
    """
    return build_card(sub, ctx, scope, **kwargs)