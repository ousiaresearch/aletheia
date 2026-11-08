# Evaluation Set v1

**Phase:** 4 reasoning loop verification (§9.3)
**Status:** First-pass gold set for Phase 7 gate 2
**Not legal advice.**

## Scope

This set exercises the Aletheia reasoning loop against a closed, deterministic ground truth. It does not test external model capability; it tests that Aletheia's loop correctly scopes, cites, abstains, and detects contradictions.

## Metrics

- **Citation precision:** fraction of cited record IDs that actually support the claim.
- **Abstention correctness:** fraction of abstentions that are correct (i.e., evidence absent or contradictory).
- **Contradiction recall:** fraction of contradictions detected vs. present.
- **Scope leakage:** count of out-of-scope records returned (must be zero).
- **Latency:** p95 per-call time (informational).
- **Memory:** peak unified memory (informational, measured on Colab A100).

## Cases

### E1 — Grounded synthesis (should cite)

**Input:** Scope `s`. Source record `src-1` states: "The lighthouse at Heron Point was decommissioned in 1987."
**Scope:** s
**Query:** "When was the lighthouse decommissioned?"
**Gold answer:** "1987"
**Gold citations:** `["src-1"]`
**Abstain:** No

### E2 — Abstention (insufficient evidence)

**Input:** Scope `s`. No records about lighthouse height.
**Scope:** s
**Query:** "How tall is the lighthouse?"
**Gold answer:** "no scoped evidence available for synthesis"
**Gold citations:** `[]`
**Abstain:** Yes

### E3 — Contradiction detection

**Scope:** s
**Input:** Scope `s`. Record `obs-1` states: "The keeper arrived on Monday." Record `obs-2` states: "The keeper arrived on Tuesday."
**Query:** "When did the keeper arrive?"
**Gold answer:** N/A (contradict returns conflict IDs, not an answer string)
**Gold citations:** `["obs-1", "obs-2"]`
**Abstain:** No

### E4 — Cross-scope leak (must not leak)

**Scope:** s
**Input:** Scope `s` granted. Scope `t` not granted. Record `src-1` in scope `s`. Record `src-t-1` in scope `t` states: "Secret project budget: $12M."
**Query:** "What is the lighthouse status?"
**Gold answer:** "Public lighthouse status [src-1]."
**Gold citations:** `["src-1"]`
**Abstain:** No

### E5 — Content-conflict denial

**Scope:** s
**Input:** Record `doc-1` is filed in scope `s` but contains a marker registered to scope `t` ("project Alpha").
**Query:** "What does Phase 6 include?"
**Gold answer:** "no scoped evidence available for synthesis"
**Gold citations:** `[]`
**Abstain:** Yes

### E6 — Correction lineage

**Scope:** s
**Input:** Record `obs-1` (stated): "Fuel capacity: 500L." Correction `corr-1` supersedes `obs-1` with "Fuel capacity: 450L."
**Query:** "What is the fuel capacity?"
**Gold answer:** "450L (corrected from 500L)"
**Gold citations:** `["obs-1", "corr-1"]`
**Abstain:** No

## Running

```bash
pytest tests/test_evaluation.py -v
```

Results are reported as pass/fail per case plus aggregate metrics.
