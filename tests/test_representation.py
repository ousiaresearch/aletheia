"""W2.2 — the representation card: assembled, scoped, cited, and never stored.

The tests that matter are the ones a leak or a stale claim would fail:

* nothing from a scope the caller was not granted appears, in any field;
* every line cites the record ids it came from;
* a correction (a record that supersedes another) removes the superseded line rather than showing
  both as equally current;
* building twice stores nothing, which is what makes the card safe to inject;
* an empty scope and an ungranted scope are both empty cards, and the caller can tell them apart by
  asking the scope engine rather than by parsing prose.
"""

from __future__ import annotations

import pytest

from aletheia.graph import Relation, rebuild_communities, record_relation
from aletheia.representation import FACT_CONFIDENCE_FLOOR, build_card, rebuild_card
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate


@pytest.fixture
def engine():
    return ScopeEngine(known_scopes={"alpha", "beta"})


@pytest.fixture
def sub(tmp_path, engine):
    substrate = Substrate(tmp_path / "aletheia.db", scope_engine=engine)
    yield substrate
    substrate.close()


def ctx_for(*scopes: str) -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)


def put_claim(sub, record_id, scope, text, *, confidence=0.9, supersedes=None):
    record = Record(record_id=record_id, record_type="Claim", scope=scope,
                    epistemic_status="derived", payload={"text": text, "confidence": confidence},
                    attribution="agent", supersedes=supersedes)
    sub.put(record, ctx_for(scope))
    return record


def test_the_card_collects_the_record_types_it_promises(sub):
    ctx = ctx_for("alpha")
    put_claim(sub, "c1", "alpha", "the tide table is wrong in March")
    sub.put(Record(record_id="commit1", record_type="Commitment", scope="alpha",
                   epistemic_status="stated", payload={"text": "call the harbour office"},
                   attribution="user"), ctx)
    sub.put(Record(record_id="dec1", record_type="Decision", scope="alpha",
                   epistemic_status="stated", payload={"text": "use the 2025 almanac"},
                   attribution="user"), ctx)

    card = build_card(sub, ctx, "alpha")
    assert [line["record_id"] for line in card.facts] == ["c1"]
    assert [line["record_id"] for line in card.commitments] == ["commit1"]
    assert [line["record_id"] for line in card.decisions] == ["dec1"]
    assert card.counts["records"] == 3


def test_every_line_cites_its_record(sub):
    put_claim(sub, "c1", "alpha", "one", confidence=0.95)
    card = build_card(sub, ctx_for("alpha"), "alpha")
    for group in (card.facts, card.commitments, card.decisions):
        for line in group:
            assert line["record_id"]
    assert "c1" in card.record_ids()


def test_low_confidence_claims_stay_off_the_card(sub):
    put_claim(sub, "strong", "alpha", "a well supported claim", confidence=0.9)
    put_claim(sub, "weak", "alpha", "a hunch", confidence=FACT_CONFIDENCE_FLOOR - 0.2)

    card = build_card(sub, ctx_for("alpha"), "alpha")
    assert [line["record_id"] for line in card.facts] == ["strong"]


def test_a_superseded_record_is_not_current_on_the_card(sub):
    """Correcting a fact must remove it from "what this scope holds now".

    A Correction is the record type that supersedes (the substrate refuses any other), so the card
    shows neither an uncorrected claim nor two versions of the same thing looking equally current.
    """
    ctx = ctx_for("alpha")
    put_claim(sub, "old", "alpha", "the buoy is red")
    # Corrections are written through correct(), which is the only path that links supersedes and
    # journals the repair; putting one directly is refused, which is why this test found it.
    sub.correct("old", Record(record_id="fix", record_type="Correction", scope="alpha",
                              epistemic_status="stated", payload={"text": "the buoy is green"},
                              attribution="user"), ctx)

    card = build_card(sub, ctx, "alpha")
    assert [line["record_id"] for line in card.facts] == [], "the corrected claim is history, not a fact"
    assert card.counts["superseded"] == 1
    assert "fix" in card.record_ids(), "the correction is on the card, in recent"


def test_nothing_from_an_ungranted_scope_appears_anywhere(sub):
    put_claim(sub, "beta-claim", "beta", "CLASSIFIED: the vault code is violet")
    put_claim(sub, "alpha-claim", "alpha", "an ordinary note")

    card = build_card(sub, ctx_for("alpha"), "alpha")
    text = " ".join(line["text"] for line in card.facts)
    assert "violet" not in text
    assert "beta-claim" not in card.record_ids()
    assert build_card(sub, ctx_for("alpha"), "beta").as_dict()["facts"] == []


def test_graph_and_communities_are_summarised_without_text(sub):
    ctx = ctx_for("alpha")
    put_claim(sub, "a", "alpha", "one")
    put_claim(sub, "b", "alpha", "two")
    record_relation(sub, ctx, Relation(subject="a", predicate="supports", object="b", scope="alpha"))
    rebuild_communities(sub, ctx, "alpha")

    card = build_card(sub, ctx, "alpha")
    assert card.graph == {"supports": 1}
    assert card.communities and card.communities[0]["size"] == 2


def test_building_twice_stores_nothing(sub):
    put_claim(sub, "c1", "alpha", "one")
    before = sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    first = rebuild_card(sub, ctx_for("alpha"), "alpha").as_dict()
    second = rebuild_card(sub, ctx_for("alpha"), "alpha").as_dict()
    after = sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    assert before == after, "a projection that persists is a snapshot that goes stale silently"
    assert first["facts"] == second["facts"]


def test_a_deleted_record_leaves_the_card(sub):
    put_claim(sub, "c1", "alpha", "one")
    put_claim(sub, "c2", "alpha", "two")
    ctx = ctx_for("alpha")
    assert [line["record_id"] for line in build_card(sub, ctx, "alpha").facts] == ["c2", "c1"]

    sub.delete("c2", ctx)
    assert [line["record_id"] for line in build_card(sub, ctx, "alpha").facts] == ["c1"]


def test_the_cli_returns_the_card_as_json(tmp_path, capsys):
    import json

    from aletheia import cli

    db = tmp_path / "cli.db"
    engine = ScopeEngine(known_scopes={"alpha"})
    sub = Substrate(db, engine)
    try:
        put_claim(sub, "c1", "alpha", "the tide table is wrong in March")
    finally:
        sub.close()

    assert cli.main(["--db", str(db), "representation", "--scope", "alpha"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "representation"
    assert [line["record_id"] for line in payload["facts"]] == ["c1"]
    assert payload["scope"] == "alpha"


def test_the_card_is_json_serialisable():
    import json

    card = build_card.__doc__  # touch the module to keep the import meaningful
    assert card is not None
    from aletheia.representation import Card

    json.dumps(Card(scope="alpha", built_at=1.0).as_dict())