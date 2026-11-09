"""Finding and marking near-duplicate records — without deleting anything or inventing a correction.

Two rules shape this module, both inherited from the substrate rather than invented here:

* **Nothing is deleted.** A duplicate is marked with a ``duplicate_of`` edge pointing at the record
  that stands for it. The history of what was written stays written.
* **Nothing is superseded automatically.** Corrections require user attribution, so an agent may
  report or mark a duplicate but may not rewrite the record of what somebody asserted. That is why
  this module never writes a ``Correction`` — the test suite asserts it.

Similarity is lexical by default (normalised token-set Jaccard) and embedding-based when an embedder
is supplied. The default is the crude one on purpose: consolidation has to be runnable without
loading a 4-bit checkpoint, its results have to be reproducible in a test, and a lexical threshold
that under-reports is a safer failure than a model that over-merges. When an embedder is passed, the
cosine similarity is computed from it directly — the stored vectors are not trusted here, because a
vector written by a different model id would silently compare across models.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from aletheia.scope import ScopeContext
from aletheia.substrate import Substrate

#: Default lexical threshold: identical token sets, allowing for word order and punctuation.
DEFAULT_THRESHOLD = 0.9

_WORD = re.compile(r"[a-z0-9']+")


def normalise(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def token_similarity(left: str, right: str) -> float:
    """Jaccard over token sets. 1.0 means the same words in any order; 0.0 means nothing shared."""
    a, b = set(normalise(left).split()), set(normalise(right).split())
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(x * y for x, y in zip(left, right))
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(y * y for y in right))
    return 0.0 if norm == 0 else dot / norm


@dataclass(frozen=True)
class Group:
    """One cluster: the keeper and the records it stands for."""

    keeper: str
    duplicates: tuple[str, ...]
    similarity: float


def _text(record) -> str:
    payload = record.payload if isinstance(record.payload, dict) else {}
    return str(payload.get("text") or "")


def _confidence(record) -> float:
    payload = record.payload if isinstance(record.payload, dict) else {}
    try:
        return max(0.0, min(1.0, float(payload.get("confidence", 0.5))))
    except (TypeError, ValueError):
        return 0.0


def _keeper_order(record):
    """Pick the record that stands for the group: strongest, then newest, then smallest id.

    Deterministic to the last tiebreak, because "which of these two is the real one" is a question
    that will be asked again, and two runs giving two answers makes the answer worthless.
    """
    return (-_confidence(record), -(record.created_at or 0.0), record.record_id)


def duplicate_groups(sub: Substrate, ctx: ScopeContext, scope: str, *, threshold: float = DEFAULT_THRESHOLD,
                     record_type: str = "Claim", limit: int = 500, embedder=None) -> list[Group]:
    """Cluster near-duplicates inside one scope. Never crosses a scope boundary.

    Greedy and order-independent: records are visited in sorted id order, each joins the first
    existing group whose keeper it is similar enough to, otherwise it becomes a keeper. A cluster
    therefore cannot depend on the order the database happened to return rows in.
    """
    if ctx is None or scope not in ctx.granted_scopes:
        return []
    if not sub.scope_engine.authorize_read(scope, ctx, "").allowed:
        return []
    rows = sub._conn.execute(
        "SELECT record_id FROM records WHERE scope = ? AND record_type = ? ORDER BY record_id LIMIT ?",
        (scope, record_type, limit),
    ).fetchall()
    records = []
    for (record_id,) in rows:
        record = sub.get(record_id, ctx)
        if record is not None and _text(record).strip():
            records.append(record)
    if len(records) < 2:
        return []

    vectors: dict[str, list[float]] = {}
    if embedder is not None:
        for record in records:
            try:
                vectors[record.record_id] = list(embedder.embed_query(_text(record)))
            except Exception:
                vectors = {}
                break

    def similarity(left, right) -> float:
        if vectors and left.record_id in vectors and right.record_id in vectors:
            return cosine(vectors[left.record_id], vectors[right.record_id])
        return token_similarity(_text(left), _text(right))

    keepers: list = []
    clustered: dict[str, list] = {}
    scores: dict[str, float] = {}
    for record in records:
        placed = False
        for keeper in keepers:
            score = similarity(keeper, record)
            if score >= threshold:
                clustered[keeper.record_id].append(record)
                scores[keeper.record_id] = min(scores.get(keeper.record_id, 1.0), score)
                placed = True
                break
        if not placed:
            keepers.append(record)
            clustered[record.record_id] = []
            scores[record.record_id] = 1.0

    groups: list[Group] = []
    for keeper_id, members in clustered.items():
        if not members:
            continue
        candidate = next(r for r in records if r.record_id == keeper_id)
        # The strongest record in the cluster is the keeper, not merely the first one seen.
        ordered = sorted([candidate, *members], key=_keeper_order)
        chosen = ordered[0]
        duplicates = tuple(r.record_id for r in ordered[1:])
        groups.append(Group(keeper=chosen.record_id, duplicates=duplicates, similarity=scores[keeper_id]))
    return sorted(groups, key=lambda group: group.keeper)


def mark_duplicates(sub: Substrate, ctx: ScopeContext, scope: str, *, threshold: float = DEFAULT_THRESHOLD,
                    record_type: str = "Claim", limit: int = 500, dry_run: bool = False,
                    embedder=None) -> dict:
    """Write a ``duplicate_of`` edge from every duplicate to its keeper.

    ``dry_run`` reports the same groups and writes nothing, which is the mode to use on a store
    somebody cares about: the edges are cheap, but a threshold that is wrong for this corpus is not
    obvious until you have looked at what it would have marked.
    """
    from aletheia.graph import Relation, record_relation

    groups = duplicate_groups(sub, ctx, scope, threshold=threshold, record_type=record_type,
                              limit=limit, embedder=embedder)
    written = 0
    if not dry_run:
        for group in groups:
            for duplicate in group.duplicates:
                if record_relation(sub, ctx, Relation(subject=duplicate, predicate="duplicate_of",
                                                      object=group.keeper, scope=scope,
                                                      source_ids=(duplicate,),
                                                      valid_from=time.time())):
                    written += 1
    return {
        "records_scanned": sum(1 + len(group.duplicates) for group in groups),
        "groups": len(groups),
        "duplicates": sum(len(group.duplicates) for group in groups),
        "edges_written": written,
        "dry_run": dry_run,
        "threshold": threshold,
        "keepers": [group.keeper for group in groups],
    }