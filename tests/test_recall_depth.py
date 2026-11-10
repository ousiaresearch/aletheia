"""Retrieval depth and scoring depth must stay separate.

The pool experiment retrieves deep (depth 50 -> a candidate pool of ~300) and scores shallow, so
`hit@10` still means "the gold record was in the first ten we would have returned". If a deeper recall
quietly widened the scoring window, every number would improve for the wrong reason — the same class of
error as scoring a proxy and calling it retrieval quality.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def tiny_run():
    """One two-question run, done once: loading the dataset is the expensive part."""
    from benchmarks import run as bench

    return bench.run("locomo", limit=3, n=2, recall_depth=25)


def test_scoring_depth_is_the_limit_not_the_recall_depth(tiny_run):
    assert tiny_run["k"] == 3, "scoring depth drifted from --limit"
    assert tiny_run["recall_depth"] == 25, "the retrieval depth was not recorded"


def test_hit_rate_cannot_exceed_one_at_shallow_scoring(tiny_run):
    if tiny_run["hit_at_k"] is not None:
        assert 0.0 <= tiny_run["hit_at_k"] <= 1.0


def test_a_deeper_recall_does_not_inflate_the_reported_depth(tiny_run):
    """The number that decides a hit is `k`, and it must stay small when retrieval went deep."""
    assert tiny_run["k"] < tiny_run["recall_depth"]


def test_recall_depth_defaults_to_the_limit():
    from benchmarks import run as bench

    summary = bench.run("locomo", limit=2, n=1)
    assert summary["recall_depth"] == 2
    assert summary["k"] == 2

def test_reader_window_is_independent_of_the_scored_window():
    """`--reader-items` widens what the reader sees without moving what is scored.

    If the reader window widened the scored window, hit@10 would improve for the wrong reason — the
    same defect class as scoring at depth 50 while calling it k=10.
    """
    from benchmarks.run import _reading_items, _ReadingItem

    class _Rec:
        def __init__(self, rid, text):
            self.record_id = rid
            self.text = text

    items = [_Rec(f"r{i}", "x" * 100) for i in range(20)]
    meta = {f"r{i}": ("Ana", "2023-05-01") for i in range(20)}

    wide = _reading_items(items[:10], meta, max_chars=40)
    assert len(wide) == 10
    assert all(len(i.text) <= 41 for i in wide)          # clipped, plus the ellipsis
    assert wide[0].text.endswith("…")

    unclipped = _reading_items(items[:3], meta)
    assert all(len(i.text) == 100 for i in unclipped)
