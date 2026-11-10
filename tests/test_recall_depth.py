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