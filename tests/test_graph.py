"""Phase 12 / W2.1 — the relation graph: storage, scope discipline, and bounded traversal.

The tests worth having here are the ones that would catch a leak or a lie:

* an edge cannot join two records in different scopes, and cannot be written into a scope the
  caller has not been granted;
* traversal cannot step out of the caller's scopes, even when an edge exists in another one;
* an edge whose window has closed is invisible at the query time and still on disk;
* distance is shortest-hop, and the walk stops where it was told to;
* extraction draws an edge only for a citation the substrate can verify, never for a word that only
  looks like a relation.
"""

from __future__ import annotations

import time

import json

import pytest

from aletheia.graph import (
    PREDICATES,
    Relation,
    community_members,
    rebuild_communities,
    community_of,
    expand_candidates,
    extract_relations,
    extraction_candidates,
    neighbours,
    record_relation,
    relations_from,
    relations_to,
)
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate


@pytest.fixture
def engine():
    return ScopeEngine(known_scopes={"alpha", "beta", "shared"})


@pytest.fixture
def sub(tmp_path, engine):
    substrate = Substrate(tmp_path / "aletheia.db", scope_engine=engine)
    yield substrate
    substrate.close()


def ctx_for(*scopes: str) -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)


def claim(record_id: str, scope: str, text: str) -> Record:
    """An agent-derived Claim: admissible without a Source record, which Observation requires."""
    return Record(record_id=record_id, record_type="Claim", scope=scope, epistemic_status="derived",
                  payload={"text": text}, attribution="agent")


def seed(sub: Substrate, *record_ids: str, scope: str = "alpha") -> None:
    ctx = ctx_for(scope)
    for record_id in record_ids:
        sub.put(claim(record_id, scope, f"record {record_id}"), ctx)


def edge(subject: str, obj: str, scope: str = "alpha", predicate: str = "related_to", **kwargs) -> Relation:
    return Relation(subject=subject, predicate=predicate, object=obj, scope=scope, **kwargs)


# --------------------------------------------------------------------------- vocabulary


def test_only_known_predicates_are_storable():
    with pytest.raises(ValueError):
        Relation(subject="a", predicate="invented_by_caller", object="b", scope="alpha")


def test_confidence_and_windows_are_bounded():
    with pytest.raises(ValueError):
        Relation(subject="a", predicate="supports", object="b", scope="alpha", confidence=1.5)
    with pytest.raises(ValueError):
        Relation(subject="a", predicate="supports", object="b", scope="alpha", valid_from=10.0, valid_to=5.0)


# --------------------------------------------------------------------------- writing


def test_a_relation_is_stored_and_readable_from_both_ends(sub):
    seed(sub, "a", "b")
    assert record_relation(sub, ctx_for("alpha"), edge("a", "b", predicate="supports")) is True

    forward = relations_from(sub, ctx_for("alpha"), "a")
    backward = relations_to(sub, ctx_for("alpha"), "b")
    assert [r.object for r in forward] == ["b"]
    assert [r.subject for r in backward] == ["a"]
    assert forward[0].predicate == "supports"


def test_the_same_triple_is_idempotent_and_updates_in_place(sub):
    seed(sub, "a", "b")
    ctx = ctx_for("alpha")
    assert record_relation(sub, ctx, edge("a", "b", confidence=0.4)) is True
    assert record_relation(sub, ctx, edge("a", "b", confidence=0.9)) is True

    rows = sub._conn.execute("SELECT COUNT(*), MAX(confidence) FROM relations").fetchone()
    assert rows[0] == 1, "one edge, not two"
    assert rows[1] == pytest.approx(0.9)


def test_a_relation_into_a_scope_the_caller_was_not_granted_is_refused(sub, engine):
    engine.known_scopes = {"alpha", "beta"}
    seed(sub, "a", scope="alpha")
    seed(sub, "b", scope="beta")

    assert record_relation(sub, ctx_for("alpha"), edge("a", "b")) is False
    assert sub._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_a_relation_needs_both_endpoints_in_its_scope(sub):
    seed(sub, "a")
    assert record_relation(sub, ctx_for("alpha"), edge("a", "missing")) is False
    assert sub._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_one_record_cannot_be_linked_across_scopes_by_claiming_the_edge_scope(sub, engine):
    """An edge declared in `shared` does not make an `alpha` record and a `beta` one adjacent."""
    engine.known_scopes = {"alpha", "beta", "shared"}
    seed(sub, "a", scope="alpha")
    seed(sub, "b", scope="beta")

    assert record_relation(sub, ctx_for("shared"), edge("a", "b", scope="shared")) is False


# --------------------------------------------------------------------------- reading


def test_a_closed_window_hides_the_edge_without_deleting_it(sub):
    seed(sub, "a", "b")
    now = time.time()
    assert record_relation(sub, ctx_for("alpha"),
                           edge("a", "b", valid_from=now - 10, valid_to=now - 5)) is True

    assert relations_from(sub, ctx_for("alpha"), "a") == []
    assert relations_from(sub, ctx_for("alpha"), "a", at=now - 6)[0].object == "b"
    assert sub._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1


def test_reading_an_ungranted_scope_returns_nothing(sub, engine):
    engine.known_scopes = {"alpha", "beta"}
    seed(sub, "a", "b", scope="alpha")
    record_relation(sub, ctx_for("alpha"), edge("a", "b", scope="alpha"))

    assert relations_from(sub, ctx_for("beta"), "a") == []
    assert relations_to(sub, ctx_for("beta"), "b") == []


# --------------------------------------------------------------------------- traversal


def test_traversal_is_bounded_by_hops(sub):
    seed(sub, "a", "b", "c")
    ctx = ctx_for("alpha")
    record_relation(sub, ctx, edge("a", "b"))
    record_relation(sub, ctx, edge("b", "c"))

    assert neighbours(sub, ctx, "a", hops=1) == {"b": 1}
    assert neighbours(sub, ctx, "a", hops=2) == {"b": 1, "c": 2}
    assert neighbours(sub, ctx, "a", hops=0) == {}


def test_traversal_reports_the_shortest_distance(sub):
    seed(sub, "a", "b", "c")
    ctx = ctx_for("alpha")
    record_relation(sub, ctx, edge("a", "b"))
    record_relation(sub, ctx, edge("b", "c"))
    record_relation(sub, ctx, edge("a", "c"))

    assert neighbours(sub, ctx, "a", hops=2) == {"b": 1, "c": 1}


def test_traversal_stops_at_the_limit(sub):
    seed(sub, "a", "b", "c", "d")
    ctx = ctx_for("alpha")
    for target in ("b", "c", "d"):
        record_relation(sub, ctx, edge("a", target))

    reached = neighbours(sub, ctx, "a", hops=1, limit=2)
    assert len(reached) == 2


def test_traversal_cannot_bridge_scopes_through_a_shared_record(sub, engine):
    """The leak that matters: alpha — shared — beta walked by a caller granted only alpha."""
    engine.known_scopes = {"alpha", "beta", "shared"}
    seed(sub, "a", scope="alpha")
    seed(sub, "s", scope="shared")
    seed(sub, "b", scope="beta")
    record_relation(sub, ctx_for("alpha", "shared"), edge("a", "s", scope="shared"))
    record_relation(sub, ctx_for("shared", "beta"), edge("s", "b", scope="shared"))

    reached = neighbours(sub, ctx_for("alpha"), "a", hops=3)
    assert "b" not in reached, "an edge outside the caller's grants is not a permission"
    assert "s" not in reached, "the shared scope was not granted to this caller either"


def test_traversal_skips_an_endpoint_that_cannot_be_read(sub, engine):
    seed(sub, "a", "b")
    record_relation(sub, ctx_for("alpha"), edge("a", "b"))
    sub.delete("b", ctx_for("alpha"))

    assert neighbours(sub, ctx_for("alpha"), "a") == {}


# --------------------------------------------------------------------------- extraction


def test_extraction_candidates_match_the_vocabulary_only():
    assert extraction_candidates("This contradicts the earlier note.") == ["contradicts"]
    assert extraction_candidates("nothing relational here") == []
    assert set(extraction_candidates("x supports y, then x supersedes y")) <= PREDICATES


def test_extraction_draws_edges_only_for_verified_citations(sub):
    ctx = ctx_for("alpha")
    sub.put(claim("target", "alpha", "the seed"), ctx)
    sub.put(claim("citing", "alpha", "this contradicts target"), ctx)
    sub.put(claim("vague", "alpha", "this contradicts something unnamed"), ctx)

    result = extract_relations(sub, ctx, "alpha")
    assert result["relations_created"] == 1
    edges = relations_from(sub, ctx, "citing")
    assert [(e.object, e.predicate) for e in edges] == [("target", "contradicts")]
    assert edges[0].source_ids == ("citing",), "an edge names what justifies it"


def test_expansion_is_ordered_deterministic_and_excludes_the_seeds(sub):
    seed(sub, "a", "b", "c", "d")
    ctx = ctx_for("alpha")
    record_relation(sub, ctx, edge("a", "b"))
    record_relation(sub, ctx, edge("a", "c"))
    record_relation(sub, ctx, edge("b", "d"))

    assert set(expand_candidates(sub, ctx, ["a"])) == {"b", "c"}, "the default is one hop"

    first = expand_candidates(sub, ctx, ["a"], hops=2)
    second = expand_candidates(sub, ctx, ["a"], hops=2)
    assert first == second, "the same query returns the same order"
    assert "a" not in first
    assert set(first) == {"b", "c", "d"}
    assert first.index("b") < first.index("d"), "one hop before two"


def test_expansion_respects_the_seed_and_node_caps(sub):
    seed(sub, *["s", "n1", "n2", "n3", "n4", "n5"])
    ctx = ctx_for("alpha")
    for target in ("n1", "n2", "n3", "n4", "n5"):
        record_relation(sub, ctx, edge("s", target))

    assert len(expand_candidates(sub, ctx, ["s"], limit=3)) == 3
    assert expand_candidates(sub, ctx, ["s"], max_seeds=0) == []


def test_expansion_does_not_cross_into_an_ungranted_scope(sub, engine):
    engine.known_scopes = {"alpha", "beta"}
    seed(sub, "a", scope="alpha")
    seed(sub, "b", scope="beta")
    record_relation(sub, ctx_for("alpha", "beta"), edge("a", "b", scope="alpha"))

    assert expand_candidates(sub, ctx_for("alpha"), ["a"]) == []


def test_recall_adds_graph_neighbours_as_context_and_says_so_in_the_receipt(sub):
    """The graph must reach retrieval, and reaching it must not promote context to a match."""
    from aletheia.retrieval import recall

    ctx = ctx_for("alpha")
    sub.put(Record(record_id="match", record_type="Claim", scope="alpha", epistemic_status="derived",
                   payload={"text": "the lighthouse keeper wrote about barnacles"}, attribution="agent"), ctx)
    sub.put(record := Record(record_id="related", record_type="Claim", scope="alpha",
                             epistemic_status="derived",
                             payload={"text": "an unrelated observation about tide tables"},
                             attribution="agent"), ctx)
    assert record is not None
    assert record_relation(sub, ctx, edge("match", "related", predicate="supports")) is True

    items, receipt = recall(sub, "barnacles", ctx, mode="lexical", limit=5)
    ids = [item.record_id for item in items]
    assert ids[0] == "match", "the matching record keeps its slot"
    assert "related" in ids, "the graph neighbour is reachable through the edge"
    assert ids.index("related") > ids.index("match"), "context ranks after matches"
    assert next(i for i in items if i.record_id == "related").neighbor is True
    assert "graph_expand" in receipt.layers


def test_recall_without_edges_reports_no_graph_layer(sub):
    from aletheia.retrieval import recall

    ctx = ctx_for("alpha")
    sub.put(Record(record_id="solo", record_type="Claim", scope="alpha", epistemic_status="derived",
                   payload={"text": "solitary needle"}, attribution="agent"), ctx)
    items, receipt = recall(sub, "needle", ctx, mode="lexical", limit=5)
    assert [i.record_id for i in items] == ["solo"]
    assert "graph_expand" not in receipt.layers


def test_communities_are_a_deterministic_partition(sub):
    """Two components in, two communities out; the same run twice gives the same labels."""
    seed(sub, "a", "b", "c", "d")
    ctx = ctx_for("alpha")
    record_relation(sub, ctx, edge("a", "b"))
    record_relation(sub, ctx, edge("b", "c"))
    record_relation(sub, ctx, edge("c", "d"))

    first = rebuild_communities(sub, ctx, "alpha")
    labels_first = {rid: community_of(sub, ctx, rid) for rid in ("a", "b", "c", "d")}
    second = rebuild_communities(sub, ctx, "alpha")
    labels_second = {rid: community_of(sub, ctx, rid) for rid in ("a", "b", "c", "d")}

    assert first["nodes"] == 4 and second["nodes"] == 4
    assert first["communities"] == second["communities"] == 1
    assert labels_first == labels_second, "a partition nobody can reproduce is not auditable"
    assert len(set(labels_first.values())) == 1


def test_separate_components_get_separate_communities(sub):
    seed(sub, "a", "b", "x", "y")
    ctx = ctx_for("alpha")
    record_relation(sub, ctx, edge("a", "b"))
    record_relation(sub, ctx, edge("x", "y"))

    result = rebuild_communities(sub, ctx, "alpha")
    assert result["communities"] == 2
    assert community_of(sub, ctx, "a") == community_of(sub, ctx, "b")
    assert community_of(sub, ctx, "a") != community_of(sub, ctx, "x")


def test_a_community_does_not_form_across_scopes(sub, engine):
    engine.known_scopes = {"alpha", "beta"}
    seed(sub, "a", "b", scope="alpha")
    seed(sub, "p", "q", scope="beta")
    record_relation(sub, ctx_for("alpha"), edge("a", "b"))
    record_relation(sub, ctx_for("beta"), edge("p", "q"))

    rebuild_communities(sub, ctx_for("alpha", "beta"), "alpha")
    assert community_of(sub, ctx_for("alpha"), "p") is None, "beta was never labelled by an alpha rebuild"
    assert community_members(sub, ctx_for("beta"), "alpha", "a") == []


def test_community_reads_are_scope_gated(sub, engine):
    engine.known_scopes = {"alpha", "beta"}
    seed(sub, "a", "b", scope="alpha")
    record_relation(sub, ctx_for("alpha"), edge("a", "b"))
    rebuild_communities(sub, ctx_for("alpha"), "alpha")

    assert community_of(sub, ctx_for("beta"), "a") is None
    assert community_members(sub, ctx_for("beta"), "alpha", "a") == []


def test_rebuilding_an_ungranted_scope_writes_nothing(sub, engine):
    engine.known_scopes = {"alpha", "beta"}
    seed(sub, "a", "b", scope="beta")
    assert rebuild_communities(sub, ctx_for("alpha"), "beta") == {"nodes": 0, "communities": 0, "iterations": 0}
    assert sub._conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0] == 0


def test_a_rebuild_replaces_the_previous_projection(sub):
    seed(sub, "a", "b", "c")
    ctx = ctx_for("alpha")
    record_relation(sub, ctx, edge("a", "b"))
    rebuild_communities(sub, ctx, "alpha")
    before = sub._conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0]

    record_relation(sub, ctx, edge("b", "c"))
    rebuild_communities(sub, ctx, "alpha")
    after = sub._conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
    assert before == after == 3, "a rebuild replaces the projection rather than appending to it"


def test_extraction_in_an_ungranted_scope_does_nothing(sub, engine):
    engine.known_scopes = {"alpha", "beta"}
    seed(sub, "a", "b", scope="beta")
    assert extract_relations(sub, ctx_for("alpha"), "beta") == {"records_scanned": 0, "relations_created": 0}


# --------------------------------------------------------------------------- operator surface


def test_the_cli_can_draw_relations_and_read_the_graph_back(tmp_path, capsys):
    """The feature is only real if an operator can populate it and look at it."""
    from aletheia import cli

    db = tmp_path / "cli.db"
    engine = ScopeEngine(known_scopes={"alpha"})
    ctx = ctx_for("alpha")
    sub = Substrate(db, engine)
    try:
        sub.put(claim("seed", "alpha", "the seed note"), ctx)
        sub.put(claim("citing", "alpha", "this contradicts seed"), ctx)
    finally:
        sub.close()

    common = ["--db", str(db)]
    assert cli.main(common + ["relate", "--scope", "alpha"]) == 0
    related = json.loads(capsys.readouterr().out)
    assert related["relations_created"] == 1, related

    assert cli.main(common + ["graph", "--scope", "alpha", "--rebuild-communities"]) == 0
    communities = json.loads(capsys.readouterr().out)
    assert communities["nodes"] == 2 and communities["communities"] == 1

    assert cli.main(common + ["graph", "--scope", "alpha", "--record-id", "seed"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["neighbours"] == {"citing": 1}
    assert view["community"] is not None


# --------------------------------------------------------------------------- schema


def test_the_schema_records_the_relations_version(tmp_path, engine):
    substrate = Substrate(tmp_path / "v8.db", scope_engine=engine)
    try:
        version = substrate._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 8
        tables = {row[0] for row in substrate._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "relations" in tables
    finally:
        substrate.close()


def test_reopening_an_existing_store_keeps_its_relations(tmp_path, engine):
    path = tmp_path / "reopen.db"
    first = Substrate(path, scope_engine=engine)
    try:
        ctx = ctx_for("alpha")
        for record_id in ("a", "b"):
            first.put(claim(record_id, "alpha", record_id), ctx)
        assert record_relation(first, ctx, edge("a", "b")) is True
    finally:
        first.close()

    second = Substrate(path, scope_engine=engine)
    try:
        assert [r.object for r in relations_from(second, ctx_for("alpha"), "a")] == ["b"]
    finally:
        second.close()