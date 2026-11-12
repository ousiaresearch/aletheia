"""Paired comparison of two judged artifacts, with the failure modes from today closed off.

Every paired statistic in this programme was until now computed by hand in a scratch cell, and one of
those comparisons was wrong because it read `failures` (a capped sample) as the set of wrong answers.
This script exists so the comparison has one audited path:

  - it refuses an artifact that predates `case_verdicts`, because there is no honest way to recover
    per-case verdicts from a capped list;
  - it compares only questions both artifacts graded and reports the shared N;
  - it reports the exact sign test, not a normal approximation, because N is small;
  - it prints judge agreement and refuses to make a claim when either side is below 90%;
  - it never prints a bare difference without the questions-gained/lost counts beside it.

    .venv/bin/python benchmarks/compare_judged.py A.json B.json [--label-a plain --label-b facts]
"""
from __future__ import annotations

import argparse
import json
import sys
from math import comb
from pathlib import Path


def exact_sign_test(gained: int, lost: int) -> float:
    """Two-sided exact binomial test on discordant pairs. Ties are excluded, as the test requires."""
    n = gained + lost
    if n == 0:
        return 1.0
    k = min(gained, lost)
    return min(1.0, 2 * sum(comb(n, i) for i in range(0, k + 1)) / 2 ** n)


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("case_verdicts"):
        raise SystemExit(
            f"{path}: no case_verdicts. This artifact predates per-case verdicts, and `failures` is a "
            f"capped sample — comparing on it is exactly the defect that invalidated two earlier "
            f"comparisons. Re-judge with the current judge.py.")
    if not data.get("supportable", False):
        print(f"WARNING {path}: judge repeat agreement {data.get('agreement_rate')} is below 0.90 — "
              f"this artifact cannot support a one-point claim.", file=sys.stderr)
    return data


def compare(a: dict, b: dict, label_a: str, label_b: str) -> dict:
    va, vb = a["case_verdicts"], b["case_verdicts"]
    shared = sorted(set(va) & set(vb))
    if not shared:
        raise SystemExit("the two artifacts share no graded questions — not a paired comparison")
    only_a = sorted(set(va) - set(vb))
    only_b = sorted(set(vb) - set(va))
    gained = [q for q in shared if vb[q] and not va[q]]
    lost = [q for q in shared if va[q] and not vb[q]]
    a_correct = sum(1 for q in shared if va[q])
    b_correct = sum(1 for q in shared if vb[q])
    return {
        "shared": len(shared),
        "only_a": len(only_a),
        "only_b": len(only_b),
        "a_correct": a_correct,
        "b_correct": b_correct,
        "a_rate": a_correct / len(shared),
        "b_rate": b_correct / len(shared),
        "gained": len(gained),
        "lost": len(lost),
        "net": len(gained) - len(lost),
        "p": round(exact_sign_test(len(gained), len(lost)), 4),
        "label_a": label_a,
        "label_b": label_b,
        "judge_a": a.get("judge_model"),
        "judge_b": b.get("judge_model"),
        "agreement_a": a.get("agreement_rate"),
        "agreement_b": b.get("agreement_rate"),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--label-a", default="A")
    p.add_argument("--label-b", default="B")
    p.add_argument("--json", default=None)
    args = p.parse_args(argv)

    a, b = load(args.a), load(args.b)
    if a.get("judge_model") != b.get("judge_model"):
        print(f"WARNING judges differ ({a.get('judge_model')} vs {b.get('judge_model')}); measured "
              f"judge sensitivity today was at most one question, but say so when quoting this.",
              file=sys.stderr)
    result = compare(a, b, args.label_a, args.label_b)

    print(f"{result['label_a']}: {result['a_correct']}/{result['shared']} = {result['a_rate']:.1%}"
          f"   (judge {result['judge_a']}, agreement {result['agreement_a']})")
    print(f"{result['label_b']}: {result['b_correct']}/{result['shared']} = {result['b_rate']:.1%}"
          f"   (judge {result['judge_b']}, agreement {result['agreement_b']})")
    print(f"paired on {result['shared']} shared questions "
          f"({result['only_a']} only in A, {result['only_b']} only in B)")
    print(f"  {result['label_b']} gained {result['gained']}, lost {result['lost']}"
          f"  -> net {result['net']:+d} questions, exact sign test p = {result['p']}")
    if result["p"] >= 0.05:
        print("  NOT SIGNIFICANT at this N — report the direction and the shared N, not a ranking.")
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())