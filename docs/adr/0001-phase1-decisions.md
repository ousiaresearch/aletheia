# Phase 1 Decisions

**Phase:** 1 — substrate and scope spike
**Status:** Decided; implementation follows.
**Constraint inherited from Phase 0:** independently written, permissively licensed, no bundled
AGPL/ELv2/non-commercial component.

## Decisions

### 1. Storage: SQLite as transactional store + human-readable projection files

**Decision:** SQLite is the authoritative store. Structured files are a *projection* of it, written
for human inspection and diff-ability, never read as authority.

**Rationale:** The core requirement is a fail-closed write path with a durable journal — that needs
transactions, which SQLite provides and plain files do not. Human inspectability matters, but it is
satisfiable by projection, not by making files authoritative. Making files authoritative would
reintroduce the journal/file ordering ambiguity the plan flagged as unresolved.

**Consequence to test in Phase 1:** projection divergence (a file edited by hand) must be *detectable*
and must never silently override the store. Hand-edited projection files are ignored, with a warning.

### 2. Language/runtime: Python for the substrate, TypeScript only for the Electron UI

**Decision:** Python 3.11+ for substrate, scope engine, retrieval, and service. TypeScript/Electron
for the desktop UI, talking to the service over HTTP.

**Rationale:** The Electron UI and the substrate are separated by a service boundary anyway (the
desktop is a *client*, per the architecture). So the "shared types" argument for TypeScript is weak —
types are shared over an API contract, not a language. Python gives faster iteration on the
substrate and reasoning loop, which is where the real work is. TypeScript's advantage would be
in-process type sharing, which this architecture deliberately avoids.

**Constraint noted:** the host's default `python3` is 3.9.6, which lacks `hashlib.scrypt`. The
substrate requires 3.11+ (recorded from prior work in this environment). Phase 1 must pin and verify
this explicitly rather than inherit the ambient interpreter.

### 3. Schema language: JSON Schema (Draft 2020-12), not a custom typed language

**Decision:** JSON Schema for record validation.

**Rationale:** A custom typed language (the Arsumbris approach) is genuinely attractive for
expressiveness, but it is a large surface to build and specify before any memory behavior exists, and
it is *advisory* in the reference implementation anyway. JSON Schema is standard, tooled, testable,
and sufficient for the record shapes we need. The scope and authority rules — which is where the real
correctness lives — are enforced in Python at the write boundary, **not** delegated to the schema
language. Schema validation is advisory; scope enforcement is authoritative.

**Revisit trigger:** if Phase 2 shows record relationships (reference integrity, inheritance, sealed
variants) that JSON Schema expresses poorly.

### 4. Journal/file/index commit ordering: single SQLite transaction, WAL, projections after commit

**Decision:** One SQLite transaction covers (a) the record write, (b) the journal entry, and
(c) index updates. Projection files and any model-configured store are updated **after** the
transaction commits, asynchronously, and are idempotent and rebuildable.

**Rationale:** This eliminates the ordering question rather than solving it — there is one commit
point, and everything rebuildable is derived. Crash between commit and projection leaves a stale
projection, which is detectable and harmless, not a lost write. This is the opposite failure mode
from losing an accepted write, and it is the correct one.

**To verify by fault injection in Phase 1:**
- Kill mid-transaction → no partial record, no orphan journal entry.
- Kill after commit, before projection → record present, projection stale and flagged.
- ~~Rebuild-from-journal reproduces the identical record set.~~ Withdrawn: the journal is an audit trail, not a rebuild source (see the addendum above).
- Concurrent writers → no lost update, no interleaved corruption.

## Phase 1 deliverables

1. `aletheia/substrate/` — record store, journal, schema validation, projection writer.
2. `aletheia/scope/` — deterministic scope engine (deny-by-default, content-based scoping).
3. `tests/` — including the fault-injection suite above.
4. `docs/adr/0001-phase1-decisions.md` — this record.

## What Phase 1 does NOT include

Retrieval ranking, reasoning loop, model integration, ingestion pipeline, or the Electron UI. Those
are Phases 3–4. Phase 1 proves the substrate holds, scope gates correctly, and the system survives
being killed.

## Audit gate

Phase 1 completes only after an independent adversarial audit of the substrate and scope engine,
including an attempt to defeat the scope gate. The regression target from Phase 0 is the acceptance
criterion: *a query about memory providers returns zero ACS history.*

## Addendum (Phase 1 fixes after adversarial review of a37d175)

Codex review of a37d175 returned BLOCK (21 findings). The fixes below change decisions above; each is covered by tests in `tests/test_regressions.py` and `tests/test_substrate.py`.

- **Authority.** Writes, overwrites, deletes, and corrections require a granted scope on the caller's `ScopeContext`. Knowing that a scope exists is not authority. The global allowlist and cross-scope reads never confer write or delete authority.
- **Journal is an audit trail, not a rebuild source.** The earlier claim that rebuild-from-journal reproduces the record set is withdrawn. The journal holds operations and reasons, never payloads, so deletion cannot leave content behind in it. SQLite remains the only authority. Journal reads are filtered by the caller's scope.
- **Content rule replaces the no-op guard.** Scopes may declare markers. Content naming another scope's marker is refused on write and withheld on read. Regex project detection stays advisory.
- **Overwrite is explicit.** `put` raises `RecordConflict` for an existing id unless `overwrite=True`, and an overwrite needs a granted scope for the incumbent.
- **Projections.** Gated by context, pruned of files no longer in scope, written atomically, and validated by a manifest with per-file hashes and a store version. `projection_status` reports `current`, `stale`, `tampered`, or `missing`.
- **Record ids** are restricted to `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` on every write, regardless of schema validation, because they are file names.
- **Known limits, unchanged:** the library trusts the code that constructs a `ScopeContext`. Attribution of user-authorized cross-scope requests is enforced at the service boundary (Phase 6). Global allowlist and scope registration are configuration, not authenticated.

### Accepted residuals (recorded before round 4 review)

1. **Content markers are best-effort, not a security boundary.** They are defence in depth against mis-filed content. The security guarantee rests on granted scopes. Lookalike coverage is a short explicit table (Cyrillic, Greek, and some Latin variants), not full Unicode confusables. Rounds 1-3 found evasions in this category; they are documented, and new ones in this category are residuals unless they defeat the granted-scope gate.
2. **In-process trust.** Code that holds a `Substrate` can reach its raw SQLite connection (`_conn`). The library enforces scope for its own API only. The service boundary (Phase 6) must not expose the connection or the database file to callers.
3. **Plaintext at rest.** The database holds all scopes unencrypted. It is created owner-only (0600, umask 077, and sidecars chmod'ed). Encryption at rest is a later phase.
4. **Global allowlist is configuration.** It is set at engine construction and exposed read-only. An attacker who can construct or mutate the engine object already controls the process.
5. **Same-scope overwrite is last-writer-wins.** `put(..., overwrite=True)` by a caller with a granted scope replaces the record. Cross-process races without overwrite produce exactly one winner per id (tested).

### Stopping rule for the review loop

Round 4 is the last open review round for Phase 1. If its BLOCK findings are limited to residuals 1 and 2 above, or to new heuristic-coverage gaps that do not defeat the granted-scope gate, they are accepted and recorded here, and Phase 2 begins. Any finding that defeats the granted-scope gate, or that discloses content or ids to a caller without the scope, blocks Phase 1 and is fixed first.

### Round-4 changes (GATE findings G1-G8)

- **One projection directory per view.** A projection is owned by the view (the set of granted scopes) that wrote it. Another view cannot write into it. Writing over unmanaged files with projected names is refused. Use separate directories for separate views.
- **Pruning is narrow.** A projection removes only files named in its own view's manifest. Files it never named are left alone (G2). A record that leaves the view, for example by deletion, is pruned on the next write.
- **Status reports only the caller's view.** It names no record outside that view (G1). It distinguishes `tampered` (a file's bytes differ from what the projection wrote) from `stale` (the view changed in the store since the projection was written) (G8). It no longer uses a global store version.
- **Unsafe ids are skipped on projection.** Ids that are not safe file names are never written to disk (G3).
- **Generic denials.** A refused write, overwrite, or correction gets one generic message, so a caller cannot tell an id in another scope from a non-existent one (G4, G5). Reasons go to the journal, which only holders of the record's scope can read.
- **Query returns readable records and no exclusions.** Exclusion reasons and counts are no longer returned, so a caller learns nothing about records it cannot read (G6, G7).
- **Known residuals, unchanged:** the content-marker table is best-effort; the raw connection is reachable in-process; the database is plaintext at rest; same-scope overwrites are last-writer-wins.

### Round-5 changes (GATE findings G9-G13)

- **Denials are never written into a scope's journal (G10).** A caller that lacks a scope is by definition not a holder of it, so it must not add rows there. Denial rows (write, overwrite, correction, delete, read) are written with no scope, no record id, and a generic detail. Consequence: holders of a scope no longer see denial entries for that scope in `journal_entries`. Denials remain in the raw audit table for operators.
- **Correction is not an existence oracle (G9).** A missing original and an unreadable original raise the same exception with the same message.
- **Supersession is set only by `correct()` (G13).** `put()` refuses a record that carries `supersedes`, so no caller can link a record into another scope's lineage.
- **Journal sequence is renumbered per view (G12).** `journal_entries` numbers the entries a caller may read 1..n, so global gaps are not observable.
- **Projection view includes all cross-scope authority (G11).** The view token covers granted scopes, cross-scope authorization, targets, and authorization source. Two callers with the same granted set but different cross-scope authority do not share a projection.
- **Projection directory is owner-only (F-E residual R3).** `write_projection` sets the directory to 0700.

Residuals carried forward unchanged from round 5: R1 (content rule does not inspect provenance and source fields), R2 (multi-marker co-occurrence), R4 (confusables outside the table), R6 (`register_scope` is a runtime mutator), R7 (unauthenticated manifest), R8 and R9 (crash and mutation windows).

### Phase 1 closure

- **Review:** round 6 on `98dbcf7` returned APPROVE_WITH_RESIDUALS. No gate-class findings.
- **Post-review cleanup (not re-reviewed; no behaviour change):** dead `reason` assignments removed; the projection directory is created owner-only (0700) from the start, closing the umask window; the stale rebuild-from-journal verification line is struck through.
- **Residuals accepted for Phase 1, carried into later phases:**
  - R1: the content rule inspects only `payload`. Provenance and source fields are not scanned. Phase 2 classifier must cover them.
  - R2: in-process code can reach `_conn`. Enforced at the service boundary in Phase 6.
  - R3: content-marker coverage is best-effort. Confusables outside the table are accepted residuals.
  - R4: over-blocking of unrelated text that names another scope's marker. Accepted; the marker list is configuration.
  - R5: `register_scope` is a runtime mutator for markers. Phase 6 service boundary must restrict it.
  - R6: the projection manifest is unauthenticated. Forging it requires write access to the owner-only projection directory, which is in-process trust.
  - R7: non-`ScopeContext` objects raise `AttributeError` rather than failing closed with a typed error. No data is returned.
  - R8: delete with `ctx=None` journals the caller-supplied id into the raw audit table (scope NULL, not visible to any view).
  - R9: the delete reason is free text with no length cap. Written only into the record's own scope journal, by a holder of that scope.
  - R10: the marker token-run matcher is separator-insensitive by design and over-blocks some strings.
  - R11: crash windows between record write and manifest rewrite can report `tampered` rather than `stale`.
- **Traceability:** the round-1 finding labelled F11 has no recoverable text in the repository; reviewer treats it as a numbering gap and judged the gated paths it would cover to be covered by F1, F7, F12, and F16.
- **Phase 1 status:** closed with the residuals above. Phase 2 may start.

### Decision: OCR is in alpha (Oct 25)

The user accepted OCR for alpha. The OCR adapter (Qwen3.5-2B via mlx-vlm) joins the four alpha adapters, for five in total. Known gap to resolve in the conformance suite: the smoke test lowercased its output, so the adapter must report case fidelity as a measured property, not a claim.

### Phase 2 closure

- **Review:** rounds 1–4 (f942747, 4c3f062, aaec341, a8ee2d7). Round 4 returned APPROVE_WITH_RESIDUALS, no gate-class defects. Gate defects found and fixed in earlier rounds: R-2 and R-5 (round 1 items), the overwrite bypass of a relied-on Verification (round 2), and the overwrite of a verified Claim (round 3).
- **Residuals accepted for Phase 2:** R-1 (`user_authored` is trusted from the context; enforced at the Phase 6 service boundary), R-3 (a registered verifier given a user-authored context), R-4 (delete scans all Summaries; no leak), R-6 (Source existence not checked; closed in Phase 3), RESIDUAL-1 (overwrite authority is by scope, not readability), RESIDUAL-2 (full scan in the reliance check), RESIDUAL-3 (version-2 check covers only the attribution column), RESIDUAL-4 (`meta.schema_version` written, never read).
- **Test gaps to fill:** a verified record remains deletable after the overwrite guard; `correct()` leaves a verified row unchanged; RESIDUAL-1 behaviour.
- **Phase 2 status:** closed with the residuals above. Phase 3 spec drafted at `docs/design/phase3-ingestion-retrieval.md`; implementation awaits go-ahead.

### Phase 3 dependency licences (verified from installed metadata)

- pypdf 6.21.2: BSD-3-Clause (PDF text layer; chosen over PyMuPDF, AGPL).
- python-docx: MIT (DOCX).
- Earlier: sqlite-vec Apache-2.0; pywhispercpp and mlx-vlm per their own metadata (to be recorded when vector and media adapters are wired).

### Phase 3 review round 1 (commit c1bf93e)

Verdict APPROVE_WITH_RESIDUALS, no gate-class findings. Build pass 2 addresses the residuals: R-6 source existence (same-scope check, generic refusal); the media crash-path test; the real-adapter gap. Remaining residual: vector path and measured-recall gate pending the gold set.

### Phase 3 review round 2 (commit f1cedd4, BLOCK, G-P3-1 and G-P3-2)

- G-P3-1: fts_candidates was ungated. Now takes ctx and checks every candidate with the Phase 1 read gate.
- G-P3-2: job_state, set_job and record_receipt were ungated. Now they take the named scope and ctx: job_state needs read on that scope, set_job needs write, record_receipt requires ctx and read on every named scope.
- Residual R-P3-1 (open): WhisperTranscriber leaks the ffmpeg output temp file on failure.
- Residual R-P3-2 (open): a crash mid-document leaves complete Observations and a 'running' job row. Rerun is idempotent but the partial output is not rolled back; to be decided with the crash-consistency ADR item.

### Phase 3 review round 3 (commit 581c19f, BLOCK, G-P3-3 and G-P3-4)

- G-P3-3: jobs were keyed without scope, so the scope gate checked a name the row did not carry. Fixed: jobs are keyed by (scope, source_id, stage, content_hash), schema version 4. The jobs table is Phase 3 idempotency state and unreleased, so migration 4 rebuilds it rather than migrating rows.
- G-P3-4: recalled text could be re-ingested as user speech. Fixed: Item.render() wraps recalled text in the recall fence; fence() is now applied to document text and derived media text as well as conversation turns. Residual: render() is best-effort; a caller that hands raw Item.text back is a Phase 6 service-boundary concern.
- Open residuals from review 581c19f: G-P3-5 (ffmpeg temp file on failure), G-P3-6 (malformed MATCH raises raw sqlite3 error), G-P3-7 (a version-3 database with an empty index opens without reindexing).

### Phase 3 review round 4 (commit fcc7961, BLOCK, G-P3-8)

- G-P3-8: a stored close tag could end the rendered recall block early, so the remainder was re-ingested as user speech. Fixed: Item.render() escapes recall-tag tokens in text; fence() removes from the first open tag through the last close tag, repeatedly, then strips stray tags.
- Open residuals from fcc7961 (to be decided, not yet fixed):
  - R-P3-8 whisper temp .wav leaked on ffmpeg failure (same as R-P3-1 / G-P3-5).
  - R-P3-9 crash mid-document leaves a committed Source and a prefix of its Observations readable; job stays 'running'.
  - R-P3-10 deleting an imported Source does not clear its jobs row, so a re-import is treated as done and returns without re-ingesting.
  - R-P3-11 an Observation may cite any same-scope record type, not only a Source (spec §4 / D3-3).
  - R-P3-12 media text returned at L2 without a machine-derived label on the read side (D3-6 not enforced on read).
  - R-P3-13 record attribution can change between the knowledge check and the row write (object mutation).
  - R-P3-14 fts_candidates scans all matches before the row-level gate (performance and exposure of counts only by timing).
  - R-P3-15 ingest_document parses before any authorization check (resource use by unauthorized callers).
  - R-P3-16 adapter scratch path is operator-specific and hard-coded.
  - R-P3-17 delete refusal confirms that some derived record exists in a scope the caller cannot delete (generic, no id).

### Phase 3 build pass 3: residuals, vector path, hybrid recall, recall gate

- R-P3-10 closed: deleting a Source removes its job row (and its vector rows). A re-import after delete ingests again.
- R-P3-12 closed: recalled items carry `derived`; machine-derived text renders with `derived="machine"`.
- N-P3-1 closed: scope_hint narrows lexical and vector candidates, not only the receipt.
- Vector path: one sqlite-vec table per scope (vec0, 1024-dim, name derived from a hash of the scope). vector_put needs write authority; vector_candidates queries only readable scopes, then gates each candidate. Hybrid recall fuses lexical and vector ranks with reciprocal-rank fusion (k=60).
- Embeddings: microsoft/harrier-oss-v1-0.6b (MIT), last-token pooling, query instruction prefix, through mlx-embeddings.
- Recall gate: eval/recall_gate.py ingests the vault (249 notes) and scores 40 gold entries.
  - Result on the DRAFT gold set: recall@5 lexical 0.825, vector 0.85, hybrid 0.90; recall@1 0.65 / 0.60 / 0.70.
  - Floor (proposed, not yet confirmed by the user): hybrid recall@5 >= 0.90 and >= lexical. Passes at the floor exactly.
  - Not acceptance evidence: the gold set is an unapproved draft. Four of the five hybrid misses are one query ("moltbook heartbeat receipt") that matches four near-identical receipts, so the gold set is ambiguous there.
  - Default mode in code stays "lexical". Vector is not enabled by default until the gold set is approved and the gate re-run.
  - Index time for 249 notes: about 1,300 s on this Mac (single-item embedding; batching is a later optimisation).
- Dependency licences added: sqlite-vec 0.1.9 (MIT per package metadata; Apache-2.0 also listed), mlx-embeddings 0.1.0 (GPL-3.0, compatible with an AGPL-3.0 Aletheia distribution; record and review), harrier-oss-v1-0.6b model weights (MIT).

### Recall gate result on gold set v0.2 (approved by user, Oct 26)

- recall@5: lexical 0.975, vector 0.95, hybrid 0.975. recall@1: 0.85 / 0.80 / 0.875. Floor 0.90 (user-confirmed as the proposal).
- Gate passes the floor and hybrid does not fall below lexical. Vector default-on decision is NOT made: the user has asked for a decision after paraphrase queries are added (the set has no paraphrase entries, so it does not test what vector adds).
- Harness fix (review b32f4d9, R-B6-3): the gate reports vector_default_on only for an approved gold set; otherwise "not decided".
- Review b32f4d9: APPROVE_WITH_RESIDUALS, no gate findings. Residuals open: R-B6-1 (Source-wide test filtering, narrowed to seed ids in this build), R-B6-2 (machine-derived label is mutable provenance, not enforced at write), R-B6-4 (newer user_version opens silently), R-B6-5 (vector_put does not check the record exists in the scope), R-B6-6 (sqlite-vec is a hard open-time dependency), R-B6-7 (fence drops text between two recall blocks), R-B6-8 (ingest_document raises instead of a named rejection for a content-rule paragraph), R-B6-9 (receipt.exclusions unreachable), R-B6-10 (D3-8 batch consent thresholds not implemented; they are a Phase 6 interaction concern and need a consent mechanism).
- Test flakiness: two real-model tests (VLM, whisper) failed once in a full run and passed on rerun. Recorded; not yet root-caused.

### Gold set v0.3 gate run (50 entries: 40 keyword, 10 paraphrase) and two fixes

- First v0.3 run (before stopword fix): overall recall@5 lexical 0.78, vector 0.82, hybrid 0.80. Keyword entries lexical 0.975 / vector 0.95 / hybrid 0.975. Paraphrase entries lexical 0.0, vector 0.3, hybrid 0.1. Floor not met overall; vector not enabled.
- Diagnosis of hybrid < vector on paraphrases: the lexical query included function words ("the", "is"), which matched unrelated notes and outranked vector hits in reciprocal-rank fusion. Fixed: function words are excluded from the lexical match (STOPWORDS in aletheia/retrieval.py), with a red-then-green test (tests/test_phase3_build7.py). The gate must be rerun to measure this.
- Test flakiness root cause (closed): an inherited PYTHONPATH from the agent runtime made numpy load from another install when pytest was launched from the agent process, so the real-model tests failed intermittently. Fixed by scripts/test.sh, which runs pytest with PYTHONPATH, PYTHONHOME and VIRTUAL_ENV removed. Run the suite through that script.


### Vector path improvement (user direction: "improve the vector path first")

- Gate v0.3 (50 entries), recall@5 by mode:
  - baseline (paragraph vectors only): vector 0.82 overall / 0.30 paraphrase; hybrid 0.82 / 0.20.
  - + one result per document (recall fix): no change on this set; a real defect (five paragraphs of one note filled the top five).
  - + document head vectors (opening 2000 chars): changes one entry; no gain. Kept as an option, off by default.
  - + document centroid (normalised mean of paragraph vectors, default on): vector 0.84 overall / 0.40 paraphrase; hybrid 0.84 / 0.30; keyword unchanged (vector 0.95, hybrid 0.975).
- Still below the 0.90 floor. The gate stays "not passed"; vector stays off by default.
- Paraphrase ranks (vector, one per document): [36, 14, 1, 1, none, 8, 14, 1, 24, 9]. Four right at rank 1; several at 8–14; some far off.
- Two paraphrases are vague by construction (the "groundwork file ..." and "bundle that brings ..." queries fit several notes). The paraphrase set needs review before the gate counts it.
- Options not yet taken: a larger embedding model (Colab, needs per-run authorization); reranking (an extra model); tuning fusion weights (not done: tuning on a 10-item set would overfit).


### Embedding model comparison (sub-4B only; 27B Harrier dropped by user direction)

- Harrier-oss-v1-0.6b (MIT, 1024-dim), centroid vectors: vector recall@5 0.84 overall; paraphrase 0.40; keyword 0.95; vector recall@1 0.68.
- Qwen/Qwen3-Embedding-0.6B (Apache-2.0, 1024-dim, mlx-embeddings), centroid vectors: vector recall@5 0.86 overall; paraphrase 0.60; keyword 0.925; vector recall@1 0.58. Hybrid recall@5 0.84 overall, paraphrase 0.40.
- Read: Qwen3 is better on paraphrases (the reason vector exists) and slightly worse on keyword and top-1. Neither clears the 0.90 floor, so vector stays off by default.
- Candidates not yet measured: BAAI/bge-m3 (MIT, 0.57B). Excluded: Qwen3-Embedding-4B (over 4B); EmbeddingGemma-300M (Gemma terms).
- Index time for the Qwen3 run was about 21 minutes on this machine under load; vectors are model-specific, so switching models requires re-indexing.


### Embedding model decision: BAAI/bge-m3 adopted (user, "Adopt BGE-M3 for sure")

- Adopted: BAAI/bge-m3 (MIT, 0.57B, 1024-dim, CLS pooling, no query prefix), run via mlx-embeddings from the mlx-community 4-bit build (337 MB).
- Gold v0.4 (74 entries, recall@5): vector 0.743 overall, paraphrase 0.50, keyword 0.95; hybrid 0.757 overall, keyword 1.0, paraphrase 0.47. Harrier and Qwen3 were 0.716 on vector overall.
- Caveats: 4-bit quantisation may cost accuracy (full-precision comparison not run); the paraphrase set is unapproved; the gap is small.
- Vector default stays OFF. The 0.90 floor is not met by any model or mode.
- Switching models requires re-indexing: vectors from different models must not mix. The embed cache is keyed per model.
- Harrier and Qwen3 adapters remain as alternatives; the gate harness defaults to bge.


### Gold set v0.4 approved; cleanup (2026-10-26)

- Gold set v0.4 (74 entries: 40 keyword, 34 paraphrase) approved by user. Status recorded in eval/gold-set-draft.json (gitignored).
- Full-precision BGE-M3 (mlx-community/bge-m3-mlx-fp16) download stalled at 455 MB of ~1.1 GB; not measured. Partial cache removed. The harness supports `bge-fp16` for a retry.
- Removed: Harrier-0.6B and Qwen3-Embedding-0.6B Hugging Face caches; their embedding caches; recall_gate WAL/SHM files; models/qwen_dl.log.
- Kept: BGE-M3 4-bit (adopted, in use); Qwen3.5-2B 8-bit (OCR/description adapter and tests); whisper large-v3-turbo q5_0 (transcription and tests); smoke fixtures.
- Other Hugging Face caches in ~/.cache/huggingface (Wan, MiniMax, neucodec, depth-anything, faster-whisper-base, etc.) are not created by Aletheia and were not touched.


### Quantisation decision: BGE-M3 4-bit adopted (user, 2026-10-26)

- Gold v0.4 (74 entries), recall@5. 4-bit: vector 0.743 (paraphrase 0.50, keyword 0.95), hybrid 0.757 (keyword 1.00). 8-bit: vector 0.770 (paraphrase 0.56, keyword 0.95), hybrid 0.730 (keyword 0.975).
- Differences are 2 to 4 queries out of 74; user judged them inconsequential and adopted the 4-bit build (smaller, already the code default).
- Full-precision fp16 not measured: download failed repeatedly (xet I/O error, then stalls).
- Vector stays off by default; neither build clears the 0.90 floor.
- 8-bit weights removed from the Hugging Face cache after this decision.


### Recall floor and vector default (user decision, 2026-10-27)

- Floor for enabling vector by default lowered from 0.90 to 0.70 (hybrid recall@5, approved gold set, must also be >= lexical recall@5). Set in eval/recall_gate.py as RECALL5_FLOOR.
- Basis: approved gold v0.4 (74 entries). BGE-M3 4-bit hybrid recall@5 0.757 (keyword 1.00, paraphrase 0.47); lexical 0.554. Vector alone 0.743 (paraphrase 0.50).
- The pass is carried mostly by keyword queries. Paraphrase recall stays weak (0.47 hybrid, 0.50 vector) and must be reported beside the headline number.
- Default mode: hybrid. Shipped default stays OFF until the Codex review of the vector path (717b85a) passes with no GATE findings.


### Review 717b85a (Claude subagent, adversarial) and response

- GATE F1 (fixed, uncommitted): vector_put authorized against the caller-supplied scope, not the stored record, and accepted unknown ids. Fix: look up the stored record; require it to exist, live in `scope`, and pass write authority. Regression tests: tests/test_phase3_review6.py (3 tests; 2 red before the fix). Suite passes.
- GATE F2 (reclassified as RESIDUAL + doc correction): the line "the paraphrase set is unapproved" in the 4-bit result entry above predates the user's approval of gold v0.4 (2026-10-26). The gold file status is correct. The real weakness is that the gate trusts a self-declared status string in the gold file. Residual: approval should be recorded out-of-band (for example a hash of the approved file) before the gate can decide a default.
- RESIDUAL F3: model identity is not stored in the vector table; mixing models in one scope is accepted silently. Harness-only isolation.
- RESIDUAL F4: overwrite and delete do not keep vector rows in step with records (old vector kept after overwrite; delete only clears the record's own scope table). Deletion across scopes should be closed with the F1 fix, but overwrite needs its own fix.
- RESIDUAL F5: embed cache keyed by text only, one file per model name.
- Review verdict on 717b85a: BLOCK (F1). Re-review required on the fixed commit before vector can default on.


### Review 717b85a residuals fixed (pass 7)

- F3 (model isolation): vector tables are now keyed by scope AND model (`vec_<scope-hash>_<model-hash>`). vector_put and vector_candidates require `model_id`. A query under one model cannot score rows built under another. Tests: tests/test_phase3_review7.py.
- F9 (stale vector on overwrite or correct): overwrite removes the record's vector rows; correct() removes the superseded original's rows. Tests: tests/test_phase3_review7.py.
- F6 (approval match): approval is exact: status must equal "APPROVED" and approved_sha256 must match the current entries. Prefix or lookalike statuses and edited sets do not count. The approved gold set was re-recorded: its content is unchanged since the user approved it; the previous status text is kept as approval_note. Tests: tests/test_gate_approval.py.
- Residual: the approver is still recorded in the gold file, which is not signed. The hash binds the content, not the identity of the approver.
- Gate re-run after the changes: hybrid recall@5 0.757, vector 0.743, decision "decided" from an approved set. Shipped default remains OFF until re-review.


### Review 3544e20 and default flip (2026-10-27)

- Review (Claude, CLI route, adversarial): APPROVE_WITH_RESIDUALS, no GATE findings. GATE-1 from ec112bc confirmed closed (approval pinned in tracked code).
- Default recall mode is now HYBRID (user direction: ship with vector on). recall() without an embedder raises ValueError (visible), never a silent lexical fallback. Test: tests/test_review9.py. Existing lexical-behaviour tests now name mode="lexical" explicitly.
- Residuals accepted and recorded:
  - R-model-id: Harrier and Qwen3 adapters report constant model ids (not used in production; their weights are removed). BGE ids are per checkpoint.
  - R-centroid: a document centroid is not recomputed when a paragraph is deleted; the Source's centroid still encodes the deleted paragraph (the deleted id cannot surface).
  - R-gold-constant: the pinned approval hash is a constant in tracked code; changes rely on git review of that line.
  - R-overwrite-no-reembed: an overwritten or corrected record has no vector until re-ingested (FTS holds the new text).
  - R-round: recall is rounded to 3 decimals before the floor comparison (no effect at n=74).
  - R-spec-drift: design doc D3-1 and older ADR lines still say 0.90; code and ADR decision say 0.70.
  - R-default-manual: vector_default_on is only written to the results file; the default is set by code, not by the gate.


### Embedder wiring (2026-10-27)

- aletheia/adapters.py: default_text_embedder() returns BGE-M3 4-bit (MIT) from the local Hugging Face cache. Missing weights raise RuntimeError (visible; no cloud or lexical fallback).
- Ingestor still takes an explicit embedder. The library has no production entry point yet; the composition root (the Phase 6 service or a CLI) must pass default_text_embedder(). Until one exists, records ingested without an embedder have no vectors and hybrid recall covers them lexically only.
- Tests: tests/test_review10.py. The real-model test runs when the weights are present and ingests a turn, then finds it by vector under the adopted model id.
- Process note: the factory and its tests were written in one step, so the tests did not fail first. They are new functions, so there was no earlier red state to show.
