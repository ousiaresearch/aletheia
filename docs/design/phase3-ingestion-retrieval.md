# Phase 3 — Ingestion and retrieval (spec)

Status: **specification, not implementation.** Implementation and its review each need an explicit go-ahead, in separate turns, per the phase gate.

Sources: `~/.hermes/plans/2026-10-24_aletheia-system-full-plan.md` §7 (ingestion), §8 (retrieval), Phase 3 exit list, and §16 open decisions. Inherits Phase 1 (scope gate, residuals R1–R11) and Phase 2 (knowledge rules, residuals R-1–R-6, RESIDUAL-1–4).

## 1. Scope

In:
- Conversation ingestion: user turns become Sources of record; assistant turns become assistant-authored Sources that may yield Hypotheses only.
- Document ingestion: Markdown, plain text, PDF with text layer, DOCX. Image and audio files are also ingested (user decision).
- Media ingestion: audio → transcript, image → description and OCR. Derived text is machine-derived, never verbatim.
- Durable, idempotent, resumable job queue in the same SQLite store.
- Tiered retrieval (L0 → L1 → L2) with scope-first candidate selection and RecallReceipt.
- Deletion propagation: cascade, receipt, derived-artifact removal.
- Context fencing: recalled and injected blocks are stripped or tagged before persistence.

Out (later phases): reasoning loop and model-layer evaluation (Phase 4), correction-as-of and export (Phase 5), desktop UI and Hermes shadow mode (Phase 6). Spreadsheets, slides, and web crawling stay out of alpha.

## 2. Decisions

**D3-1. Vector retrieval is on at ship, gated by a measured-recall pass before ship (user decision).** Phase 3 builds the lexical path (SQLite FTS5) and the vector path. The vector path is enabled as a default only if the measured-recall test passes before release, and the result is recorded in the ADR. The scope filter still applies before any index query, and the out-of-scope leakage test must pass for the vector path.

**D3-2. Scope before ranking, always.** Candidate selection reads only records the caller can read (the Phase 1 gate). Ranking cannot widen scope. For vector retrieval, the scope filter applies before the index query, with one index per scope (Phase 1 design), so out-of-scope neighbours cannot leak into results.

**D3-3. Documents are evidence, never instructions.** Document text is stored as Source and Observation records with attribution `document`. The Phase 2 rules already stop a document from creating a Decision, Commitment, or user-attributed record. Phase 3 adds a regression test where a document says "the user commits to X" and no Commitment appears.

**D3-4. Recall output is assembled at call time; nothing injectable is persisted.** A recall returns structured items with scope, tier, and locator. The assembled prompt text is not written back to the store. Re-injected recall is detected by fencing tags and not re-ingested.

**D3-5. Fencing at ingestion.** Any text carrying recall fence markers is stripped or tagged before a Source is created. Tagged content is stored with attribution `system` and `fenced = true` in provenance, and it can never be cited as user speech.

**D3-6. Media derived text is machine-derived.** Transcripts and descriptions carry `machine_derived = true`, model id, model version, and a confidence where the adapter provides one. They are never returned as verbatim quotation. OCR output (accepted for alpha, Oct 25) follows the same rule.

**D3-7. Jobs are idempotent by `(source_id, stage, content_hash)`.** A job that crashes mid-run leaves no half-written Observations: each job writes its outputs and its completion marker in one transaction. Re-running a completed job is a no-op.

**D3-8. Import is consent-gated for batches.** Batches over 10 files, 30 minutes of audio, or 100 MB require a confirmation, and any external provider call requires one. The import receipt records what was sent where before processing starts. Local-only is the default egress (plan §10).

**D3-9. Missing extraction is a named failure, never a silent success.** Encrypted PDF, corrupt zip, unsupported encoding, and failed transcription each produce a rejection with a reason code, and the Source is recorded as rejected, not ingested.

**D3-10. Deletion propagates to derived artifacts.** Deleting a Source removes its Observations and Summaries (the Phase 2 cascade extended to Observations), and removes the derived transcript and OCR text. A deletion produces a receipt naming counts only, never ids in a scope the caller cannot read.

**D3-11. Retrieval tiers.** L0 abstract (~100 tokens) → L1 overview (~2k tokens) → L2 full source, opened only when a higher tier does not satisfy the query. Tier selection is recorded in the receipt.

**D3-12. RecallReceipt.** Records the scope consulted, Sources read, Records selected, exclusions with reasons, and the tier that satisfied the query. Exclusion reasons are generic for out-of-scope records (the Phase 1 oracle rule) and detailed only for in-scope content (for example "withheld by content rule"). No secrets.

## 3. Schema additions

- `sources`: already Source records (Phase 2). Add `content_hash`, `document_ref` (author, document, version, location), `fenced`.
- `jobs`: `job_id`, `source_id`, `stage`, `content_hash`, `state` (`queued | running | done | failed`), `reason`, unique on `(source_id, stage, content_hash)`.
- `fts`: FTS5 virtual table over record text, with scope in the row and a scope filter on every query.
- `receipts`: `receipt_id`, `kind` (`recall | import | deletion`), `scope_consulted`, `payload_counts`, `created_at`. Stores no record text.

Migration 3, forward-only, transactional, following the Phase 2 pattern.

## 4. Tests (must exist and pass before the Phase 3 review)

Plan regression targets
- Memory-provider query returns zero ACS-project records, with ACS records indexed in FTS.
- Prompt injection in a document has no effect: no Commitment, Decision, or user-attributed record results.
- Re-injected recall is not persisted: a recall's output, fed back through ingestion, creates no Source.
- Deletion removes derived artifacts: after deleting a Source, its Observations, Summaries, transcript, and FTS rows are gone.

Ingestion
- User turn creates a user-attributed Source; assistant turn creates an assistant-attributed Source that cannot produce a Decision or Commitment.
- Document Observation cites an existing Source. (This closes Phase 2 residual R-6: the Source must exist.)
- Re-import of identical content hash is idempotent.
- Encrypted PDF, corrupt DOCX, and unsupported format each produce a named rejection and no Source of success.
- Fenced text is tagged and cannot be cited as user speech.
- Media output carries `machine_derived = true`; no path quotes it as verbatim.

Jobs
- Killed mid-job: restart completes the job once, with no partial Observations.
- Duplicate job submission is a no-op.

Retrieval
- Candidate scan excludes out-of-scope records before ranking; ranking cannot return an out-of-scope record even when it scores highest.
- Tier L0 satisfies a short query; L2 opens only when needed; the receipt records the tier.
- RecallReceipt exclusions for out-of-scope records are generic; in-scope exclusions carry reasons.
- Vector path, when enabled: an indexed out-of-scope record never appears in results (the plan §15 leakage test). Vector tests are required before any vector enablement, even though the default is off.

Migration
- Phase 2 database migrates to version 3; second run is a no-op; failure rolls back.

## 5. Exit criteria

- All §4 tests pass.
- Fresh independent adversarial review returns APPROVE or APPROVE_WITH_RESIDUALS with no gate-class findings.
- Recall measured on the gold set for the lexical path, recorded in the ADR. Vector recall is measured only if vector is to be enabled.

## 6. Open decisions (need the user)

1. **Vector retrieval in alpha.** D3-1 proposes off by default, enabled only after measured recall. Confirm, or enable at alpha ship.
2. **Formats for Phase 3 (user decision):** Markdown, plain text, PDF with text layer, DOCX, plus images and audio files. Scanned PDF pages reach the OCR adapter (accepted Oct 25).
3. **Batch thresholds.** Confirm 10 files, 30 minutes of audio, 100 MB as the consent triggers (plan §10 proposal, not yet confirmed).
4. **Gold set.** Recall needs a gold set. Who authors it, and from which sources? Proposal: the user authors 30 to 50 query-to-record pairs from their own corpus, scoped, and the set stays private.
EOF
echo drafted
## 7. Decisions received (user)

- D3-1 revised: vector retrieval on at ship, subject to a measured-recall pass before ship.
- Formats: Markdown, plain text, PDF with text layer, DOCX, images, and audio.
- Batch consent thresholds (user: "you're clear", recommendation adopted): ask before processing any batch over 10 files, over 30 minutes of audio, or over 100 MB; always ask before any external provider call. Config values, changeable.
- Gold set: explained to the user; not yet authorized. Draft from the Arien vault awaits approval.

## 8. Implementation status (first pass, for review)

- Built: conversation turns (user, agent, system attribution), fencing, document ingestion (txt, md, PDF text layer, DOCX) with named rejections, media ingestion through injected adapters, idempotent jobs, FTS5 index kept in step with every write, lexical scope-first recall, RecallReceipt (names and counts only), deletion removing index rows and derived Observations.
- Not built yet: the real media adapters (OCR and image description via the Qwen3.5-2B VLM, transcription via whisper.cpp); the vector path (needs an embedding adapter); the gold set and measured-recall gate (needs the gold set, which the user has not yet approved); tiers L1 and L2 (only L0 abstracts are returned); crash-kill test for media jobs.
- Migration 3 indexes existing rows.

## 9. Build pass 2 (as built)

- Real adapters, both local, no network: `aletheia/adapters.py`. `QwenVLM` (Qwen3.5-2B-8bit via mlx-vlm, image text and description; chat template applied). `WhisperTranscriber` (ffmpeg to 16 kHz mono WAV, then whisper.cpp large-v3-turbo q5_0). Integration tests run the real models: the VLM reads `ALETHEIA MEMORY TEST 42`; whisper transcribes the smoke clip.
- Retrieval tiers: L0 abstract, L1 overview (2000 chars), L2 full text. The smallest tier containing a query term is returned; the receipt records the highest tier used.
- Source existence (closes R-6): an Observation may cite only a Source in its own scope. A missing or foreign source gets one generic refusal, so there is no existence oracle.
- Media crash path: an interrupted derivation writes nothing; a rerun creates exactly one derived record.
- Still not built: the vector path (needs an embedding adapter), the measured-recall gate (needs the gold set approved), and a proper receipt for L1/L2 selection beyond the highest-tier field.
