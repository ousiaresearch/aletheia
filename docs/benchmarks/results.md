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

### Query expansion, attempted and measured — 2026-11-09

The 74%-unexplored finding pointed at query expansion, and `aletheia/query_expansion.py` already had
the machinery — it had simply never been given a working provider. `run.py --expand` now wires the
pinned reader model into it (paraphrase expansion, HyDE and multi-hop decomposition, fused by RRF).
Same 40 questions, seed 2:

| | lexical | + expansion |
|---|---|---|
| hit@10 | 60.0% | 63.3% |
| MRR | 0.371 | **0.327** |
| coverage | 0.519 | 0.567 |
| full recovery | 46.7% | 50.0% |
| expansion applied to | — | 40 of 40 questions |

**A wash with a ranking cost.** One more question retrieved and one more fully recovered, on N=30
gradable — a single question either way at 3.3 points each — while MRR fell 0.044. The intervals
overlap almost entirely (42.3–75.4 against 45.5–78.1). This does not justify expansion as a default.

The reason is visible in what expansion can and cannot produce. Paraphrase expansion and HyDE generate
different *phrasings* of the question; the multi-hop decomposition generates *sub-questions*, but it
only fires on questions containing and/or/also/then/after/before. LoCoMo's hard multi-hop questions are
not compound sentences — "What fields would Caroline be likely to pursue in her education?" — so the
decomposition that would help never triggers.

More fundamentally, **a record sharing no terms with the question is never a lexical candidate at all**,
so no amount of rephrasing the query reaches it. That is the mechanism behind the 74% in unexplored
sessions, and it bounds what query work can do. The levers that can reach such a record are semantic
candidate generation (the vector arm) or entity-hop traversal — and the vector arm is the only one
already measured to help: on the representative N=120 sample, hybrid raised coverage 0.595 → 0.655 and
full recovery 52.5% → 58.3%, at ~15x the query latency.

### Mechanisms do not compose — more retrieval machinery made answers worse — 2026-11-09

Every mechanism that showed a gain was switched on together, on the same 40 questions: deep pool (depth
50), cross-encoder rerank, one hop round, reader.v3.

| Configuration | hit@10 | MRR | coverage | full recovery | **judged accuracy** |
|---|---|---|---|---|---|
| reader.v2 alone | 60.0% (18/30) | 0.371 | 0.519 | 46.7% | **60.0%** (18/30) |
| reader.v3 alone | 60.7% (17/28) | 0.362 | 0.539 | 50.0% | **60.7%** (17/28) |
| `qwen3.8-max` reader | 58.6% (17/29) | 0.367 | 0.503 | 44.8% | **62.1%** (18/29) |
| hop + reader | 63.3% (19/30) | 0.375 | 0.561 | 50.0% | **56.7%** (17/30) |
| deep pool + cross-encoder | **66.7% (20/30)** | 0.462 | **0.628** | **56.7%** | not yet measured |
| deep pool + CE + hop + reader | 62.1% (18/29) | **0.471** | 0.580 | 51.7% | **48.3%** (14/29) |

**The best retrieval configuration produced the worst answers.** Adding the hop to the winning
retrieval setup cost two gold records and roughly five judged-correct answers; judged accuracy fell from
60.0% to 48.3% while MRR went *up*. Retrieval quality and answer quality moved in opposite directions.

The mechanism is now visible across four independent observations: **whenever the reader is handed more
or different records, conversion falls.** The crosstab said the reader converts ~76% of what it is
given; these runs say that figure is not constant — marginal extra records actively dilute it. The hop
adds records, and the deep pool adds *candidates*, and what reaches the reader changes character even
when hit@10 rises.

That reframes the goal rather than closing it. "Highest answer-level accuracy" is not achieved by
maximising retrieval recall; it needs retrieval *precision at the reader's interface* — a small, clean
evidence set. The next measurement is reader depth: the same deep-pool + cross-encoder configuration,
cutting to five items instead of ten before the reader sees them.

The one number that did improve in the stacked run is MRR (0.471) and judge repeat agreement slipped to
93.1% there, from 100% in every other run — both noted, neither explained.

### The cross-encoder was never the problem; the pool was — 2026-11-09

The cross-encoder looked useless earlier: it lifted MRR and coverage while leaving hit@10 at 18/30. The
reason was not the reranker but what it had to work with — `recall` builds a candidate pool of
`limit * 6 = 60` records, so a reranker could only reorder sixty candidates before ten were cut.

Retrieving deep and scoring shallow separates the two. `run.py --recall-depth 50` retrieves into a pool
of ~300 and still scores at 10, so `hit@10` keeps its meaning:

| Same 40 questions, seed 2 | baseline (depth 10) | depth 50, no reranker | **depth 50 + cross-encoder** |
|---|---|---|---|
| hit@10 | 60.0% (18/30) | 60.0% (18/30) | **66.7% (20/30)** |
| MRR | 0.371 | 0.371 | **0.462** |
| mean coverage | 0.519 | 0.519 | **0.628** |
| full recovery | 46.7% | 46.7% | **56.7%** |
| category 2 (temporal) | 40.0% | 40.0% | **80.0%** |

**Deep retrieval alone changes nothing — the numbers are identical to baseline, digit for digit.** The
entire gain comes from reranking a wider pool. That is the cleanest attribution in this file: one
variable moved, one variable moved the result.

It is also the strongest retrieval result measured here — +2 questions, +24% relative MRR, +0.109
coverage and +10 points of full recovery. For comparison: one hop round also gave +2 questions but with
far less MRR and coverage movement, and three hop rounds gave nothing at all.

Two things follow. First, the production configuration this implies is that `recall` should build a
wider pool and rerank it when a cross-encoder is available — the current `limit * 6` pool leaves most of
the reranker's value on the floor. That is a change to shipped behaviour and belongs behind its own
review, not in a benchmark commit. Second, the answer-level question is now open again: hit@10 moved,
and the cross-tab says answers follow hit@10, so a judged run at this configuration is the next
measurement rather than an assumption.

### More hop rounds do not help — the reasoning approach saturates — 2026-11-09

One round of "what else should I look up" moved hit@10 from 18 to 20 of 30. Two more rounds, on the
same questions:

| | baseline | hop x1 | hop x3 |
|---|---|---|---|
| hit@10 | 60.0% (18/30) | 66.7% (20/30) | 66.7% (20/30) |
| coverage | 0.519 | 0.578 | 0.577 |
| full recovery | 46.7% | 50.0% | 50.0% |
| category 1 (multi-hop) | 66.7% | 77.8% | **88.9%** |
| category 2 (temporal) | 40.0% | 80.0% | 60.0% |

**The second and third rounds find nothing.** The model issued 2.92 rounds on average — it kept
generating queries — and hit@10, coverage and full recovery are identical to one round. Additional
reasoning about what is missing stops paying immediately.

Two details worth keeping: the extra rounds *did* move multi-hop (77.8% → 88.9%, one more question of
nine) while costing temporal (80% → 60%), which nets to zero — the hop's queries drift toward the
conversation's people and away from its timeline. And one hop call failed even here, which the run
counted rather than hiding.

So the default stays at one round, and the reasoning-based approach is exhausted at that point. What
remains is index-side: the candidate pool is `limit * 6 = 60` records, and the cross-encoder can only
reorder what is inside it. A larger pool with cross-encoder reranking is the one untested configuration
that could change *membership* of the top 10 rather than its order.

### Answer accuracy tracks retrieval almost exactly — 2026-11-09

The most useful analysis in this file, and it corrected a conclusion drawn an hour earlier. Cross-
tabulating judged correctness against whether the gold evidence was retrieved, over three
configurations on the same 40 questions:

| Configuration | judged accuracy | retrieval hit@10 | accuracy *given* evidence retrieved |
|---|---|---|---|
| reader.v2 alone | 60.0% (18/30) | 60.0% | **77.8%** (14/18) |
| second hop + reader | 56.7% (17/30) | 63.3% | **73.7%** (14/19) |
| reader.v3 alone | 60.7% (17/28) | 60.7% | **76.5%** (13/17) |

Three things follow, and the first two are uncomfortable.

1. **The aggregate answer number is pinned to hit@10.** It reads 60.0% against 60.0%, 56.7% against
   63.3%, 60.7% against 60.7%. Any claim that "retrieval is no longer the binding constraint" was wrong;
   it was drawn from a single hop experiment and the cross-tab contradicts it.
2. **The hop raised retrieval and lowered conversion.** Conditional accuracy fell from 77.8% to 73.7%
   with the hop in place: its extra records are sometimes noise that confuses the reader. Improving a
   ranker and a reader independently is not the same as improving the pair.
3. **The reader converts about 76% of what it is handed**, with 3–4 questions answered correctly even
   without a retrieval hit (adjacent context supplying the fact). So reading is a real but secondary
   loss: roughly a quarter of retrieved evidence, against a third of questions whose evidence is never
   retrieved at all.

**Therefore answer-level work is retrieval work**, and it must be judged on hit@10 specifically. Three
reader changes — the abstention fix, the hop's better evidence, and the list/absolute-date instructions
— all land at 60%, which is what a ~60% retrieval rate plus a ~76% conversion rate predicts.

### Retrieval gains do not pay out at the answer level — 2026-11-09

The hop retrieves more evidence. Does it produce better answers? Same 40 questions, same reader, same
judge, same protocol:

| | reader alone | hop + reader |
|---|---|---|
| retrieval hit@10 | 60.0% (18/30) | 63.3% (19/30) |
| questions answered | 24–26 of 40 | 27 of 40 |
| judged accuracy, `qwen3.8-flash` judge | 18/30 = **60.0%** | 17/30 = **56.7%** |
| judged accuracy, `glm-5.3` judge | 13/21 = 61.9% | 10/17 = 58.8% |
| repeat agreement | 100% | 100% |

**Two more gold records retrieved produced one fewer correct answer.** Both judges agree on the
direction, both at 100% repeat agreement, and both differences sit inside noise at N=30 — but there is
no gain to report, and the honest reading is that retrieval is no longer the binding constraint on
answer accuracy.

That is the stage-isolation logic from the parity spec, measured in our own system: answer accuracy is
*bounded* by retrieval but not *implied* by it. LongMemEval showed the same shape long before this
(97.0% hit@10 against 44.2% answer-in-context); LoCoMo confirms it with a judged score.

So the next answer-level lever is the reader, not the ranker — and the two obvious candidates are its
capability (the pinned model is a flash tier) and its habit of declining: 13 to 16 of 40 questions still
end in abstention despite retrieved evidence.

### Second-hop retrieval — the first retrieval lever that works — 2026-11-09

Three cheaper mechanisms were measured and failed: top-k diversification (addresses 26% of misses),
paraphrase/HyDE expansion (a wash), and entity co-mention (6.9% of misses once the bridge entities are
selective). They share one flaw — none of them can reason about what the missing evidence would *say*.

`run.py --multihop` does. After a first pass it asks the reader model, given the question and the top
six items, what else it would need to look up, takes a 2–6 word reply, retrieves on it, and merges the
result so the strongest seven first-hop matches keep their slots and the hop competes for the rest. One
extra LLM call and one extra recall per question.

Same 40 questions, seed 2, two runs (the hop query is model-generated, so a single run is not a result):

| | baseline | hop run 1 | hop run 2 |
|---|---|---|---|
| hit@10 | 60.0% (18/30) | 66.7% (20/30) | 69.0% (20/29) |
| coverage | 0.519 | 0.578 | 0.632 |
| full recovery | 46.7% | 50.0% | 58.6% |
| MRR | 0.371 | 0.379 | 0.362 |
| hop issued | — | 24/40 | 24/40 |

**The hit count is identical across both runs — 20 questions retrieved, against a deterministic
baseline of 18.** That is the part that matters: the baseline's retrieval is deterministic (three runs
all read 18/30, 0.519, 46.7%), so a stable +2 questions is a real shift rather than generator noise,
even though coverage and full recovery vary more between the two hop runs (the second hop changes which
*extra* records are found, and that part is model-dependent).

It also lands where the mechanism predicts. Category 1 (multi-hop) goes 66.7% → 77.8% hit@10 and 33.3% →
44.4% full recovery, identically in both runs; category 2 (temporal) goes 40% → 75–80%. Category 3,
three questions, went 33.3% → 0% — one question lost, which at n=3 is noise and is reported rather than
smoothed.

At N=30 an interval still spans roughly 49–83%, so this is a direction with a mechanism and a stable
signal, not a significant improvement. It is, however, the first retrieval change in this programme that
moved the number the right way while doing what it was designed to do.

**One failure mode found and handled.** A hop call timed out on one question in run 2. The hop is an
enhancement, so its failure now degrades to first-hop retrieval instead of costing the question — but it
is recorded as `hop_failure` and counted in the summary, because a silently skipped enhancement is the
defect class this repository keeps meeting.

### Entity co-mention, and why 74% was an illusion — 2026-11-09

The obvious bridge across the wall is the entity graph: the missing record sits in another session, but
it shares a person or a topic with what we already retrieved. `diagnose_hops.py` measures exactly that
— for each missed gold record, does it mention an entity the top-k already mentions?

| Entity document-frequency ceiling | Missed gold records reached by co-mention |
|---|---|
| 60% of records (effectively no filter) | 676 / 913 = **74.0%** |
| 15% | 86 / 913 = **9.4%** |
| 5% | 63 / 913 = **6.9%** |

**The 74% is an artefact of ubiquitous names.** At that ceiling the bridge entities are `caroline` and
`melanie` — the two people the conversation is about, present in 46% and 31% of its records. "Any record
mentioning Caroline" is not a selective signal; it is most of the corpus. Cut the ceiling to something
that would actually rank candidates and the reachable share collapses by an order of magnitude.

So entity co-mention can address under a tenth of the coverage gap, and it is not built. This also
exposed a real weakness in `extract_entities`: it keeps any capitalised token longer than two
characters past a short interrogative list, which is why `wow`, `thanks`, `that` and `they` are
"entities" in the graph. That is a product defect worth fixing on its own merits, not a benchmark one,
and it means the graph's precision is lower than its link count suggests.

### The vector arm against the 75% wall — 2026-11-09

`diagnose_coverage.py --mode hybrid` reruns the same attribution on the same 427 multi-record
questions with the embedding arm in the candidate pool:

| 427 multi-record questions | lexical | hybrid |
|---|---|---|
| full recovery | 11.9% | **16.2%** |
| missed gold records | 913 | 838 |
| … in a session the top-k reached | 236 (25.8%) | 210 (25.1%) |
| … in a session never reached | 677 (74.2%) | 628 (74.9%) |
| distinct sessions per top-10 | 8.59 | 8.51 |

Optimistically, that is +4.3 points of complete-evidence recovery and 75 fewer missed gold records, and
it lands where it should: category 1 (multi-hop) goes 29/277 → 40/277 fully recovered, category 4 10/46
→ 16/46.

Pessimistically — and this is the part that matters — **the share of misses in never-reached sessions
does not move at all**: 74.2% → 74.9%. Adding semantic candidates retrieves more of what was already
within reach and does not break the wall. Both arms are bounded by the same thing: the second hop of a
multi-hop question shares neither terms nor topic with the question, and in a conversation of 500–800
turns the chance that any single global ranking surfaces it is small.

That reframes what "highest retrieval accuracy" can mean here. On the evidence gathered, the remaining
points are not in a better global ranker — they are in **structured retrieval**: locating the hop
through an entity or relation rather than through similarity to the question. Aletheia already stores
the substrate for that (`entity_link`, `graph`, `relate`) and recall reaches the graph only as bounded
context, ranked after every match. Making graph-reached records able to occupy match slots is the
change with a mechanism behind it, and it collides with the existing invariant that a neighbour never
displaces a match — which is a design decision to take deliberately, not to slip in.

### A judge for stage 2 — 2026-11-09

`benchmarks/judge.py` implements the judged score parity-measurement-spec.md §3 requires: a pinned
model and prompt version, a repeatability check (n judgements per case with the agreement rate
published, and `supportable` false below 90%), and the judged-wrong examples stored rather than
summarised away. It is deliberately shown only question, gold answer and prediction — never the
retrieved evidence, because a judge that sees the evidence can reward an answer the evidence supports
over one that matches the gold answer, which is a different question than the benchmark asks.

No judged number has been published yet: the first judged run is the next measurement, and its
agreement rate decides whether it may be quoted at all.

### reader.v2 — same questions, same retrieval, better reading

Developed on dev slice `--seed 2` (N=40) and compared against a `reader.v1` baseline taken on the
identical questions. A held-back slice (`--seed 99`) is untouched until this work stops moving, so the
reported number is not the one being tuned on.

| Same 40 questions, seed 2 | reader.v1 | reader.v2 | |
|---|---|---|---|
| retrieval hit@10 | 60.0% (18/30) | 60.0% (18/30) | unchanged — the date plumbing did not move retrieval |
| coverage / full recovery | 0.519 / 46.7% | 0.519 / 46.7% | unchanged |
| questions answered | 22 of 40 | **26 of 40** | 6 newly answered, 2 newly abstained |
| exact match | 20.0% (CI 9.5–37.3) | **30.0%** (CI 16.7–47.9) | +10 points |
| token F1 | 0.374 | **0.541** | +0.167 |
| cited a gold record | 50.0% | 50.0% | unchanged |

Two changes produced it, both capability fixes rather than test-fitting:

1. **Session dates recovered.** LoCoMo ships `session_N_date_time` per session ("1:56 pm on 8 May,
   2023") and the converter discarded them, so 321 temporal questions were asked of evidence carrying
   no date. The dates now travel in `provenance["when"]` and `effective_at`, and the reader sees
   `[3] (Caroline, 1:56 pm on 8 May, 2023) I went to the support group yesterday.` — the same omission
   as the LongMemEval converter dropping `haystack_dates`.
2. **The abstention instruction was pushing the reader to decline.** v1 told it not to answer unless
   the evidence was sufficient, and it abstained on 18 of 40 questions whose evidence had been
   retrieved. v2 answers when the evidence supports an answer and abstains only when the specific fact
   is absent.

**Retracted, same day — the reader is not deterministic, and the effect was the size of the noise.**
Two runs of *identical* configuration (reader.v2, qwen3.8-flash, temperature 0, same 40 questions, same
retrieved items, same prompt version) disagreed:

| | v2 run 1 | v2 run 2 |
|---|---|---|
| questions answered | 26 of 40 | 24 of 40 |
| exact match | 30.0% (16.7–47.9) | **20.0%** (9.5–37.3) |
| token F1 | 0.541 | 0.450 |
| cited a gold record | 50.0% | 56.7% |

Paired per question: **2 abstention flips and 9 of the 24 answered texts differ** — `"two"` vs `"2"`,
`"last year"` vs `"2022"`, `"weight training"` vs `"weight training classes"`. Temperature 0 does not
make a hosted model deterministic; batching and routing are not under our control.

So the v1 → v2 improvement of 10 exact-match points is **the same magnitude as the difference between
two identical v2 runs**, and the claim that v2 improved exact match is withdrawn. What survives is the
mechanism — v1 abstained on 18 of 40 questions whose evidence had been retrieved, which is a defect
either way — and the honest statement is that its measured effect is not separated from reader noise at
this sample size. Separating it needs k repeated runs per configuration, reported as a range, which is
now part of the protocol below.

**Exact match was measuring surface form, not correctness.** The judge scores the same 30 gradable
answers at 60.0% while exact match scores them at 20.0%: `"two"` against a gold `"2"` is a correct
answer marked wrong. Token F1 splits the difference and is still a proxy. This is the argument for the
judge that the spec predicted, made with our own numbers.

### First judged answer-level number — 2026-11-09

`benchmarks/judge.py`, prompt `judge.v1`, `--repeat 2`, on the 30 gradable cases above:

| Judge | Judged accuracy | Repeat agreement | Unusable judgements |
|---|---|---|---|
| `qwen3.8-flash` (same family as the reader) | 18/30 = **60.0%** | 100% | 0 |
| `glm-5.3` (independent family) | 13/21 = **61.9%** | 100% | 18 of 60 calls |

Two judges from different families land within 1.9 points of each other, which is the check that
matters: a reader judged only by its own model could be flattering itself, and here it is not. Both
report 100% repeat agreement, so this judge can support a claim; attention should go to `glm-5.3`'s 30%
unusable-output rate, which is a judge-reliability problem rather than an accuracy one.

**This is the first apples-to-apples-shaped number this project has had**: answer-level, judged, with
the judge declared and its repeatability published. It is still N=30 on a dev slice, from a reader whose
own run-to-run spread is ±7 exact-match points, and it says nothing about Mem0, Zep or MemHQ because
their questions, scorer and judge are not ours.

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

