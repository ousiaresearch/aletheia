"""The comparison tool must refuse the inputs that made an earlier comparison wrong.

The defect being guarded against: `failures` is capped at forty examples, so an analysis treating it as
the set of wrong answers silently counts every question past the cap as correct. Two paired comparisons
were reported that way.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.compare_judged import compare, exact_sign_test


def _artifact(verdicts: dict[str, bool], **extra) -> dict:
    data = {"case_verdicts": verdicts, "accuracy": sum(verdicts.values()) / len(verdicts),
            "judged": len(verdicts), "correct": sum(verdicts.values()),
            "agreement_rate": 0.98, "supportable": True,
            "judge_model": "openai-compatible:qwen3.8-flash"}
    data.update(extra)
    return data


def test_an_artifact_without_case_verdicts_is_refused(tmp_path: Path):
    from benchmarks.compare_judged import load

    path = tmp_path / "old.json"
    path.write_text(json.dumps({"accuracy": 0.5, "failures": [{"question_id": "q1"}]}),
                    encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        load(path)
    assert "case_verdicts" in str(exc.value)


def test_only_shared_questions_are_compared():
    a = _artifact({"q1": True, "q2": False, "q3": True})
    b = _artifact({"q1": False, "q2": True, "q4": True})
    r = compare(a, b, "A", "B")
    assert r["shared"] == 2, "q3 and q4 are not shared and must not enter either rate"
    assert r["only_a"] == 1 and r["only_b"] == 1


def test_gained_and_lost_are_counted_per_question():
    a = _artifact({"q1": True, "q2": False, "q3": True, "q4": False})
    b = _artifact({"q1": False, "q2": True, "q3": True, "q4": True})
    r = compare(a, b, "A", "B")
    assert r["gained"] == 2          # q2, q4
    assert r["lost"] == 1            # q1
    assert r["net"] == 1
    assert r["a_correct"] == 2 and r["b_correct"] == 3


def test_sign_test_is_exact_not_approximate():
    assert exact_sign_test(0, 0) == 1.0
    assert exact_sign_test(3, 3) == 1.0
    # 9 gained / 0 lost over 9 discordant pairs: 2 * (1/512) = 0.0039
    assert exact_sign_test(9, 0) == pytest.approx(2 / 512, rel=1e-6)
    assert exact_sign_test(1, 9) == pytest.approx(2 * 11 / 1024, rel=1e-6)


def test_a_flat_result_is_reported_as_not_significant():
    a = _artifact({"q1": True, "q2": False, "q3": True, "q4": False})
    b = _artifact({"q1": False, "q2": True, "q3": True, "q4": False})
    r = compare(a, b, "A", "B")
    assert r["p"] >= 0.05


def test_comparing_disjoint_artifacts_fails_loudly():
    a = _artifact({"q1": True})
    b = _artifact({"q2": True})
    with pytest.raises(SystemExit):
        compare(a, b, "A", "B")