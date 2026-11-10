# Bolstering recall — what the numbers say, and what to do next

Written 2026-11-09, after the first benchmark runs on current code. Every figure here is measured on
this machine and reproducible with the commands named beside it. Where a number comes from someone
else's paper it says so.

## 1. The measurement was wrong, and that is the most important finding

For two days the working number for this repository was **LongMemEval-S exact match 33.8%** (the
Colab run in `results.md`) and then **44.2%** on current code. Both were computed by asking whether
the *gold answer string* appears somewhere in the retrieved context.

That is not retrieval quality. On this set the gold answers are things like `"2"`, `"20"`, `"$300"`.
A bare digit either appears by coincidence or does not appear at all, and the metric says nothing
about whether the evidence session was found.

The dataset ships `answer_session_ids` (gold evidence) and the harness never used them. It also ships
`haystack_dates`, `haystack_session_ids` and `question_date`, none of which were used either. The
converter dropped all four.

With gold evidence wired through — scoring hit@10 against the evidence sessions, MRR over the same:

| LongMemEval-S, lexical, N=500 | Value | 95% CI |
|---|---|---|
| **hit@10 (gold evidence)** | **97.0%** (485/500) | 95.1–98.2 |
| **MRR** | **0.907** | — |
| answer-string-in-context (the old "44.2%") | 44.2% | — |
| p50 recall | 19.5 ms | — |

Reproduce: `python benchmarks/sample.py --n 500 --json benchmarks/sample-longmem-500-lexical-gold.json`

So the substrate's recall is strong and the previous number was measuring the wrong thing. Anyone
reading the old figure would have concluded a thirteen-point capability gap existed where the real gap
is in the reading stage, not retrieval.

## 2. Where the remaining misses are

Per question type, same run:

| Category | N | hit@10 | MRR | answer-in-context |
|---|---|---|---|---|
| single-session-user | 70 | 100.0% | 0.971 | 81.4% |
| knowledge-update | 78 | 100.0% | 0.987 | 71.8% |
| single-session-assistant | 56 | 100.0% | 1.000 | 53.6% |
| multi-session | 133 | 98.5% | 0.923 | 36.8% |
| temporal-reasoning | 133 | 95.5% | 0.857 | 21.8% |
| **single-session-preference** | 30 | **76.7%** | **0.533** | 0.0% |

The 15 misses are concentrated: preferences (7), temporal (6), multi-session (2). Preference
questions are also where ranking is worst (MRR 0.533): they ask what the user *likes*, which is
phrased differently from how the preference was stated.

## 3. Two defects in our own retrieval, both measured

Sent to the same substrate with the vector index built once per question, so the comparison is paired
(`python benchmarks/diagnose_recall.py --offset 200 --n 3`):

| Config | q200 | q201 | q202 |
|---|---|---|---|
| lexical | HIT | **HIT** | miss |
| lexical + bi-encoder rerank | HIT | **miss** | miss |
| vector | HIT | miss | miss |
| hybrid (no rerank) | HIT | miss | miss |
| hybrid + bi-encoder rerank | HIT | miss | miss |
| hybrid + cross-encoder | HIT | miss | miss |

Two conclusions from that table, on three questions:

1. **The bi-encoder rerank destroys hits.** `lexical → lexical+rerank` turns q201 from a hit into a
   miss. The cause is visible in `_rerank` in `retrieval.py`: it re-sorts the fused candidates purely
   by normalised query–candidate cosine and **discards the fused rank entirely**, with min-max
   normalisation that forces a spread even when every cosine is meaningless. Reranking with the same
   bi-encoder that produced the vector scores adds noise rather than information; the cross-encoder is
   the stage that can actually help.
2. **Vector candidates dilute lexical strength.** `lexical` hits q201 while `hybrid` without any
   rerank misses it. RRF is unweighted and the candidate pool is `limit * 6 = 60`, so a candidate that
   appears mid-list in both the lexical and vector rankings can outscore the lexical rank-1 hit that
   appears in only one list.

Both are fixable without a model change.

## 4. What the benchmark's own authors found (external, cited)

LongMemEval (Wu et al., ICLR 2025, arXiv:2410.10813) reports four design findings, each measured on
the same set we are using:

- **Round granularity beats session granularity** for storing history; compressing further into
  per-fact records costs overall accuracy but helps multi-session reasoning. We already store one
  record per message, so this is where we sit — and multi-session is our second-strongest category.
- **Fact-augmented key expansion**: indexing memory values together with extracted user facts gives
  **+4% recall@k and +5% downstream accuracy**. We have an extraction module and a facts surface; we
  do not index with it.
- **Time-aware indexing + query expansion** improves temporal-reasoning recall by **7–11%**. We now
  store `effective_at` from the session timestamps, and nothing yet uses it at query time.
- **Reading is its own stage**: Chain-of-Note plus a structured JSON prompt format lifts reading
  accuracy by up to 10 absolute points across three LLMs even when recall is perfect.

## 5. Plan, in the order the evidence justifies

**P0 — measurement (done).** Gold evidence ids, session dates and `question_date` wired through the
converter; `sample.py` now reports hit@10 and MRR over evidence sessions *and* answer-in-context, so
the proxy can never again be quoted as retrieval quality.

**P1 — remove the two retrieval defects.** *(Outcome, measured 2026-11-09 on LoCoMo, N=100 across
all ten conversations: see `docs/benchmarks/results.md`.)*

- **Rerank discarding the fused rank: no measured benefit left on LoCoMo.** On that 100-question
  sample `hybrid` and `hybrid+rerank` carry identical hit flags and paired gained/lost (11/3), while
  the rerank lowers MRR (0.416 → 0.393) at about ten times the latency (3,104 ms against 323 ms).
  That is an absence of benefit on one unrepresentative sample, not proof of the band limit — the
  invariant is tested in `tests/test_rerank_fusion.py`. Default-off stands on this evidence; the
  cross-encoder path was never measured.
- **RRF crowding out single-list hits: measured as 3 losses against 11 gains on that sample.** The
  mechanism is unconfirmed — the artifact records no retrieved id lists, so nothing shows which list
  a lost record came from — and no weighted-fusion configuration was run, so the recoverable headroom
  from re-weighting is unknown, including on the questions both configurations miss. Demote it below
  the coverage work rather than closing it.
- Delete or default-off the bi-encoder rerank path; keep the cross-encoder. *(default-off done; the
  cross-encoder was not measured on LoCoMo — the 8 GB box could not hold torch beside MLX without
  thrashing.)*

**P2 — temporal retrieval (targets the 95.5% / MRR 0.857 category).** Parse relative time expressions
in the query ("a few months ago", "last week", "in March") against `question_date`, resolve to a
window, then filter or boost by `effective_at`. The paper's reported gain is 7–11% on this category;
6 misses and 0.143 of MRR headroom are available here.

**P3 — fact-augmented keys (targets preferences, 76.7% / MRR 0.533).** Index each record with an
auxiliary key text built from extracted facts/preferences, keeping record-level return granularity.
The paper's reported gain is +4% recall@k. This category has the worst ranking by a wide margin, so
it is where a modest recall gain lands on the most broken behaviour.

**P4 — chunk long records for the vector index.** Messages reach 5,085 characters; a single vector for
that much text is a poor representation and it costs ~1.8 docs/s to compute. Index ~512-character
windows and return the parent record. Also the prerequisite for making any hybrid comparison
affordable on one machine.

**P5 — the reading stage.** answer-in-context is 44.2% while hit@10 is 97.0%: the evidence is usually
retrieved and the answer is usually not readable from the top-10 as plain text. This is where an
actual reader (and the paper's Chain-of-Note/JSON findings) belongs, and it is a separate layer from
anything above.

## 6. What is still unmeasured

- ~~LoCoMo with evidence-id scoring~~ — **measured 2026-11-09 over the whole benchmark: hit@10 64.6%
  (N=1,982, CI 62.5–66.7), MRR 0.394, mean evidence coverage 0.591.** The 62.6% it replaces was a
  contiguous 200-question slice, 199 of it from one conversation. A parity claim still cannot be
  made: the Mem0/Zep/MemHQ numbers are answer-level and ours is retrieval-level, and the "93.0 floor"
  this document previously cited does not exist in `competitive-gap-closure-plan.md` or anywhere else
  in the repo — it was introduced by the benchmark commit itself and is withdrawn. The retrieval
  number is now real and whole; there is still no parity target to compare it to. See
  `docs/benchmarks/results.md`.
- **Multi-evidence coverage is the live failure and the next thing to fix.** On the 427 questions
  needing more than one record, hit@10 reads 57.6% while mean per-question coverage is 0.317 and full
  recovery 11.9% (51 of them; 181 recover nothing). Coverage here is bimodal, so "a third" is a mean
  and not a typical case. Category 1 is the sharpest at 11.0% full recovery — with the caveat that
  the category names in `results.md` are inferred from question content, not from a shipped key.
  Every retrieval improvement above should be judged on coverage and full-recovery rate rather than
  hit@10, which cannot see this. Whether a reader could answer from partial evidence, and whether the
  evidence a question needs is even reachable given how the converter builds records, are separate
  and unmeasured.
- **Abstention on LoCoMo's 446 adversarial questions.** 444 of them carry no answer field at all and
  two answer "No"; the converter hardcodes `abstain=False`, so they are graded as evidence-retrieval
  questions. They do not distort the aggregate (64.3% excluding them against 64.6%), but the right
  metric for them is whether recall declines to answer, and nothing measures that.
- **Hybrid/vector end to end at scale** — still blocked, for a measured reason: on this 8 GB machine
  the embedder costs about 11 records/s on index builds and turns a 20–27 ms query into ~300 ms
  (2,700–3,100 ms with a bi-encoder rerank). The seeded random N=120 sample is now the largest and
  only representative fusion measurement: lexical 66.7%, vector 65.0% (15 gained / 17 lost, p=0.86 —
  a wash), hybrid 73.3% (15 gained / 7 lost, p=0.134). Fusion leads by 6.6 points and nothing at this
  N establishes it; vectors alone are not what helps. A larger random sample, sized by a prospective
  power calculation, is the next measurement. Nothing currently licenses a benchmark-wide fusion
  claim.
- **Isolation** (W3.2) — untouched, and still the only place in the plan where we would be first.