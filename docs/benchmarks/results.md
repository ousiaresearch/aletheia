# Aletheia Benchmark Results (full run — Colab)

**Generated:** 2026-10-29T22:11:18Z
**Methodology:** Retrieval hit@k + MRR when evidence_ids available; token F1 fallback.
**Embedder:** none — lexical only
**Hardware:** Colab CPU (no GPU allocated)

## LoCoMo (10 conversations, 1,986 QA pairs)

| Metric | Value |
|--------|-------|
| Exact match | 55.3% |
| Token F1 | 0.356 |
| MRR | 0.356 |
| p50 latency | 77.6 ms |
| p95 latency | 147.5 ms |

## LongMemEval-S (500 questions)

| Metric | Value |
|--------|-------|
| Exact match | 33.8% |
| Token F1 | 0.014 |
| p50 latency | 153.1 ms |
| p95 latency | 295.9 ms |

## BEAM 100K (20 conversations, 400 questions)

| Metric | Value |
|--------|-------|
| Exact match | 5.5% |
| Token F1 | 0.035 |
| Abstention count | 160 (0.0% accuracy) |
| p50 latency | 86.3 ms |
| p95 latency | 191.2 ms |

## BEAM 500K (35 conversations, 700 questions)

| Metric | Value |
|--------|-------|
| Exact match | 5.0% |
| Token F1 | 0.033 |
| Abstention count | 280 (0.0% accuracy) |
| p50 latency | 1,313.2 ms |
| p95 latency | 2,896.8 ms |

## Competitive context

| System | LoCoMo | BEAM | Method |
|--------|--------|------|--------|
| **Aletheia (lexical)** | **55.3% hit@10** | **5.0–5.5%** | Retrieval MRR / F1 |
| Mem0 | 67.1% | — | LLM judge |
| Zep / Graphiti | 75.1% | — | LLM judge |
| MemHQ | 83.2% | — | LLM judge |

> Methodology note: Competitors (Mem0, Zep, MemHQ) generate answers and score with an LLM judge. Aletheia's numbers use retrieval hit@k + MRR (LoCoMo) and token F1 (BEAM, LongMemEval-S), which are strict retrieval metrics, not generative QA. A direct apples-to-apples comparison requires the same scoring method; the 55.3% is Aletheia's recall quality, not its end-to-end QA accuracy.

## Local run, 2026-11-09 (current code, lexical)

The Colab run above predates 2.1.0. This one is current code, run locally with
`python benchmarks/sample.py --n 500`, over the same LongMemEval-S questions in the local cache.

**Correction, same day.** The table below scored "does the gold answer string appear in the top-10
context". Once the converter was wired to the dataset's `answer_session_ids`, the real retrieval
numbers became visible: **hit@10 97.0% (485/500), MRR 0.907**. The 44.2% was never retrieval quality —
on this set gold answers are bare values like `"2"` or `"$300"`. Both readings are kept below because
the pair of them is the finding. See `docs/design/recall-bolstering-plan.md`.

| Slice | N | hit@10 (evidence) | MRR | answer-in-context | p50 |
|---|---|---|---|---|---|
| LongMemEval-S, all questions | 500 | **97.0%** (485/500, CI 95.1–98.2) | **0.907** | 44.2% | 19.5 ms |
| LoCoMo, first 200 QA pairs | 198 | **62.6%** (124/198, CI 55.7–69.1) | **0.448** | 33.5% | 10.5 ms |

LongMemEval-S by question type (N=500, lexical):

| Category | N | hit@10 | MRR | answer-in-context |
|---|---|---|---|---|
| single-session-user | 70 | 100.0% | 0.971 | 81.4% |
| knowledge-update | 78 | 100.0% | 0.987 | 71.8% |
| single-session-assistant | 56 | 100.0% | 1.000 | 53.6% |
| multi-session | 133 | 98.5% | 0.923 | 36.8% |
| temporal-reasoning | 133 | 95.5% | 0.857 | 21.8% |
| single-session-preference | 30 | 76.7% | 0.533 | 0.0% |

**LoCoMo is where the gap is.** 62.6% on this single-conversation slice, MRR 0.448; the
whole-benchmark run below supersedes it at 64.6% over all ten conversations, and the gap that matters
is complete-evidence recovery, not hit@10. Any recall work should be judged on LoCoMo.

> Correction 2026-11-09: this paragraph previously measured the slice against "the 93.0 floor in the
> parity plan". No such floor exists — `docs/design/competitive-gap-closure-plan.md` sets no numeric
> parity target anywhere, and the figure entered the repo in the same commit that wrote this
> paragraph. It was mine, not the plan's, and it is withdrawn. The only external comparison numbers
> in this file (Mem0 67.1, Zep 75.1, MemHQ 83.2) are answer-level and LLM-judged, so they are not a
> retrieval floor either.

| Slice | N | Exact match | 95% CI | p50 |
|---|---|---|---|---|
| LongMemEval-S, all questions (answer-string scoring, superseded) | 500 | 44.2% (221/500) | 39.9–48.6 | 15.7 ms |
| offsets 0–49 | 50 | 88.0% | 76.2–94.4 | 15.8 ms |
| offsets 200–249 | 50 | 34.0% | 22.4–47.8 | 15.9 ms |
| offsets 400–449 | 50 | 62.0% | 48.2–74.1 | 15.7 ms |

**Read this carefully.**

- **Exact match is a retrieval-contains metric**: the gold answer string appears somewhere in the
  top-10 retrieved context. It is not answer accuracy, and it is not comparable to a competitor's
  LLM-judged number.
- **Token F1 (0.007) and MRR (0.000) are not informative in this configuration.** MRR needs gold
  evidence ids, which this slice does not carry; F1 compares a short gold answer against long
  retrieved context and therefore measures length mismatch.
- **A 50-question slice is not a score.** Three disjoint slices of the same set scored 88%, 34% and
  62% — a 54-point spread — and their 150-question aggregate (61%) sits well above the full-set mean
  (44.2%). Question order in this file is not neutral; the early questions are largely
  single-session. Every rate above needs its N and interval or it is a rumour.
- Against the Colab run's 33.8% EM over the same 500 questions, this is +10.4 points, but the token
  F1 moved the other way (0.014 → 0.007), which is a sign the two runs do not score identically.
  Treat the improvement as indicative, not established, until both runs use one scoring path.

### Hybrid, first honest look — 2026-11-09

The vector arm had never run in a full-set benchmark: `create_substrate` called
`embedder.embed_documents`, the alpha embedder has no such method, and a bare `except: pass` turned
that into "no vectors, no error". Fixed; these are the first numbers from a working vector path.

| Mode | Questions (offset 200–202) | EM | p50 per recall | Wall clock |
|---|---|---|---|---|
| Lexical | 3 | 2/3 | 14.6 ms | 3.5 s |
| Hybrid (bge-m3 + bi-encoder rerank) | 3 | **1/3** | **43,662 ms** | 1,253 s |

Two things, both provisional at N=3:

1. **Hybrid is not a free win.** On these three questions it *lost* a hit that lexical found, and it
   cost about 3,000× the latency. Cause not yet diagnosed — candidates are the fusion weighting, the
   auto-enabled cross-encoder rerank demoting a lexical hit, or vector candidates crowding the
   top-10. Diagnosing it is cheap; assuming it away is not.
2. **It is unaffordable at haystack scale on one machine.** The MLX bge-m3 adapter embeds at ~1.8
   documents/s on real message lengths (up to 5,085 characters each, 460 messages per question),
   which is ~7 minutes per question and ~3 hours for a 25-question run. Batched embedding in the
   adapter, or a GPU, is the prerequisite for any hybrid number worth quoting.

## The suite, on one command — 2026-11-09

`benchmarks/run.py` is now the entrypoint Phase 9 asked for: one documented command, gold answers and
reproduction steps committed (`docs/benchmarks/REPRODUCE.md`), results published here and referenced
from `README.md`.

```bash
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset locomo
```

| Dataset | Metric | Result | N |
|---|---|---|---|
| LoCoMo, whole benchmark, lexical | hit@10 against gold evidence records | **64.4%** (95% CI 61.9–66.7) | 990 / 1,538 answerable |
| LoCoMo | MRR | 0.389 | 1,538 |
| LoCoMo | mean evidence coverage | 0.574 | 1,538 |
| LoCoMo | full evidence recovery | 52.2% | 1,538 |
| LoCoMo, >1 record needed | full evidence recovery | **11.9%** | 427 |
| LoCoMo, no gold answer | correct abstention | **0.0%** | 444 |
| LongMemEval-S | hit@10 against gold evidence sessions | 97.0% (95% CI 95.1–98.2) | 500 |
| BEAM 100K | answer-in-context (proxy) | not a retrieval number | — |

**Two changes from the figures earlier in this file, both deliberate.**

1. **The denominator moved.** The 444 LoCoMo questions with no gold answer are now scored as
   abstention questions rather than as evidence-retrieval questions, which leaves 1,538 answerable
   questions instead of 1,982. That is why the whole-set rate reads 64.4% here and 64.6% there: the
   same run, without the questions whose correct behaviour is to decline.
2. **Abstention is measured, and it fails.** All 444 return results. Recall never declines, so
   abstention accuracy is 0.0%. This is the first time the suite has scored the thing Phase 9's
   "abstention assertions" deliverable was for, and the assertion does not pass.

Gold-token validation is now a check rather than a caveat: the runner verifies every cited evidence id
against the records actually present and reports any it cannot reach. On LoCoMo it finds **5 of 1,982
questions citing ids no record carries** (`…-d10-19`, `…-d`, `…-d-11-26` and two more), so full
recovery is impossible for them by construction. That independently reproduces what the harness audit
found by inspection, and it will now fail loudly if the converter starts dropping ids again.

Gold units are not interchangeable, and the runner labels them instead of emitting one number across
three datasets:

| Dataset | Gold unit | hit@k | coverage | abstention |
|---|---|---|---|---|
| LoCoMo | a record | yes | yes | yes |
| LongMemEval-S | a session (~460 records) | yes | **no** — one session id is hundreds of records, so "coverage" of it is meaningless | yes |
| BEAM | none shipped | **no** — the probing questions carry no evidence ids | no | yes |

LongMemEval's 100% hit@10 is a units artefact worth stating plainly: finding any single message of
the gold session counts as a hit, so the session is "retrieved" once one of its ~460 messages lands.
A coverage column would read near-zero and mean nothing. Reporting that beside LoCoMo's coverage
would be the error this file spends most of its length warning about, so the runner suppresses it
and says why.

## LoCoMo, the whole benchmark — 2026-11-09

`benchmarks/diagnose_locomo.py`, lexical, every conversation, no sampling:

| LoCoMo, lexical, N=1,982 gradable of 1,986 | Value | 95% CI |
|---|---|---|
| hit@10 (gold evidence record) | **64.6%** | 62.5–66.7 |
| MRR | 0.394 | — |
| mean evidence coverage | 0.591 | — |
| full recovery (all gold records retrieved) | **54.8%** | — |
| p50 recall | 21.6 ms | — |
| wall clock | 57 s | — |

Reproduce: `python benchmarks/diagnose_locomo.py --n 1986 --configs lexical`

**The 62.6% was a contiguous 200-question slice: 199 questions from `conv-26` and one from
`conv-30`.** The ten conversations carry 199, 105, 193, 260, 242, 158, 190, 239, 196 and 204 pairs.
The full set reads 2.0 points higher with an interval about 3.2 times narrower (13.4 points wide
against 4.2), so the earlier figure was thin rather than wrong — but it was never a LoCoMo rate and
should not be quoted as one.

The category names below are **inferred from question and answer content**, not from a shipped key:
the dataset carries bare integer codes and neither the cache nor the converter documents a legend.
They are consistent with the content but they are an interpretation, not a fact.
|---|---|---|---|---|---|---|---|
| 4 | single-hop | 841 | 46 | 69.2% | 0.677 | 66.0% | 0.445 |
| 2 | temporal | 321 | 40 | 67.9% | 0.648 | 62.0% | 0.402 |
| 5 | adversarial: 444 lack an answer field, the other two answer "No" | 446 | 14 | 65.7% | 0.648 | 63.9% | 0.412 |
| 1 | multi-hop | 282 | 277 | 54.3% | 0.287 | **11.0%** | 0.264 |
| 3 | inference / counterfactual | 92 | 50 | 38.0% | 0.257 | 17.4% | 0.204 |

**hit@10 is too generous, and this is the finding that matters.** It scores a question as retrieved
when *any one* gold record lands in the top 10. Split by how many records the question actually
needs:

- **single-gold questions** (n=1,555, 78% of the set): 66.6% hit@10 (CI 64.2–68.9), and there
  coverage equals the hit rate because one record is all there is to find.
- **multi-gold questions** (n=427): 57.6% hit@10 (CI 52.9–62.2) but mean per-question coverage
  **0.317** and full recovery **11.9%** (51 of 427; 181 of them recover nothing at all). "A third" is
  a mean, not a bound and not a typical case: coverage is bimodal, not uniform across these
  questions.

So the aggregate 64.6% is carried by single-fact questions. Complete-evidence retrieval on the
questions that need more than one record sits near 12% — and that is a retrieval statement only.
Whether a reader could answer from partial or alternative evidence was not measured here, and
neither was answer generation. Excluding category 5 barely moves the aggregate: 64.3% (CI
61.9–66.7) against 64.6%.

## Lexical vs vector vs fusion on LoCoMo — 2026-11-09

**Superseded as evidence — kept as a lesson in sampling.** Ten questions from each of the ten
conversations (N=100), one substrate and one embedding pass per conversation, every configuration
scored against the same gold record ids.

**This sample is not representative and the numbers from it are scoped to it.** Taking the first ten
questions of each conversation produced a category mix of 41/43/14/2/0 across categories 1–5, against
282/321/96/841/446 in the benchmark: it nearly excludes the largest category and completely excludes
the fifth. The intervals below are approximate asymmetric Wilson intervals and do not account for
the deterministic selection or for questions clustering inside conversations. A random, seeded
sample re-run appears at the end of this section.

| Config | hit@10 | 95% CI | MRR | p50 | vs lexical (gained/lost) | sign test |
|---|---|---|---|---|---|---|
| lexical | 56.0% | 46.2–65.3 | 0.328 | 27 ms | — | — |
| vector | 66.0% | 56.3–74.5 | 0.365 | 321 ms | 16 / 6 | p=0.052 |
| hybrid (no rerank) | 64.0% | 54.2–72.7 | **0.416** | 323 ms | 11 / 3 | p=0.057 |
| hybrid + bi-encoder rerank | 64.0% | 54.2–72.7 | 0.393 | 3,104 ms | 11 / 3 | p=0.057 |

Three readings, in order of how much they should change the plan:

1. **On this sample fusion gained eight net hits and the paired test does not reach 0.05.** 11
   questions gained against 3 lost is a net +8 at N=100; the exact two-sided sign test gives p=0.057
   under an independence assumption these clustered questions do not satisfy. A benchmark-wide
   improvement is not established. The plausible explanation — that LoCoMo's questions paraphrase
   short conversational turns, where BM25 alone is weak — is a hypothesis about mechanism, not a
   measured cause, and it should not be reported as the reason.
2. **On these 100 questions the rerank changes no hit outcomes and costs a great deal.** `hybrid` and
   `hybrid+rerank` carry identical hit flags on every sampled question, but their MRRs differ: 0.416
   against 0.393, so the rerank is not inert with respect to ranking — it makes the ordering worse
   while leaving the top-10 membership alone. It also costs 3,104 ms against 323 ms per query. That
   is a measured absence of benefit on this sample. It is not a proof of the band-limit invariant:
   that invariant lives in `tests/test_rerank_fusion.py`, not in this artifact, and this artifact
   contains no candidate-order trace.
3. **Fusion lost 3 questions that lexical found and gained 11 the other way.** That is the measured
   count and nothing more. Attributing the 3 to RRF crowding-out would need candidate-level evidence
   — which list each record appeared in and at what rank — and this artifact records no retrieved id
   lists and no scores. Nor does a small observed loss count bound what re-weighting could recover on
   the many questions both configurations currently miss; that comparison was not run.

None of this touches the coverage finding above: fusion gains are hit@10 gains on any-single-record
matching, and the multi-gold questions are still the ones nobody can answer from what we return.

## Is the coverage gap depth or ranking? — 2026-11-09

Same lexical run, full set, three depths. This decides what a coverage fix would even be:
`python benchmarks/diagnose_locomo.py --n 1986 --limit 25 --configs lexical`

| Depth | any-hit | coverage | full recovery | multi-gold: any-hit | multi-gold coverage | multi-gold full recovery |
|---|---|---|---|---|---|---|
| 10 | 64.6% | 0.591 | 54.8% | 57.6% | 0.317 | 11.9% |
| 25 | 73.2% | 0.673 | 62.2% | 71.2% | 0.441 | 20.4% |
| 50 | 78.3% | 0.726 | 67.1% | 78.7% | 0.526 | 26.9% |

**Mostly ranking, partly depth.** Five times the slots takes multi-gold full recovery from 11.9% to
26.9% — a real gain, and still barely a quarter; at depth 50 we hold 53% of a multi-evidence
question's records and a third of all questions still cannot be fully covered. Since the ranking is
precision-shaped (it returns successive variations on the best-matching record rather than a set that
covers the question), raising `limit` buys about 12 points of full recovery across the whole set and
then flattens. The measurement points at coverage-aware selection — diversifying the top-k — judged on
full-recovery rate rather than hit@10.

Gold counts across the 1,982 gradable questions: 1,555 need one record, 241 need two, 83 three, 60
four, 20 five, and a tail out to nineteen.

## The reading stage, first measurement — 2026-11-09

The reader now exists (`aletheia/reader.py`, `PROMPT_VERSION = reader.v1`) and runs against a live
model. This is its first number, on a 20-question seeded random sample, and it is bad.

**Pinned reader:** `openai-compatible:qwen3.8-flash` via Alibaba's token-plan endpoint
(`https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`), temperature 0, 90 s
timeout. Configured through `ALETHEIA_LLM_PROVIDER` / `ALETHEIA_LLM_MODEL` / `ALETHEIA_LLM_BASE_URL`
and `ALETHEIA_LLM_API_KEY_ENV` — the key is referenced by variable name, never committed.

| LoCoMo, N=20 seeded random, lexical k=10 | Value |
|---|---|
| retrieval hit@10 (same run) | 13/15 = 86.7% |
| reader answered | 10 of 20 |
| reader abstained | 10 of 20 |
| exact match against gold | **20.0%** (3/15, CI 7.0–45.2) |
| token F1 | **0.301** |
| cited a gold record | 66.7% |

Read the three columns together, because that is the finding: **the evidence was usually retrieved
(86.7%) and the reader usually cited a gold record (66.7%), yet it abstained on half the questions and
answered correctly by exact match only a fifth of the time.** One case makes the shape unmistakable —
gold answer `"California"`, the gold record was retrieved, the reader cited it, and it returned an
empty answer anyway. That is not a recall problem; it is the reading stage failing with the evidence in
hand, which is exactly what the parity spec predicted the reading stage would show.

Two caveats that cut both ways, stated rather than buried:

- **Exact match is brutally strict here.** LoCoMo gold answers are bare spans — `"Three dogs."`,
  `"\"Finding Freedom\""`, `"2022"`. "He met a Canadian woman and they married" against a gold answer
  of "Evan met the woman he would marry" scores zero on exact match and is not wrong. Token F1 (0.301)
  is the fairer interim read, and neither is a substitute for the judged score the spec requires.
- **N=20 with an interval from 7% to 45%.** This establishes that the pipeline works and that the
  reading stage is currently weak. It does not establish a rate.

**No judge exists yet, so none of this is comparable to Mem0, Zep or MemHQ.** The artifact records
`comparable_to_published_answer_scores: false` and `judge: null`. Publishing an unjudged answer number
beside their judged ones would be the same category error this file spends its length warning about.

### Two defects this run found and fixed

1. **The reader inherited the transport's 30 s timeout**, and two of the first twenty questions died
   with `TimeoutError` — an infrastructure failure that would have read as a reader failure had the
   harness not counted errors separately. Raised to 90 s for a ten-item reasoning prompt; the re-run
   had 0 errors.
2. **The artifact kept only aggregates.** A reading number whose failures cannot be inspected is
   unauditable, and the parity spec requires publishing the failures. `run.py` now stores
   per-question outcomes — answered/abstained, the text, the citations, whether a cited record was a
   gold record, and the retrieval outcome for the same question — which is how the
   cited-the-gold-record-and-still-abstained case above was found at all.

### The open question this raises

An abstention rate of 10/20 on questions whose evidence was retrieved is high enough that the prompt
is a candidate cause: it instructs the reader not to answer unless the evidence is sufficient, and a
model facing ten loosely-relevant conversation turns may reasonably judge it insufficient. Tuning that
is a **prompt version change** (`reader.v2`), not a tweak — the spec's rule is that a stage-2 number is
only comparable to another taken with the same prompt version, so both numbers get published with
their versions attached. No tuning has been done yet, and none should be done to improve a number
without saying so.

## Audit of the LoCoMo harness — 2026-11-09

Two independent adversarial audits ran against this work before it was trusted: one on whether
`diagnose_locomo.py` measures what it claims, one on whether every number above survives the JSON
artifacts. Verdicts: **NOT TRUSTWORTHY** on the harness as first written, **CLAIMS HOLD WITH
CORRECTIONS** on the documentation. Everything below was found by those audits and fixed here.

### Defects that were fixed

1. **A dead retrieval stage scored as a miss, not an error.** `aletheia/retrieval.py` catches every
   non-ValueError from its own candidate queries and substitutes an empty list, so a broken FTS index
   or a failing embedder returns an empty result that the harness recorded as an ordinary miss with
   `errors=0`. Reproduced by injecting `RuntimeError` into `Substrate.fts_candidates`: the run reported
   12 questions, 0 hits, 0 errors, exit 0. That is an infrastructure failure presented as a retrieval
   regression. `diagnose_locomo.py` now runs a per-conversation preflight through both primitives and
   marks every question in a conversation whose retrieval is dead as an error rather than a miss.
   Same injection after the fix: `N=0, err=8, nothing scoreable`.
2. **`lexical+rerank` silently did not rerank.** The embedder gate derived its answer from the
   recipe's kwargs, and that recipe carries `mode="lexical"`, so a run requesting only `lexical` and
   `lexical+rerank` loaded no embedder and executed plain lexical twice under two labels. Fixed by
   naming the embedder-free configurations explicitly. Verified: the same pairing now costs 2,620 ms
   against 15.5 ms per query, which is what actually reranking looks like.
3. **Errors were excluded from the denominators and from the pairing.** A configuration that failed
   the exact questions it lost could report no losses at all; two of three questions scored as 100%.
   Failures now count against the denominator, `err` is a column, and pairing runs over every
   gold-bearing question both configurations attempted with failures counted as misses and disclosed
   as `pairs_with_errors`.

### Caveats that are not fixable here, but change what the numbers mean

4. **Gold ids sometimes identify an extracted observation rather than the cited dialogue turn.**
   `benchmarks/full/locomo.py` lets `observation` records claim dialogue evidence ids first and drops
   the raw turn when the id collides, so 2,387 raw turns are replaced by different text and 1,675
   questions reference at least one replaced id. One example: the gold id for the first question of
   `conv-26` cites a turn reading "yesterday" but the retained record reads "recently". Retrieval of
   that id is retrieved *something*, but not necessarily the sentence the question was written
   against. Every coverage and full-recovery figure here is over that transformed corpus.
5. **A few gold ids cannot exist at all** — five questions reference malformed ids (`…-d`, `…-d-11-26`
   and similar) that no record carries, so full recovery is impossible for them by construction.
   Separately, five questions carry distinct gold ids whose records hold identical text, so recovering
   the sentence once scores as partial coverage. Set arithmetic is not the same as semantic
   completeness.
6. **Four of the 1,986 questions carry no gold ids**, so they are excluded from every rate above — but
   they are still scored by the answer-string fallback in `benchmarks/full/common.py`, which means
   `per_question` in the JSON is not uniformly id-scored. One of the four reads `hit: true` by that
   fallback. Any later analysis reading `per_question` directly must filter on `gold`, not on the
   presence of `hit`.

### What survived

The full-set lexical result reproduces exactly after the fixes: 1,982 scored, **0 errors**, hit@10
0.646, MRR 0.394, coverage 0.591, full recovery 54.8%. The two independent full-set runs (431 s run,
coverage run, verified run) agree on every per-question hit and MRR flag. p50 recall varies between
14.2 and 21.6 ms across runs on this machine depending on memory pressure, so it should be read as an
order of magnitude, not a figure.

### The comparison that counts: seeded random sample, N=120

The prefix sample above is superseded as evidence. This one draws 120 of the 1,986 questions uniformly
at random with `--seed 1`, so its category mix (22/19/6/49/24 across categories 1–5) tracks the
benchmark's own (282/321/96/841/446) rather than the prefix's 41/43/14/2/0. Ten conversations, one
substrate and one embedding pass each, no errors, no preflight failures.

| Config | hit@10 | 95% CI | MRR | coverage | full recovery | p50 | vs lexical (gained/lost) | sign test |
|---|---|---|---|---|---|---|---|---|
| lexical | 66.7% | 57.8–74.5 | 0.410 | 0.595 | 52.5% | 20 ms | — | — |
| vector | 65.0% | 56.1–72.9 | 0.387 | 0.580 | 52.5% | 296 ms | 15 / 17 | p=0.86 |
| hybrid (no rerank) | **73.3%** | 64.8–80.4 | **0.464** | **0.655** | **58.3%** | 299 ms | 15 / 7 | p=0.134 |
| hybrid + bi-encoder rerank | 73.3% | 64.8–80.4 | 0.443 | 0.655 | 58.3% | 2,742 ms | 15 / 7 | p=0.134 |

What this settles and what it does not:

- **Vectors alone are a wash.** 15 questions gained against 17 lost, p=0.86. Whatever fusion buys, it
  is not the vector arm on its own.
- **Fusion is ahead by 6.6 points and it is still not significant** (15 gained, 7 lost, p=0.134 at
  N=120). Both samples point the same way — the prefix sample gave +8.0 at p=0.057 — and neither
  establishes it. A benchmark-wide fusion claim still needs a larger random sample sized by a power
  calculation rather than by hope.
- **The rerank finding replicates.** Identical hit outcomes and identical paired counts to the
  no-rerank row on two independent samples, MRR lower both times (0.416→0.393, 0.464→0.443), at nine
  to ten times the latency. That is now a replicated result rather than a single sample.
- **Coverage improves slightly with fusion** (0.595 → 0.655, full recovery 52.5% → 58.3%) and remains
  the real problem: on this sample's 34 multi-gold questions, hybrid reaches 76.5% any-hit but only
  0.488 mean coverage and 23.5% full recovery.

### Machine note

This box has 8 GB and sits under real swap pressure, which shapes what is affordable to measure.
Completed runs only: a 736-record conversation built in 65 s with vectors (about 11.3 records/s) in
the full lexical run that still embedded, and in 73 s in the comparison run. The lexical-only pass
over all 1,986 questions takes 57 s, against 431 s for the same pass while building vectors — that
pair, both from completed runs, is the measured cost of the embedder here. An earlier figure in this
section cited a 96 s build from a run that was killed partway and has no companion result; it is
withdrawn (it implies 7.7 records/s). A claim that per-question recall degrades from 0.37 s to ~5 s
was never established by a controlled comparison and is withdrawn too; the completed comparison
reports 27 / 321 / 323 / 3,104 ms across its four modes, which is the defensible statement.
`diagnose_locomo.py` now loads the embedder only when a requested configuration uses it. Batched
embedding in the adapter is a proposed mitigation, not an experimentally established prerequisite.

