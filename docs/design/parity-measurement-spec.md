# Parity measurement spec — what a comparable number is

**Written:** 2026-11-09, after the first honest benchmark run and its two audits.
**Why this file exists:** `competitive-gap-closure-plan.md` calls Phase 9 a "credibility gate" and
never defines what makes a number credible or comparable. Its acceptance criteria ask whether the
harness reproduces, not whether its output can be set beside anyone else's. That omission produced
three concrete failures in one week: an answer-string proxy read as retrieval quality (44.2% quoted
where the true figure was 97.0%), a fabricated "93.0 floor" measured against, and a fusion claim
built on a category-skewed sample. This file is the definition that was missing.

---

## 1. The three stages, which are never the same number

A memory system answers a question in stages, and a score at one stage says nothing about another.

| Stage | Question it answers | Metric | What it cannot see |
|---|---|---|---|
| **Retrieval** | did the evidence come back? | hit@k on gold evidence ids; mean coverage (fraction of a question's gold ids retrieved); full-recovery rate (all of them) | unreadable evidence, partial evidence on multi-hop, arithmetic and aggregation |
| **Reading** | can an answer be produced *from fixed evidence*? | answer accuracy with retrieval held constant | whether retrieval would have found it in practice |
| **End-to-end** | can the whole system answer? | answer accuracy with retrieval free to succeed or fail | which stage caused a failure |

Two rules follow, and both have been violated here:

1. **Never publish one stage's number under another stage's name.** "hit@10 64.4%" is retrieval.
   "answer-in-context 44.2%" is a proxy for reading a string, and is degenerate in both directions:
   on LongMemEval, where gold answers are bare values like `"2"`, it reads 44.2% against real
   retrieval of 97.0%; on LoCoMo, where gold answers are literal spans, it reads 64.3% against 64.4%
   and is merely redundant.
2. **Answer accuracy is bounded by retrieval.** A reader limited to our top-10 cannot exceed our
   hit@10. So our 64.4% is a *ceiling* for any end-to-end number built on that retrieval, and it is
   not comparable to Mem0's 67.1%, which is an achieved end-to-end figure under their own retrieval
   and their own reader. Neither figure establishes who is ahead.

## 2. What "surpass a competitor" is allowed to mean

Any statement of the form "we beat X" requires all four of the following. Missing any one, the claim
is withdrawn, not softened.

1. **The same questions.** Same dataset, same split, same filtering of unanswerable items. State the
   N and the exclusions.
2. **The same scoring path.** Same metric definition and, for answer-level, the same judge. If we
   cannot run their scorer, we may publish our number labelled with our scorer and say explicitly
   that no comparison is licensed.
3. **The same stage.** Retrieval against retrieval, answer against answer. A cross-stage comparison
   is never a comparison.
4. **An interval, and it must separate.** If our 95% CI overlaps theirs, the honest statement is
   "indistinguishable on this sample", not "we win".

A target is derived from a measured competitor figure under §2.1–2.4, or it is not written down. No
target may enter a document without the artifact that produced it — the rule the withdrawn "93.0
floor" broke.

## 3. The judge is part of the measurement

Answer-level scores from an LLM judge carry their own variance and their own leniency, so an
answer-level number is meaningless without saying what judged it.

Requirements for any answer-level figure we publish:

- **Declare** judge model, version, prompt, and decoding settings in the results file.
- **Report agreement**: repeat the judging on a subsample (n ≥ 50) and publish the agreement rate
  between runs. A judge whose own repeat-agreement is below ~90% cannot support a one-point claim.
- **Pin** the judge for the life of a comparison. Switching judge mid-programme invalidates every
  prior number, so the version lives in the artifact.
- **Publish the failures**: store judged-wrong examples, because a judge's systematic leniency is
  visible in them and invisible in an aggregate.

## 4. The reader question, which is a product decision

Mem0, Zep and MemHQ publish answer-level numbers because generation is inside their product. Aletheia
deliberately is not that: `recall` returns scoped, receipted evidence and the host agent generates.
There is no reader in our path, so **Aletheia currently has no answer-level number — not a low one,
none.** Two legitimate ways forward, and this needs a decision rather than drift:

- **Option A — ship a reference reader for measurement only.** A pinned local model, a pinned prompt,
  used to produce end-to-end numbers for comparison and never enabled in the recall path. Costs a
  model dependency and a judge programme; buys a number that sits in the same table as everyone
  else's.
- **Option B — compete on retrieval only, and say so.** Publish retrieval numbers with coverage and
  full recovery, and state plainly that answer-level parity is not claimed and cannot be, because the
  reader belongs to the host. Cheaper, honest, and concedes the table that buyers actually read.

Drift between these two is the failure mode: shipping retrieval numbers while implying answer-level
competitiveness.

## 5. Gates, in dependency order

Work is judged by the gate it is in. A change that improves a later gate while regressing an earlier
one is a regression.

1. **Coverage.** Full-recovery rate on multi-evidence questions. Currently 11.9% on LoCoMo's 427
   multi-record questions; depth 50 only reaches 26.9%, so this is a ranking problem, not truncation.
   Nothing downstream can exceed this.
2. **Retrieval precision.** hit@k and MRR, with abstention scored separately — currently 0/444, i.e.
   recall never declines.
3. **Reading.** With retrieval fixed, measure whether evidence converts to answers. Our
   answer-in-context of 44.2% on LongMemEval against 97.0% hit@10 says the reading stage is where the
   largest measured loss sits even when retrieval succeeds.
4. **End-to-end.** Only after 1–3, and only under whichever of §4's options is chosen.

## 6. What this project may and may not claim today

**May claim:** retrieval-level results on LoCoMo (hit@10 64.4%, CI 61.9–66.7, N=1,538) and
LongMemEval-S (97.0% against gold sessions, CI 95.1–98.2, N=500), with coverage, full recovery and
abstention published beside them; that the harness has been adversarially audited and its instrument
defects fixed; that the multi-record deficit is the largest measured weakness.

**May not claim:** parity with, superiority over, or a gap to Mem0/Zep/MemHQ; any end-to-end or
answer-level figure; any target floor not derived from a committed artifact.

---

## Appendix — the protocol for a real comparison, when we get there

1. Fix the question set: dataset, split, and the rule for excluding unanswerable items, written down
   before running.
2. Run our system at a declared retrieval depth and record per-question retrieved ids.
3. Score stage 1 (retrieval) against gold ids: hit@k, coverage, full recovery, abstention.
4. Score stage 2 (reading) with a pinned reader over those fixed ids, judged by a pinned judge with a
   repeatability check (§3).
5. For a competitor's number, obtain their scorer or their per-question outputs. If neither is
   available, publish ours labelled and state that comparison is not licensed.
6. Report both stages with N and 95% intervals, and state the verdict as separated / overlapping.