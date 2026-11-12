# Benchmarks

One entrypoint: `benchmarks/run.py`. It is the only runner whose numbers are published.

```bash
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset locomo
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset longmem --n 100
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset beam --splits 100K --n 40
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset all --sample random --seed 1 --n 120
```

Add `--json out.json` to write machine-readable results. `--mode hybrid` loads the local bge-m3
embedder and is one to two orders of magnitude slower on this hardware.

## What it measures

`hit@k` is scored against each dataset's gold evidence ids and is retrieval quality. Where a dataset
ships no evidence ids, the runner says so instead of substituting a proxy. `answer_in_context` is that
proxy — whether the gold answer string appears in the retrieved context — and is always labelled as
one. Every rate carries its N and a 95% Wilson interval.

Gold units differ by dataset and the runner will not pretend otherwise:

| Dataset | Gold unit | hit@k | coverage | abstention |
|---|---|---|---|---|
| LoCoMo | record | yes | yes | yes |
| LongMemEval-S | session (hundreds of records) | yes | no | yes |
| BEAM | none shipped | no | no | yes |

## Layout

| Path | Purpose |
|---|---|
| `run.py` | the suite entrypoint — use this |
| `diagnose_locomo.py` | paired per-question comparison of recall configurations on LoCoMo |
| `diagnose_recall.py` | the same on LongMemEval; answer-string scored, so not for numbers |
| `sample.py` | single-dataset runner with gold-id scoring; superseded by `run.py` |
| `smoke.py` | 22 hand-built gold cases — a sanity check that saturates, not a benchmark |
| `full/` | dataset adapters (`locomo.py`, `longmemeval.py`, `beam.py`) and older runners |
| `datasets/` | the small hand-built gold sets used by `smoke.py` |

## Do not quote these

`full/run_all.py` and the per-dataset `run()` functions score LongMemEval-S and BEAM **without**
passing their gold ids, so both fall back to the answer-string proxy. That is how a 44.2% proxy figure
once circulated as retrieval quality when the real number was 97.0%. They are kept for reference;
their output is not a result.

## Rules this harness learned the hard way

- **A rate without its N and interval is a rumour.** Three disjoint 50-question slices of LongMemEval-S
  scored 88%, 34% and 62%.
- **A prefix is not a sample.** The first 200 LoCoMo questions are all one conversation; the first ten
  of each conversation are category-skewed (41/43/14/2/0 against 282/321/96/841/446). Use
  `--sample random --seed` for anything you intend to quote.
- **Failures must not look like misses.** `aletheia.retrieval.recall` swallows non-ValueError from its
  candidate queries and returns an empty list, so a dead index reads as bad retrieval. `run.py` reports
  errors separately from hits, and `diagnose_locomo.py` preflights the retrieval path per conversation.
- **Check that the stage you meant to measure actually ran.** A configuration that quietly skips its
  embedder, or a rerank that silently falls back, produces a plausible number for something else.

Reproduction commands and expected output: [`docs/benchmarks/REPRODUCE.md`](../docs/benchmarks/REPRODUCE.md).
Results and methodology: [`docs/benchmarks/results.md`](../docs/benchmarks/results.md).