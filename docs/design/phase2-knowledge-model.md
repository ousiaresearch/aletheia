# Phase 2 — Knowledge model (spec)

Status: **implemented, pending review.** Work is uncommitted. Commit and the Phase 2 adversarial review run in separate turns, per the phase gate.

Source: `~/.hermes/plans/2026-10-24_aletheia-system-full-plan.md` §4 (knowledge model) and §5 (scope engine). Inherits the Phase 1 closure in `docs/adr/0001-phase1-decisions.md`, including residuals R1–R11.

## 1. Scope of Phase 2

In:
- Record types from plan §4, with per-type create rules and lineage.
- Epistemic status gate: no direct `verified`.
- Attribution rules enforced at the write boundary.
- Content-based scope classifier (plan §5), advisory tagging with a deterministic decision.
- Versioned, forward-only migrations.
- Regression tests for each rule.

Out (later phases): ingestion, retrieval ranking, reasoning loop, correction-as-of queries, export, desktop, Hermes shadow integration. Phase 2 defines the record model those phases consume; it does not implement them.

## 2. Decisions

**D2-1. Claim and Hypothesis share one table, status-gated.** Plan §4 left this open and recommended one table. Adopted: one `records` table with `record_type` and `epistemic_status`; lineage queries stay uniform. Rejected: separate tables, because they would split lineage and duplicate the scope gate.

**D2-2. Attribution is a required field on every record.** Values: `user`, `agent`, `document`, `media-derived`, `system`, `verifier`. The write boundary rejects a record whose attribution is not permitted for its type (table below). No default.

**D2-3. The status gate is in the substrate, not the caller.** `put()` refuses `epistemic_status = verified` unless the writer is a verifier actor with a Verification record that names the verified claim. `derived` is allowed for reasoning-created Claims and Hypotheses. `stated` is the default for user- or document-attributed records.

**D2-4. Classifier: advisory tag, deterministic decision, fail-closed.** The classifier proposes a `scope_tag`. The decision uses the tag and the scope engine (Phase 1), never the classifier's opinion alone. Untagged content that matches a scope-bound category is refused (fail closed). The classifier does not run on read.

**D2-5. Residual R1 is closed in Phase 2.** The content rule and the classifier scan every string-bearing field a reader sees: `payload`, `provenance`, `source_ids`, and `record_type` values. Phase 1 scanned only `payload`. Regression test required (see §6).

**D2-6. Per-field allowlist is user-curated.** Global eligibility is never derived automatically from Person or Project records. The allowlist is configuration, set at engine construction (Phase 1 N9).

**D2-7. Cross-scope is user-attributed only.** `ScopeContext.user_authorized_cross_scope` is set only from a user-authored turn or UI action. Attribution ambiguity denies.

**D2-8. Classifier is rules-only in Phase 2 (decided).** The classifier uses the scope markers already defined in configuration, with the Phase 1 normaliser. No model-based classifier in Phase 2. Reason: rules are deterministic and reviewable, and the Phase 1 gate already rests on them. Known limit: paraphrased references to a project pass unless they contain a marker. A model-based classifier is a Phase 4 option, judged against the same §6 tests before adoption.

**D2-9. Verifiers are user-created scope-holding actors (decided).** Only a verifier can promote a Claim to `verified`, and only with a Verification record naming that Claim. The user creates verifiers through an explicit action. The agent cannot create, modify, or grant a verifier. Reason: otherwise the agent could mint its own verification, which defeats the status gate.

## 3. Record types and creation rules

| Type | Created by | Attribution allowed | Status allowed | Lineage |
|---|---|---|---|---|
| Person | user or import | user, document | stated | none |
| Agent | system registration | system | stated | none; self-statements attributed to agent |
| Project | user only | user | stated | scope boundary; explicit membership list |
| Source | ingestion | document, media-derived, user | stated | immutable |
| Conversation | ingestion | system | stated | project, participants, time range |
| Observation | extraction | document, media-derived | stated | must cite a Source; machine-derived marked |
| Claim | reasoning or verification | agent (derived), verifier (verified) | derived, verified | may supersede a Claim via Correction |
| Hypothesis | reasoning | agent | derived | explicitly not a conclusion |
| Decision | user only | user | stated | requires explicit confirmation record |
| Commitment | user only | user | stated | never inferred from assistant text |
| Correction | user, or reasoning-proposed and user-accepted | user | stated | original preserved; supersedes link |
| Verification | verifier actor | verifier | stated | names the Claim it checks |
| Deletion | user | user | stated | cascades; emits receipt |
| Summary | derived | system | derived | projection, never authoritative, never injected whole |
| RecallReceipt | system | system | stated | no secrets |

Enforcement: `put()` rejects any record whose `(record_type, attribution, epistemic_status)` is not in this table. Assistant text can never produce a Decision or Commitment: attribution `agent` is refused for those types.

## 4. Migrations

- Schema version stored in the database. Migrations are numbered, forward-only, and idempotent.
- Migration 2 adds the Phase 2 columns (`attribution`, `schema_version`, `effective_at` if absent) and backfills existing Phase 1 rows with `attribution = system` and a recorded backfill journal entry.
- A failed migration leaves the database at the prior version (one transaction per migration).
- Tests: migrate from a Phase 1 fixture; re-run is a no-op; failure mid-migration is rolled back.

## 5. Classifier

- Input: a record's string fields (D2-5 set). Output: `scope_tag` (a scope name, or `none`) and `confidence` (advisory only).
- Decision: a record with a non-`none` tag is written to that tag's scope if the writer holds it; otherwise refused.
- Untagged record matching a scope-bound category (per the scope's markers): refused.
- Classifier errors and timeouts: fail closed (refuse the write), with a generic denial.

## 6. Tests (must exist and pass before Phase 2 review)

Status gate
- Direct `put(epistemic_status="verified")` by a non-verifier is refused.
- Verifier with a matching Verification record may create a verified Claim.
- Verification naming a different Claim does not promote.

Attribution
- Agent-attributed Decision refused.
- Agent-attributed Commitment refused.
- Agent-attributed Claim allowed only as `derived`.
- Document-attributed Observation without a Source reference refused.

Lineage
- Correction preserves the original; supersedes link set; original still readable in its own scope.
- Correction cannot change scope (Phase 1 N10 still holds).
- Deletion cascade reaches Summary projections derived from the deleted record.

Classifier (fail closed)
- Untagged ACS-category content refused for a memory-providers writer.
- Classifier exception refuses the write, with a generic denial.
- Classifier output cannot grant a scope the writer does not hold.

R1 closure
- ACS marker in `provenance` refused under memory-providers.
- ACS marker in `source_ids` refused under memory-providers.
- ACS marker in `record_type` refused under memory-providers.

Plan §5 regression targets
- Memory-provider query returns zero ACS-project records.
- Positive same-project recall returns the relevant records.
- Unknown scope denied.
- Agent-minted cross-scope request refused.
- Cross-scope read does not authorize delete.
- Identity-sourced ACS fact excluded from unrelated queries.

Migration
- Phase 1 fixture migrates; second run is a no-op; mid-migration failure rolls back.

## 7. Exit criteria

- All §6 tests pass; no test is vacuous (each asserts behaviour that would fail on the Phase 1 code, where the rule is new).
- Fresh independent adversarial review (Codex, per standing rule) returns APPROVE or APPROVE_WITH_RESIDUALS with no gate-class findings.
- Residuals recorded in the ADR.

## 8. Open items

None blocking Phase 2 implementation.

- OCR in alpha: resolved (accepted by the user, Oct 25). OCR output enters through the Phase 3 ingestion path as machine-derived Observations.
- Classifier: resolved as D2-8.
- Verifier registration: resolved as D2-9.

## 9. Implementation notes (as built, for the reviewer)

- **Rules table:** `aletheia/knowledge.py` (`RULES`, `check_record`). Runs inside the write transaction, after lineage is set. Refusals are generic (G4/G10).
- **Attribution:** `Record.attribution`, required. Column added by migration 2 (`PRAGMA user_version`), with legacy rows backfilled to `system`. Migration is transactional; a failure rolls back (tested with a SQL trigger).
- **User-authored:** `ScopeContext.user_authored` (default False). Required for attribution `user` and for any cross-scope request. The library trusts this flag from the context it is given. It refuses the request when the flag is absent, but it cannot verify who built the context. The agent-minted cross-scope path is closed only at the Phase 6 service boundary (review residual R-1).
- **Verifiers:** table `verifiers`, populated only through `register_verifier(actor, ctx)`, which requires `user_authored`. A verifier must be registered before it can write `verifier`-attributed records.
- **Verified status:** a Claim may be `verified` only with attribution `verifier`, a registered actor, and a Verification record in the same scope that names it (`payload.claim_id`) and is cited in `source_ids`.
- **Correction:** `Correction` records are written only through `correct()`. A `correct()` replacement must be a Correction.
- **R1 closure:** the content rule and classifier inspect payload, provenance, source_ids, and record_type.
- **Classifier fail-closed:** an exception in the content check refuses the write or read generically.
- **Summary cascade:** deleting a record also removes derived Summary records that cite it in `source_ids`. Refused if the caller cannot delete any of them.
- **Known gap, deliberately deferred:** a document Observation's `source_ids` must be non-empty, but the referenced Source is not checked for existence. Phase 3 ingestion creates Sources, and the existence check belongs there.
- **Phase 1 test changes (documented, not weakened):** test helpers now supply attribution and source; cross-scope test contexts are user-authored; `correct()` replacements are Corrections written from a user-authored context.

## 10. Review record

- **Round 1 (f942747):** APPROVE_WITH_RESIDUALS. All nine invariants hold. No gate-class findings.
- **Residuals carried:** R-1 (`user_authored` is trusted from the context; service boundary enforces, Phase 6), R-3 (a registered verifier given a user-authored context can write user records; same trust as R-1), R-4 (delete scans every Summary row, no leak, bare False), R-6 (source existence deferred to Phase 3).
- **Fixed in round 2:** R-2, a Verification that a verified Claim relies on cannot be deleted (generic refusal; the Claim must be removed first). R-5, a database recorded as version 2 without the attribution column is refused at open with a typed error.
- **Test gaps closed:** knowledge refusals leave no row in the target scope; unsafe verifier ids refused; read-side classifier error denies.
- **Round 2 (4c3f062):** BLOCK. The reliance guard covered delete only, so overwrite of a relied-on Verification re-pointed it (gate). Fixed: overwrite of a relied-on Verification is refused. Also: corrupt stored lineage now fails closed (`SubstrateError`), and a failed open closes its connection. Residuals kept: the read path does not re-check the status gate for rows written outside the API (in-process, R2); a database with version 2 and the attribution column but other missing columns is accepted (no other v2 columns exist).
- **Round 3 (aaec341):** BLOCK. Overwrite could demote the verified Claim itself (the guard only covered the Verification). Fixed: a verified record cannot be overwritten at all; it can only be removed, and deletion is still guarded by the reliance rule. Also fixed: a classifier error now also refuses delete (D2-4); the projection view token includes `user_authored`.
- **Round 4 (a8ee2d7):** APPROVE_WITH_RESIDUALS. All nine invariants hold; no gate-class findings. Residuals accepted: RESIDUAL-1 (overwrite authorises the incumbent by scope, not by readability; a holder of a scope may overwrite a row the content rule withholds from it; this is write authority, not disclosure); RESIDUAL-2 (`_relied_on` is a full scan; acceptable at measured scale, index later); RESIDUAL-3 (version-2 check verifies only the attribution column); RESIDUAL-4 (`meta.schema_version` is written but never read).
- **Test gaps for follow-up:** a verified record is still deletable after the overwrite guard; correct() leaves a verified row unchanged; RESIDUAL-1 behaviour.
