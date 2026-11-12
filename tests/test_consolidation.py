"""W2.3 — consolidation: near-duplicates are found and marked, never deleted or auto-superseded.

The properties worth testing are the ones a wrong implementation would break silently:

* a duplicate is marked, and the record it duplicates still exists;
* consolidation never writes a Correction — Corrections need user attribution, and an agent that
  could retire a person's stated fact by clustering would be able to launder it away;
* clustering is order-independent and the keeper is chosen deterministically (strongest, then
  newest, then smallest id), including the last tiebreak;
* nothing crosses a scope boundary, in either direction;
* a dry run reports exactly what marking would do and writes nothing.
"""

from __future__ import annotations

import pytest

from aletheia.consolidation import (
    cosine,
    duplicate_groups,
    mark_duplicates,
    normalise,
    token_similarity,
)
from aletheia.graph import relations_from
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


def claim(sub, record_id, text, *, scope="alpha", confidence=0.8):
    record = Record(record_id=record_id, record_type="Claim", scope=scope,
                    epistemic_status="derived", payload={"text": text, "confidence": confidence},
                    attribution="agent")
    sub.put(record, ctx_for(scope))
    return record


# --------------------------------------------------------------------------- similarity


def test_normalisation_ignores_case_punctuation_and_order():
    assert normalise("The Buoy, is RED!") == "the buoy is red"
    assert token_similarity("the buoy is red", "RED buoy is the") == 1.0
    assert token_similarity("the buoy is red", "the tide is low") < 0.5
    assert token_similarity("", "") == 1.0


def test_cosine_is_symmetric_and_bounded():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(cosine([1.0, 2.0], [1.0, 2.0]))


# --------------------------------------------------------------------------- grouping


def test_near_duplicates_are_grouped_and_distinct_claims_are_not(sub):
    claim(sub, "a1", "the buoy is red")
    claim(sub, "a2", "The buoy is red.")
    claim(sub, "b1", "the tide table is wrong in March")

    groups = duplicate_groups(sub, ctx_for("alpha"), "alpha")
    assert len(groups) == 1
    assert groups[0].keeper in {"a1", "a2"}
    assert set(groups[0].duplicates) | {groups[0].keeper} == {"a1", "a2"}


def test_the_keeper_is_the_strongest_then_the_newest_then_the_smallest_id(sub):
    claim(sub, "weak", "the buoy is red", confidence=0.4)
    claim(sub, "strong", "the buoy is red", confidence=0.95)
    groups = duplicate_groups(sub, ctx_for("alpha"), "alpha")
    assert groups[0].keeper == "strong", "confidence decides the keeper"

    claim(sub, "tie-b", "the tide is low", confidence=0.7)
    claim(sub, "tie-a", "the tide is low", confidence=0.7)
    tide = [g for g in duplicate_groups(sub, ctx_for("alpha"), "alpha") if "tie-a" in g.duplicates + (g.keeper,)][0]
    assert tide.keeper == "tie-a", "equal confidence and age falls back to the id, deterministically"


def test_grouping_does_not_depend_on_row_order(sub):
    for record_id in ("n3", "n1", "n2"):
        claim(sub, record_id, "the lighthouse needs paint")
    first = duplicate_groups(sub, ctx_for("alpha"), "alpha")
    second = duplicate_groups(sub, ctx_for("alpha"), "alpha")
    assert [(g.keeper, g.duplicates) for g in first] == [(g.keeper, g.duplicates) for g in second]


def test_nothing_is_grouped_across_scopes(sub):
    claim(sub, "alpha-1", "the buoy is red", scope="alpha")
    claim(sub, "beta-1", "the buoy is red", scope="beta")

    groups = duplicate_groups(sub, ctx_for("alpha", "beta"), "alpha")
    assert [g.keeper for g in groups] == [], "one duplicate per scope has nothing to group with"
    assert duplicate_groups(sub, ctx_for("beta"), "alpha") == [], "and an ungranted scope yields nothing"


# --------------------------------------------------------------------------- marking


def test_marking_writes_edges_and_deletes_nothing(sub):
    claim(sub, "a1", "the buoy is red", confidence=0.9)
    claim(sub, "a2", "The buoy is red.")
    report = mark_duplicates(sub, ctx_for("alpha"), "alpha")

    assert report["edges_written"] == 1
    edges = relations_from(sub, ctx_for("alpha"), "a2")
    assert [(e.predicate, e.object) for e in edges] == [("duplicate_of", "a1")]
    assert sub.get("a2", ctx_for("alpha")) is not None, "the duplicate is still on the record"


def test_a_dry_run_reports_the_same_and_writes_nothing(sub):
    claim(sub, "a1", "the buoy is red", confidence=0.9)
    claim(sub, "a2", "The buoy is red.")
    report = mark_duplicates(sub, ctx_for("alpha"), "alpha", dry_run=True)

    assert report["dry_run"] is True and report["duplicates"] == 1
    assert report["edges_written"] == 0
    assert relations_from(sub, ctx_for("alpha"), "a2") == []


def test_consolidation_never_writes_a_correction(sub):
    """The failure that matters: an agent laundering away a person's stated fact."""
    claim(sub, "a1", "the buoy is red", confidence=0.9)
    claim(sub, "a2", "The buoy is red.")
    mark_duplicates(sub, ctx_for("alpha"), "alpha")
    corrections = sub._conn.execute(
        "SELECT COUNT(*) FROM records WHERE record_type = 'Correction'").fetchone()[0]
    superseded = sub._conn.execute(
        "SELECT COUNT(*) FROM records WHERE supersedes IS NOT NULL").fetchone()[0]
    assert corrections == 0 and superseded == 0


def test_a_threshold_of_one_marks_only_exact_token_matches(sub):
    claim(sub, "a1", "the buoy is red")
    claim(sub, "a2", "the buoy is red")
    claim(sub, "a3", "the buoy is red and rusty")

    report = mark_duplicates(sub, ctx_for("alpha"), "alpha", threshold=1.0)
    assert report["duplicates"] == 1
    assert relations_from(sub, ctx_for("alpha"), "a3") == []


def test_recall_returns_the_duplicate_but_the_keeper_keeps_the_match_slot(sub):
    """Marking has to change something in retrieval, or it is bookkeeping pretending to be work."""
    from aletheia.retrieval import recall

    ctx = ctx_for("alpha")
    claim(sub, "keeper", "the buoy is red at the harbour mouth", confidence=0.9)
    claim(sub, "copy", "the buoy is red at the harbour mouth", confidence=0.5)
    mark_duplicates(sub, ctx, "alpha")

    items, receipt = recall(sub, "buoy", ctx, mode="lexical", limit=5)
    by_id = {item.record_id: item for item in items}
    assert "keeper" in by_id and "copy" in by_id, "nothing is hidden"
    assert items[0].record_id == "keeper", "the keeper takes the first slot"
    assert by_id["copy"].neighbor is True, "the duplicate is kept as context, not as a match"


def test_recall_is_unaffected_when_the_duplicate_was_not_marked(sub):
    from aletheia.retrieval import recall

    ctx = ctx_for("alpha")
    claim(sub, "keeper", "the buoy is red at the harbour mouth", confidence=0.9)
    claim(sub, "copy", "the buoy is red at the harbour mouth", confidence=0.9)

    items, _ = recall(sub, "buoy", ctx, mode="lexical", limit=5)
    assert {item.neighbor for item in items if item.record_id in {"keeper", "copy"}} == {False}


def test_the_cli_reports_without_marking_by_default(tmp_path, capsys):
    import json

    from aletheia import cli

    db = tmp_path / "cli.db"
    engine = ScopeEngine(known_scopes={"alpha"})
    sub = Substrate(db, engine)
    try:
        sub.put(Record(record_id="a1", record_type="Claim", scope="alpha", epistemic_status="derived",
                       payload={"text": "the buoy is red", "confidence": 0.9}, attribution="agent"),
                ctx_for("alpha"))
        sub.put(Record(record_id="a2", record_type="Claim", scope="alpha", epistemic_status="derived",
                       payload={"text": "The buoy is red.", "confidence": 0.5}, attribution="agent"),
                ctx_for("alpha"))
    finally:
        sub.close()

    assert cli.main(["--db", str(db), "consolidate", "--scope", "alpha"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["duplicate_count"] == 1
    assert report["edges_written"] == 0

    assert cli.main(["--db", str(db), "consolidate", "--scope", "alpha", "--mark"]) == 0
    marked = json.loads(capsys.readouterr().out)
    assert marked["edges_written"] == 1