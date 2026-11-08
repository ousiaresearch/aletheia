# Aletheia Architecture

**Phase:** 0 — Capability study (deliverable)
**Status:** Draft, informed by the capability study. Subject to adversarial review.
**Constraint:** No AGPL or Sustainable-Use-License component may be copied, linked, or bundled. Everything below is to be independently implemented.

## Principle

> Rules decide what a model may consult; reasoning decides what matters within that boundary.

Deterministic scope and permission checks run **before** any relevance ranking or model call. A
model may discover useful connections; it may never grant itself access to out-of-scope material.

## Layers

```
┌─────────────────────────────────────────────────────────────┐
│ Electron desktop          Hermes adapter       (Phase 6)    │
│ inspect / correct / test-recall / configure    shadow mode  │
├─────────────────────────────────────────────────────────────┤
│ Reasoning loop  (Phase 4)                                   │
│ extract → retrieve → hypothesize → contradiction-check →    │
│ conclude (with citations) → synthesize (or abstain)         │
├─────────────────────────────────────────────────────────────┤
│ Retrieval  (Phase 3)                                        │
│ scope resolve → candidate scan → rank → tiered load         │
│         (L0 abstract → L1 overview → L2 source)             │
├─────────────────────────────────────────────────────────────┤
│ Ingestion  (Phase 3)                                        │
│ conversations · documents · media(transcribe/describe)      │
│ idempotent · durable jobs · recall receipts                 │
│ CONTEXT FENCING: recalled/injected blocks stripped or       │
│ tagged non-source before persistence                        │
├─────────────────────────────────────────────────────────────┤
│ Knowledge model  (Phase 2)                                  │
│ Person · Agent · Project · Source · Observation · Claim ·   │
│ Hypothesis · Decision · Commitment · Correction             │
├─────────────────────────────────────────────────────────────┤
│ Substrate  (Phase 1)        Scope engine  (Phase 2)         │
│ typed structured files     deterministic, fail-closed,      │
│ validated on write         runs before ranking/models       │
└─────────────────────────────────────────────────────────────┘
```

## Knowledge model

Records are distinct types, not one undifferentiated store. Every derived record carries:
source IDs, scope, epistemic status (stated / derived / verified), recorded time, effective time
where known, and supersession links.

| Record | Meaning | Authority |
|---|---|---|
| **Person** | A human. Model evolves from what they actually said. | Their statements; others' inferences are separate records |
| **Agent** | An assistant. Self-statements are attributed, never promoted to user commitments. | Its own statements |
| **Project** | A scope boundary. Knowledge does not cross it implicitly. | Explicit membership only |
| **Source** | Raw material: a message, document, or media file. Immutable. | Canonical evidence |
| **Observation** | Something extracted from a source, with location and confidence. | Derived, cites source |
| **Claim** | An assertion held to be true, with epistemic status. | Derived or verified |
| **Hypothesis** | A tentative connection under evaluation. | Explicitly not a conclusion |
| **Decision** | A chosen course, with effective time. | User-attributed; requires explicit confirmation |
| **Commitment** | A promise or obligation. | User-attributed only; never inferred from assistant text |
| **Correction** | A revision. Preserves original + new + lineage. | Supersedes; never erases (see Deletion for the erasure case) |
| **Deletion** | A privacy removal request. Cascades to all derived records. | Overrides archival preservation; retains only a non-sensitive tombstone where policy permits |
| **Verification** | An explicit act of checking a claim against its source. | The **only** path from stated/derived → verified |
| **Conversation** | A session container: participants, project membership, time range. | First-class scope axis; join point for Observation location and Correction lineage |
| **Summary** | A rebuildable projection over other records (e.g. a session or project overview). | **Projection, not authority.** Never an injectable unit; assembled at recall time from in-scope facts only. Deletable in cascade. |
| **RecallReceipt** | What a recall did: scope consulted, sources read, records selected, exclusions and why. | Audit trail; no secrets |

**Invariant:** retrieval can never upgrade an inference into a fact. Graph links and type
membership are not evidence.

**Enforced invariants (write-boundary rules, not aspirations):**

1. **Epistemic status transitions.** Status `verified` requires a `Verification` record citing the
   verifying actor, method, and timestamp. Retrieval paths are read-only with respect to status —
   no read operation may change it. The only permitted transition to `verified` is
   `stated|derived → verified` via a `Verification` record. The reasoning loop's step 5 writes a
   *proposed* claim at status `derived`; it cannot write `verified`.

2. **Correction vs. Deletion are distinct.** `Correction` supersedes and retains lineage (archival).
   `Deletion` removes and cascades (privacy). Privacy deletion **overrides** archival preservation.
   Where policy requires both — a corrected fact that must also be erased — the record becomes a
   tombstone: lineage structure retained, content removed, and the tombstone carries no sensitive
   residue. Per-record policy decides which applies; it is never silently chosen.

3. **User attribution.** `Decision` and `Commitment` records require explicit user attribution.
   Assistant text may *propose* a commitment; it may never create one. An assistant-authored turn
   proposing a decision creates a `Hypothesis` or a `proposed Decision`, never a `Decision`.

## Scope engine (runs first)

1. Resolve the query's permitted scopes from identity + project membership + explicit request.
2. Unknown or unstated scope → **deny by default**. No silent widening.
3. Candidate records outside permitted scopes are **excluded before ranking**, not merely ranked low.
4. A **user-curated, per-field-gated allowlist** may be granted globally. Every always-eligible
   fact is explicitly approved and scope-tagged by the user. It is not derived automatically from
   identity records.
5. **Default rule: any fact referencing a project, organization, or body of work is scope-bound**,
   regardless of where it is stored. A fact's location does not determine its scope; its content does.
   The live Honcho peer card demonstrates why: it contains `ATTRIBUTE: Owner of the ACS
   Constitutional Convention Letters` — ACS material resident in the identity layer. Under a
   naive "identity is always eligible" rule that fact would leak into every query, making the
   regression target below unachievable for the most identity-adjacent formulation.

   **Derived artifacts are not injectable units.** A peer card, session summary, or representation is
   a *composite derived artifact*, never a source of recall in itself. It is assembled **at recall
   time** from individually scope-tagged facts that survive the scope filter. Consequences:
   - Derived artifacts are stored decomposed into per-fact records, each with its own scope tag. A
     composite block is never stored, cached, or injected as a whole.
   - A composite artifact is therefore never "mostly in scope" — an out-of-scope fact inside it is
     simply absent from the assembled result, with the exclusion recorded in the RecallReceipt.
   - This closes the live leak path: the ACS ATTRIBUTE line inside the peer card cannot be injected by
     assembling "the peer card", because no such injectable object exists. Only in-scope facts are
     assembled, and that fact is scope-bound by rule 5.
   - Rebuildable projections (summaries, indexes) may be cached for performance, but they are
     **projections, not authority**, and they are never the thing a recall injects. A stale or
     contaminated projection is detectable and discardable.

7. Scope-engine rule 1 is read as: identity contributes *candidate facts*, not a scope grant. Identity
   never confers eligibility by itself; every identity-sourced fact still passes rules 4 and 5.

8. Cross-project recall requires an explicit **user-attributable** request in that query — a
   user-authored turn or a UI action, never agent-asserted. The agent cannot mint its own
   cross-scope authorization, because in the Hermes integration the query text is produced partly by
   the agent. Attribution is ambiguous → fail closed. Attribution is recorded in the RecallReceipt.
7. Every exclusion is recorded in the RecallReceipt with its reason.

**Regression target:** a query about memory providers returns zero ACS history. This is a
deterministic scope decision, not a relevance-model outcome — it must hold even with no model available.

## Retrieval and tiered loading

Candidate scan (scoped) → rank → load tiers progressively:
- **L0** (~100 tokens): abstract — enough to judge relevance.
- **L1** (~2k tokens): overview.
- **L2**: full source, opened only when needed.

The agent and the desktop UI both read the summary before opening the source. RecallReceipt records
which tier satisfied the query.

## Reasoning loop

1. **Extract** observations from source, with location and confidence.
2. **Retrieve** relevant evidence within permitted scope.
3. **Hypothesize** connections; mark explicitly tentative.
4. **Contradiction-check** against existing claims; surface conflicts, never merge silently.
5. **Conclude** as a proposed claim, citing source IDs; persist model + config version.
6. **Synthesize** an answer, or **abstain** when evidence is absent or contradictory.

External models are opt-in per query with a visible data-egress policy; local and external models
share one interface. Reasoning explanations expose evidence and decisions, not hidden chain-of-thought.

## Context fencing (ingestion-side defence)

Recalled or injected context must never be re-ingested as new source. This is not hypothetical — the
reference implementation exhibits exactly this failure: `saveMessages: true` persists the injected
`<memory-context>` block back into storage, where it reappears through history and re-contaminates
subsequent turns. That mechanism is what carried ACS material into the peer card.

Rules:
1. Any recalled/injected block is **stripped or tagged non-source** before persistence.
2. Ingestion is idempotent on `(source_id, content_hash)` of **user-authored material only**.
   Re-captured recall does not create a new source.
3. A `Source` record carries a provenance marker distinguishing user-authored, assistant-authored,
   document, and media-derived material. Only the first is eligible to become a `Source` of record
   for observations about what the user said.

## Deletion propagation

`Deletion` is a first-class operation, not an absence of one. On request:

1. Cascade to **all** derived records — Observations, Claims, Hypotheses, and any Summary built from
   the source.
2. Purge from rebuildable indexes and any model-configured store.
3. Purge from exports and backups; backups carry an explicit deletion/expiry policy rather than
   retaining deleted content indefinitely. **Phasing note:** the export/backup purge obligation is
   *recorded* by the Phase 3 deletion receipt and *implemented* when the export path exists in
   Phase 5. Phase 3 does not attempt to purge artifacts that do not yet exist; the receipt already
   carries the obligation so it cannot be forgotten later.
4. Emit an auditable **deletion receipt** (what was removed, when, by whose request) containing no
   sensitive residue.
5. Where policy requires lineage retention alongside erasure, emit a **tombstone**: structure kept,
   content removed.

## Media ingestion

Audio → transcription, images → description, via the configured model layer. Derived records are
marked machine-derived with model, version, and confidence, and are never presented as verbatim
quotation. Original file stays canonical for evidence linking. Unsupported formats and failed
extractions are rejected or surfaced, never recorded as silent successes. Transient artifacts follow
derived-text retention and deletion policy.

## Governance

- Typed validation is **advisory at the engine** (Arsumbris lesson: diagnostics never block a save).
  Aletheia adds **authoritative validation at the memory write/recall boundary** — scope-invalid writes
  are rejected regardless of engine behavior.
- Mediation must **fail closed** in Aletheia (Arsumbris's mediation fails open when the daemon is
  unreachable). An unreachable policy engine must block the write, not allow it.
- No sandboxing exists in the reference systems; Aletheia treats all retrieved content as untrusted data
  that cannot authorize writes, alter scope, or act as instructions.

## Durability

One authority per record. The substrate holds the canonical typed knowledge; a journal records
accepted operations for crash recovery. **Journal vs. file commit ordering is unresolved** — Phase 1
must resolve it via fault injection before any durability claim is made. Indexes and summaries are
rebuildable derived projections; a stale index is detectable, not silently trusted.

## Adapter contract (Hermes, Phase 6)

The adapter is a **consumer of the stack, bound by the scope engine** — not a bypass around it:

1. All recall enters through the scope engine. There is no path to the retrieval layer that skips it.
2. The adapter cannot request unscoped recall; ambiguous-scope calls fail closed.
3. The adapter cannot mint cross-scope requests on the user's behalf. Cross-scope requires
   user-attributable authorization (see scope engine rule 6).
4. Shadow mode: the adapter observes and records what it *would* have injected, without that context
   controlling live behavior, until the cutover gate passes.

## Phasing

See "Staged execution" in the plan: Phase 1 substrate + scope spike, Phase 2 knowledge model
(**includes Correction lineage and the epistemic-status/Verification rule**), Phase 3 ingestion +
retrieval (**includes context fencing and deletion propagation**), Phase 4 reasoning, Phase 5
correction semantics + privacy + portability, Phase 6 Hermes shadow integration, Phase 7 gated cutover.

**Phasing correction (after adversarial review):** Correction lineage and the status-transition rule
were previously deferred to Phase 5 while the capability matrix marked them Alpha — a contradiction
that would have shipped an alpha without them. Corrected: the *mechanism* (Correction record,
Verification record, status gate) is built in Phase 2 as part of the knowledge model, because the
retrieval layer cannot be tested meaningfully without it. Phase 5 then covers correction *semantics*
(effective-time queries, supersession behaviour, summary invalidation) and the privacy/portability
work. Deletion propagation moved to Phase 3 alongside the ingestion path it must purge.

## Open questions for Phase 1

1. Storage engine: SQLite + structured files, or files-only with a derived index? (Favouring SQLite for transactional integrity, files for human inspectability — needs a decision with evidence.)
2. Journal/file/index commit ordering under crash.
3. Schema language: reuse a standard (JSON Schema) or define a minimal typed language of our own?
4. Language/runtime: Rust (substrate performance) vs TypeScript (shared types with Electron UI) — trade-off between a process boundary and implementation speed.
