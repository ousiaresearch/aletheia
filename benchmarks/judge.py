#!/usr/bin/env python3
"""An LLM judge for stage-2 answers, with the repeatability check the spec demands.

Why this exists: exact match is brutal against LoCoMo's bare-span gold answers ("Three dogs.",
"2022") and token F1 is a proxy for correctness rather than a measurement of it. Published
answer-level numbers — Mem0, Zep, MemHQ — are judged, so any comparison needs a judge. But a judged
number is only as trustworthy as the judge, and parity-measurement-spec.md §3 sets four conditions
before one may be published:

  1. the judge model, version, prompt and decoding are declared in the artifact;
  2. repeatability is measured on a subsample (n >= 50) and the agreement rate published — a judge
     whose own repeat agreement is below ~90% cannot support a one-point claim;
  3. the judge is pinned for the life of a comparison, because changing it invalidates every prior
     number;
  4. the systematic failures are stored, not just the aggregate, because a judge's leniency is visible
     in its wrong cases and invisible in a rate.

Judging is deliberately strict about being a *judge*: it is given the question, the gold answer and
the predicted answer, and nothing else — no retrieved evidence, no reasoning trace. A judge shown the
evidence can reward an answer that the evidence supports over one that matches the gold answer, which
is a different question than the benchmark asks.

    python benchmarks/judge.py --artifact benchmarks/dev-s2-v2-dates.json --repeat 2
    python benchmarks/judge.py --artifact a.json --json judged.json --repeat 3 --sample 60
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: Bump when the judging prompt changes. A judged number is comparable only to another taken with the
#: same judge prompt version, exactly as with the reader.
JUDGE_PROMPT_VERSION = "judge.v1"

_JUDGE = """\
You are grading a question-answering system.

Question: {question}
Reference answer: {gold}
System answer: {predicted}

Decide whether the system answer is CORRECT or INCORRECT.

Rules:
- Correct means it conveys the same fact as the reference answer. Different wording, extra words, or a
  more verbose phrasing is still correct if the fact matches.
- A date, number or name must match the reference. A near-miss is incorrect.
- An empty system answer is INCORRECT.
- Do not reward an answer merely for being plausible or well-written.

Respond with a single JSON object and nothing else:
{{"verdict": "CORRECT" or "INCORRECT", "reason": "<one short sentence>"}}
"""


def build_prompt(question: str, gold: str, predicted: str) -> str:
    return _JUDGE.format(question=(question or "").strip(),
                         gold=(gold or "").strip(),
                         predicted=(predicted or "").strip() or "(empty)")


def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or "verdict" not in parsed:
        return None
    verdict = str(parsed["verdict"]).strip().upper()
    if verdict not in ("CORRECT", "INCORRECT"):
        return None
    return {"correct": verdict == "CORRECT", "reason": str(parsed.get("reason") or "")[:300]}


def judge_one(question: str, gold: str, predicted: str, *, api_key: str | None = None,
              timeout: float = 60.0) -> dict | None:
    """One judgement. ``None`` means the judge could not be reached or did not answer usably."""
    from aletheia import llm_client

    config = llm_client.get_config(api_key)
    if config is None:
        return None
    raw = llm_client.complete_text(build_prompt(question, gold, predicted), max_tokens=120,
                                  temperature=0.0, api_key=api_key, timeout=timeout)
    parsed = _parse(raw)
    if parsed is None:
        return None
    parsed["judge"] = f"{config.provider}:{config.model}"
    parsed["prompt_version"] = JUDGE_PROMPT_VERSION
    return parsed


def _cases_from_artifact(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    results = data.get("results") or [data]
    out = []
    for result in results:
        for case in result.get("reading_cases") or []:
            out.append(case)
    return out


def run(artifact: str, *, repeat: int = 2, sample: int | None = None, seed: int = 7,
        api_key: str | None = None) -> dict:
    from aletheia import llm_client

    cases = [c for c in _cases_from_artifact(artifact)
             if str(c.get("gold_answer") or "").strip()]
    if sample is not None and len(cases) > sample:
        cases = random.Random(seed).sample(cases, sample)
    if not cases:
        raise SystemExit("no gradable cases in the artifact (need reading_cases with a gold answer)")

    started = time.time()
    verdicts: list[list[bool]] = []
    verdict_by_qid: dict[str, bool] = {}
    verdict_unanimous_by_qid: dict[str, bool] = {}
    disagreement_qids: list[str] = []
    failures: list[dict] = []
    failures_total = 0
    unusable = 0
    for case in cases:
        per_case = []
        for _ in range(repeat):
            result = judge_one(case.get("question", case.get("question_id", "")),
                               str(case.get("gold_answer") or ""),
                               str(case.get("text") or ""), api_key=api_key)
            if result is None:
                unusable += 1
                continue
            per_case.append(result["correct"])
            if not result["correct"]:
                failures_total += 1
            if not result["correct"] and len(failures) < 40:
                failures.append({"question_id": case.get("question_id"),
                                 "gold": case.get("gold_answer"),
                                 "predicted": case.get("text"),
                                 "judge_reason": result["reason"]})
        if per_case:
            qid = str(case.get("question_id"))
            # The headline accuracy below uses per_case[0] (the first repeat). The per-case maps must
            # use the SAME rule or a published rate and its own record disagree by the number of
            # repeat-disagreements — which is exactly what happened (46.6% headline against a 45.5%
            # map). Both rules are recorded so a stricter reading is still available.
            verdict_by_qid[qid] = per_case[0]
            verdict_unanimous_by_qid[qid] = all(per_case)
            if len(set(per_case)) > 1:
                disagreement_qids.append(qid)
            verdicts.append(per_case)

    judged = [v[0] for v in verdicts]
    agree = [v for v in verdicts if len(v) > 1]
    agree_rate = (sum(1 for v in agree if len(set(v)) == 1) / len(agree)) if agree else None
    config = llm_client.get_config(api_key)
    return {
        "artifact": artifact,
        "cases": len(cases),
        "judged": len(judged),
        "unusable_judgements": unusable,
        "correct": sum(1 for v in judged if v),
        "accuracy": (sum(1 for v in judged if v) / len(judged)) if judged else None,
        "repeat": repeat,
        "agreement_n": len(agree),
        "agreement_rate": agree_rate,
        "judge_model": f"{config.provider}:{config.model}" if config else None,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "supportable": bool(agree_rate is not None and agree_rate >= 0.9),
        "failures": failures,
        "failures_total": failures_total,
        "failures_stored": len(failures),
        "failure_cap_note": ("`failures` is a capped sample for inspection; `case_verdicts` holds the "
                             "verdict for every graded question and is what paired comparisons must use."),
        "case_verdicts": verdict_by_qid,
        "case_verdicts_unanimous": verdict_unanimous_by_qid,
        "repeat_disagreement_qids": disagreement_qids,
        "verdict_rule": ("case_verdicts uses the first repeat, matching `accuracy`; "
                         "case_verdicts_unanimous requires every repeat to agree"),
        "elapsed_s": round(time.time() - started, 1),
        "note": ("A judged number is published only with its judge model, prompt version and repeat "
                 "agreement. Below 90% agreement the judge cannot support a one-point claim."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifact", required=True, help="a run.py --reader JSON artifact")
    parser.add_argument("--repeat", type=int, default=2, help="judgements per case, for agreement")
    parser.add_argument("--sample", type=int, default=None, help="judge a seeded sample")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    report = run(args.artifact, repeat=args.repeat, sample=args.sample, seed=args.seed)
    print(f"{report['artifact']}")
    print(f"  judge {report['judge_model']}  prompt {report['judge_prompt_version']}  "
          f"repeat {report['repeat']}")
    print(f"  judged {report['judged']}/{report['cases']} "
          f"({report['unusable_judgements']} unusable judgements)")
    if report["accuracy"] is not None:
        print(f"  judged accuracy {report['correct']}/{report['judged']} = "
              f"{report['accuracy']:.1%}")
    if report["agreement_rate"] is not None:
        print(f"  repeat agreement {report['agreement_rate']:.1%} on {report['agreement_n']} cases"
              f"  -> {'supportable' if report['supportable'] else 'NOT supportable (<90%)'}")
    if report["failures"]:
        stored = report["failures_stored"]
        total = report["failures_total"]
        shown = f"{stored} of {total}" if stored < total else f"{total}"
        print(f"  {shown} judged-wrong examples stored"
              f"{' (capped sample, not the full set)' if stored < total else ''}, e.g.:")
        for failure in report["failures"][:3]:
            print(f"    {failure['question_id']}: gold={str(failure['gold'])[:28]!r} "
                  f"predicted={str(failure['predicted'])[:28]!r}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())