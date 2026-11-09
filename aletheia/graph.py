"""Typed relations between records: subject — predicate → object, scoped and time-bounded.

The substrate's ``entity_links`` table answers *"which record mentions this string"*. This answers
the question a graph is for: *"which records are related, and how"*. Subject and object are
``record_id`` values, never free text, for one reason — an edge can then be authorised the same way
a record is. A string-keyed graph would be a second scope system, and a second scope system is how
a leak gets in.

Everything here is scope-checked on the way in and on the way out:

* writing an edge requires the scope to be granted *and* writable, and both endpoints to exist in it;
* traversing requires the edge's scope to be granted and readable, checked per hop, so a hop cannot
  step from a shared scope into a private one;
* an edge whose window has closed is invisible at the query time, without being deleted — the
  history of what was believed stays in the table.

Nothing is inferred here. Extraction is a separate, opt-in step (``extract_relations``) and its
output is an ordinary record-citing edge; an edge with no ``source_ids`` is a bug, not a shortcut.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from aletheia.scope import ScopeContext
from aletheia.substrate import Substrate

#: The predicates this module will store. A closed set, because an open one is a string field with
#: pretensions: nothing can be filtered, ranked or explained against a vocabulary that is invented
#: per caller.
PREDICATES = frozenset({
    "related_to",
    "part_of",
    "supports",
    "contradicts",
    "supersedes",
    "derived_from",
})

#: Rules for the rule-based extractor. Deliberately small and auditable: each pattern names the
#: predicate it produces, and anything it does not match produces no edge at all. A wrong edge is
#: worse than a missing one, because traversal multiplies whatever it is given.
_EXTRACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:contradicts|conflicts with|disagrees with)\b", re.IGNORECASE), "contradicts"),
    (re.compile(r"\b(?:supersedes|replaces|corrects)\b", re.IGNORECASE), "supersedes"),
    (re.compile(r"\b(?:supports|confirms|corroborates)\b", re.IGNORECASE), "supports"),
    (re.compile(r"\b(?:part of|belongs to|member of)\b", re.IGNORECASE), "part_of"),
    (re.compile(r"\b(?:derived from|based on|summarises|summarizes)\b", re.IGNORECASE), "derived_from"),
)


@dataclass(frozen=True)
class Relation:
    """One edge. ``source_ids`` names the records that justify it — never empty in practice."""

    subject: str
    predicate: str
    object: str
    scope: str
    confidence: float = 1.0
    valid_from: float | None = None
    valid_to: float | None = None
    source_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.predicate not in PREDICATES:
            raise ValueError(f"unknown predicate {self.predicate!r}; known: {sorted(PREDICATES)}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence is a number between 0 and 1")
        if self.valid_from is not None and self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")


def _authorised(sub: Substrate, ctx: ScopeContext, scope: str, *, write: bool) -> bool:
    """Scope-level gate, with the same shape ``entity_link`` uses for writes.

    A scope absent from ``granted_scopes`` is refused before the engine is consulted: the engine
    answers questions about records and content, and a write here happens before either exists.
    """
    if ctx is None or scope not in ctx.granted_scopes:
        return False
    decision = sub.scope_engine.authorize_read(scope, ctx, "")
    if not decision.allowed:
        return False
    return True


def record_relation(sub: Substrate, ctx: ScopeContext, relation: Relation) -> bool:
    """Store one edge, scope-checked and already-bounded. Idempotent on the same triple.

    Returns False — never raises for an authorisation outcome — when the scope is not writable, or
    when either endpoint is not a record in that scope. Both endpoints existing in the same scope is
    what keeps a cross-scope edge from being expressible at all.
    """
    if not _authorised(sub, ctx, relation.scope, write=True):
        return False
    with sub._lock:
        own = None
        try:
            own = sub._begin_write("aletheia_graph_relate")
            endpoints = {relation.subject, relation.object}
            placeholders = ", ".join("?" for _ in endpoints)
            found = {
                row[0]
                for row in sub._conn.execute(
                    f"SELECT record_id FROM records WHERE scope = ? AND record_id IN ({placeholders})",
                    (relation.scope, *endpoints),
                ).fetchall()
            }
            if found != endpoints:
                sub._abort_write("aletheia_graph_relate", own)
                return False
            sub._conn.execute(
                "INSERT INTO relations (subject, predicate, object, scope, confidence, valid_from, "
                "valid_to, source_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(subject, predicate, object, scope) DO UPDATE SET "
                "confidence=excluded.confidence, valid_from=excluded.valid_from, "
                "valid_to=excluded.valid_to, source_ids=excluded.source_ids",
                (relation.subject, relation.predicate, relation.object, relation.scope,
                 relation.confidence, relation.valid_from, relation.valid_to,
                 json.dumps(list(relation.source_ids)), time.time()),
            )
            sub._finish_write("aletheia_graph_relate", own)
            return True
        except Exception:
            if own is not None:
                sub._abort_write("aletheia_graph_relate", own)
            return False


def _row_to_relation(row) -> Relation:
    subject, predicate, obj, scope, confidence, valid_from, valid_to, source_ids = row[:8]
    try:
        sources = tuple(json.loads(source_ids or "[]"))
    except (TypeError, ValueError):
        sources = ()
    return Relation(subject=subject, predicate=predicate, object=obj, scope=scope,
                    confidence=float(confidence or 1.0), valid_from=valid_from, valid_to=valid_to,
                    source_ids=sources)


def relations_from(sub: Substrate, ctx: ScopeContext, record_id: str, *, predicate: str | None = None,
                   at: float | None = None, limit: int = 200) -> list[Relation]:
    """Edges leaving ``record_id`` that the caller may read, live at ``at``."""
    scopes = [scope for scope in sorted(ctx.granted_scopes)
              if sub.scope_engine.authorize_read(scope, ctx, "").allowed]
    if not scopes:
        return []
    live_at = time.time() if at is None else float(at)
    clauses = [f"scope IN ({', '.join('?' for _ in scopes)})", "subject = ?",
               "(valid_from IS NULL OR valid_from <= ?)", "(valid_to IS NULL OR valid_to >= ?)"]
    params: list = [*scopes, record_id, live_at, live_at]
    if predicate is not None:
        clauses.append("predicate = ?")
        params.append(predicate)
    rows = sub._conn.execute(
        "SELECT subject, predicate, object, scope, confidence, valid_from, valid_to, source_ids "
        f"FROM relations WHERE {' AND '.join(clauses)} ORDER BY confidence DESC, object LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row_to_relation(row) for row in rows]


def relations_to(sub: Substrate, ctx: ScopeContext, record_id: str, *, at: float | None = None,
                 limit: int = 200) -> list[Relation]:
    """Edges arriving at ``record_id``. The graph is traversed in both directions on purpose: a
    correction points forward, and the reader of the corrected fact wants to know it happened."""
    scopes = [scope for scope in sorted(ctx.granted_scopes)
              if sub.scope_engine.authorize_read(scope, ctx, "").allowed]
    if not scopes:
        return []
    live_at = time.time() if at is None else float(at)
    rows = sub._conn.execute(
        "SELECT subject, predicate, object, scope, confidence, valid_from, valid_to, source_ids "
        f"FROM relations WHERE scope IN ({', '.join('?' for _ in scopes)}) AND object = ? "
        "AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to >= ?) "
        "ORDER BY confidence DESC, subject LIMIT ?",
        (*scopes, record_id, live_at, live_at, limit),
    ).fetchall()
    return [_row_to_relation(row) for row in rows]


def neighbours(sub: Substrate, ctx: ScopeContext, record_id: str, *, hops: int = 1, limit: int = 200,
               at: float | None = None) -> dict[str, int]:
    """Breadth-first walk from ``record_id``: ``{record_id: distance}``, the start excluded.

    Every hop is authorised separately, so a path cannot leave the caller's scopes; the walk is
    bounded by ``hops`` and ``limit`` and stops at whichever comes first. Distance is the shortest
    one found, which is what a retrieval boost should use — a fact two edges away is weaker evidence
    than one edge away, and pretending otherwise would flatten the graph into a second keyword index.
    """
    if hops <= 0 or limit <= 0:
        return {}
    reached: dict[str, int] = {}
    frontier = [record_id]
    seen = {record_id}
    for distance in range(1, hops + 1):
        next_frontier: list[str] = []
        for node in frontier:
            edges = relations_from(sub, ctx, node, at=at, limit=limit) + relations_to(sub, ctx, node, at=at, limit=limit)
            for edge in edges:
                other = edge.object if edge.subject == node else edge.subject
                if other in seen:
                    continue
                # The far endpoint must be readable in its own scope: an edge is not a permission.
                record = sub.get(other, ctx)
                if record is None:
                    continue
                seen.add(other)
                reached[other] = distance
                next_frontier.append(other)
                if len(reached) >= limit:
                    return reached
        if not next_frontier:
            break
        frontier = next_frontier
    return reached


def expand_candidates(sub: Substrate, ctx: ScopeContext, seed_ids: list[str], *, hops: int = 1,
                      max_seeds: int = 5, limit: int = 20, at: float | None = None) -> list[str]:
    """Record ids reachable from the first ``max_seeds`` seeds, as *context*, in a stable order.

    Ordered by seed, then by distance, then by record id: a caller that retries the same query gets
    the same list, which a set iteration would not give. Seeds are excluded. Bounded twice — by
    ``hops`` and by ``limit`` — because a graph expansion that can add an unbounded number of
    records is a way to make recall slow and its receipt meaningless.
    """
    ordered: list[str] = []
    seen = set(seed_ids)
    for seed in seed_ids[:max_seeds]:
        if len(ordered) >= limit:
            break
        reached = neighbours(sub, ctx, seed, hops=hops, limit=limit, at=at)
        for record_id in sorted(reached, key=lambda rid: (reached[rid], rid)):
            if record_id in seen:
                continue
            seen.add(record_id)
            ordered.append(record_id)
            if len(ordered) >= limit:
                break
    return ordered


# --------------------------------------------------------------------------- communities


def rebuild_communities(sub: Substrate, ctx: ScopeContext, scope: str, *, max_iterations: int = 10,
                        at: float | None = None) -> dict[str, int]:
    """Label propagation inside one scope, written as a projection to the `communities` table.

    Deterministic by construction: nodes are visited in sorted order and a tie between equally
    frequent labels is broken by the smaller label, so two runs over the same edges produce the same
    partition. A non-deterministic partition would make "which cluster is this" unreproducible, and
    therefore unauditable.

    Only edges in ``scope`` are read and only records in ``scope`` are labelled: a community is not a
    way to see across a boundary. Membership is derived state — it is rebuilt, never trusted, and
    deleting it loses nothing.
    """
    if not _authorised(sub, ctx, scope, write=True):
        return {"nodes": 0, "communities": 0, "iterations": 0}

    live_at = time.time() if at is None else float(at)
    nodes = {row[0] for row in sub._conn.execute(
        "SELECT record_id FROM records WHERE scope = ?", (scope,)).fetchall()}
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for subject, obj in sub._conn.execute(
            "SELECT subject, object FROM relations WHERE scope = ? "
            "AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to >= ?)",
            (scope, live_at, live_at)).fetchall():
        if subject in adjacency and obj in adjacency and subject != obj:
            adjacency[subject].add(obj)
            adjacency[obj].add(subject)

    labels = {node: node for node in nodes}
    iterations = 0
    with sub._lock:
        for iterations in range(1, max_iterations + 1):
            changed = False
            for node in sorted(nodes):
                neighbours_here = adjacency[node]
                if not neighbours_here:
                    continue
                counts: dict[str, int] = {}
                for other in neighbours_here:
                    counts[labels[other]] = counts.get(labels[other], 0) + 1
                best = max(counts.values())
                winner = min(label for label, count in counts.items() if count == best)
                if labels[node] != winner:
                    labels[node] = winner
                    changed = True
            if not changed:
                break

        own = None
        try:
            own = sub._begin_write("aletheia_graph_communities")
            now = time.time()
            sub._conn.execute("DELETE FROM communities WHERE scope = ?", (scope,))
            sub._conn.executemany(
                "INSERT INTO communities (scope, record_id, label, updated_at) VALUES (?, ?, ?, ?)",
                [(scope, record_id, label, now) for record_id, label in sorted(labels.items())],
            )
            sub._finish_write("aletheia_graph_communities", own)
        except Exception:
            if own is not None:
                sub._abort_write("aletheia_graph_communities", own)
            return {"nodes": 0, "communities": 0, "iterations": 0}

    return {"nodes": len(nodes), "communities": len(set(labels.values())), "iterations": iterations}


def community_of(sub: Substrate, ctx: ScopeContext, record_id: str) -> str | None:
    """The label this record was last projected into, or None when it has not been computed.

    Read through the scope gate: a projection is not a permission, and an unreadable record has no
    community to report.
    """
    record = sub.get(record_id, ctx)
    if record is None:
        return None
    row = sub._conn.execute(
        "SELECT label FROM communities WHERE scope = ? AND record_id = ?", (record.scope, record_id)
    ).fetchone()
    return None if row is None else str(row[0])


def community_members(sub: Substrate, ctx: ScopeContext, scope: str, label: str, *,
                      limit: int = 100) -> list[str]:
    """Records projected into ``label`` in ``scope`` — sorted, and only if the scope is readable."""
    if not _authorised(sub, ctx, scope, write=False):
        return []
    return [row[0] for row in sub._conn.execute(
        "SELECT record_id FROM communities WHERE scope = ? AND label = ? ORDER BY record_id LIMIT ?",
        (scope, label, limit)).fetchall()]


def extraction_candidates(text: str) -> list[str]:
    """Predicates the rule set finds in ``text``, in the order they appear.

    Rules, not a model: this runs on write, must be reproducible, and must never invent a predicate
    the vocabulary does not have. Whether an edge is worth drawing stays the caller's decision —
    a matched word is a candidate, not a relation.
    """
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    for pattern, predicate in _EXTRACTION_RULES:
        for match in pattern.finditer(text):
            hits.append((match.start(), predicate))
    return [predicate for _position, predicate in sorted(hits)]


def extract_relations(sub: Substrate, ctx: ScopeContext, scope: str, *, limit: int = 100,
                      at: float | None = None) -> dict[str, int]:
    """Create edges for records that name another record and a known predicate.

    A record names another by ``record_id`` in its text or payload. Citation is the only relation
    this extractor trusts, because it is the only one the substrate can verify; anything fuzzier
    belongs to a model, opt-in, with its confidence recorded.
    """
    if not _authorised(sub, ctx, scope, write=True):
        return {"records_scanned": 0, "relations_created": 0}
    created = scanned = 0
    rows = sub._conn.execute(
        "SELECT record_id, payload FROM records WHERE scope = ? LIMIT ?", (scope, limit)
    ).fetchall()
    known = {row[0] for row in sub._conn.execute(
        "SELECT record_id FROM records WHERE scope = ?", (scope,)).fetchall()}
    for record_id, payload_json in rows:
        scanned += 1
        try:
            text = str(json.loads(payload_json).get("text") or "")
        except (TypeError, ValueError):
            text = ""
        predicates = extraction_candidates(text)
        if not predicates:
            continue
        cited = sorted({other for other in known if other != record_id and other in text})
        for target in cited:
            if record_relation(sub, ctx, Relation(subject=record_id, predicate=predicates[0],
                                                  object=target, scope=scope,
                                                  source_ids=(record_id,), valid_from=at)):
                created += 1
    return {"records_scanned": scanned, "relations_created": created}